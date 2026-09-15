"""Daemon-side adapter for bounded systemd hardware jobs. No resident helper."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import cast

from keymasq.common.types import JsonObject
from keymasq.masking.backend import (
    RUNTIME_DIR,
    STATE_DIR,
    DeviceInUseError,
    LinuxMaskBackend,
    finish_io,
    run_host,
)
from keymasq.masking.inventory import Attachment
from keymasq.masking.operations import REQUESTS

POLICY_DIR = Path("/var/lib/keymasq/masking")


async def request(operation: str, identity: str, **data: object) -> JsonObject:
    token = uuid.uuid4().hex
    path = REQUESTS / token

    def write() -> None:
        REQUESTS.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"operation": operation, "id": identity, **data}, stream)

    await finish_io(write)

    async def exchange() -> JsonObject:
        try:
            await run_host(
                "systemctl",
                "--no-ask-password",
                "start",
                f"keymasq-hardware@{token}.service",
                timeout=75,
            )
            result = cast(JsonObject, json.loads(await finish_io(path.read_text)))
            if result.get("status") != "ok":
                if result.get("error_code") == "device_in_use":
                    raise DeviceInUseError(
                        str(result.get("pid", "")), str(result.get("application", ""))
                    )
                raise OSError(str(result.get("message", "Privileged hardware operation failed")))
            return result
        finally:
            await finish_io(path.unlink, missing_ok=True)

    task = asyncio.create_task(exchange())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # systemctl exiting does not stop its job. Await the bounded job before
        # allowing rollback to race its sysfs writes.
        try:
            await task
        except (OSError, ValueError):
            pass
        raise


class HardwareBackend(LinuxMaskBackend):
    def __init__(self, identity: str = "") -> None:
        super().__init__(
            runtime_dir=RUNTIME_DIR / "reservations" / identity if identity else RUNTIME_DIR,
            state_dir=POLICY_DIR / "reservations" / identity if identity else POLICY_DIR,
            reservation_id=identity,
        )
        self.identity = identity

    def for_attachment(self, identity: str) -> HardwareBackend:
        # Reuse the privileged backend's identity validation.
        super().for_attachment(identity)
        return HardwareBackend(identity)

    def prepare_directories(self) -> None:
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Preserve existing development selections when switching architectures.
        old = STATE_DIR / "reservations" / self.identity if self.identity else STATE_DIR
        for name in ("policy.json", "selection.json"):
            target = self.state_dir / name
            if not target.exists() and (old / name).exists():
                target.write_bytes((old / name).read_bytes())
                target.chmod(0o600)

    def reservation_ids(self) -> list[str]:
        return sorted(set(super().reservation_ids()) | set(LinuxMaskBackend().reservation_ids()))

    async def install_rules(self, attachment: Attachment) -> None:
        await request("arm", self.identity)

    async def activate(self, attachment: Attachment) -> list[str]:
        result = await request("activate", self.identity, generation=attachment.generation)
        self.active_attachment = await finish_io(
            self.inventory.resolve, self.identity, str(result["generation"])
        )
        return cast(list[str], result["event_nodes"])

    async def refresh_interfaces(self, attachment: Attachment) -> list[str]:
        result = await request("refresh", self.identity, generation=attachment.generation)
        return cast(list[str], result["event_nodes"])

    async def recover(self, *, keep_rules: bool = False) -> None:
        if self.journal.exists() or self.armed or self.permissions.exists():
            await request("recover", self.identity, keep_rules=keep_rules)
