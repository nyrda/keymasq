"""Daemon-side adapter for bounded systemd hardware jobs. No resident helper."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import cast

from keymasq.common.types import JsonObject
from keymasq.masking.backend import DeviceInUseError, finish_io, run_host
from keymasq.masking.inventory import Attachment, HardwareInventory
from keymasq.masking.operations import REQUESTS
from keymasq.masking.paths import (
    POLICY_DIR,
    RUNTIME_DIR,
    STATE_DIR,
    reservation_ids,
    validate_identity,
)


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
                "--job-mode=fail",
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


class SystemdMaskBackend:
    """Request privileged transactions; own no sysfs or permission mutations."""

    def __init__(self, identity: str = "", inventory: HardwareInventory | None = None) -> None:
        self.identity = identity
        self.inventory = inventory or HardwareInventory()
        self.runtime_dir = RUNTIME_DIR / "reservations" / identity if identity else RUNTIME_DIR
        self.state_dir = POLICY_DIR / "reservations" / identity if identity else POLICY_DIR
        self.journal = self.runtime_dir / "journal.json"
        self.permissions = self.runtime_dir / "permissions.json"
        self.armed_record = self.runtime_dir / "armed.json"
        suffix = f"-{identity}" if identity else ""
        self.early = Path("/run/udev/rules.d") / f"72-keymasq-masking{suffix}.rules"
        self.late = Path("/run/udev/rules.d") / f"99-zz-keymasq-masking{suffix}.rules"
        self.active_attachment: Attachment | None = None

    @property
    def armed(self) -> bool:
        return self.early.exists() or self.late.exists() or self.armed_record.exists()

    def for_attachment(self, identity: str) -> SystemdMaskBackend:
        validate_identity(identity)
        return SystemdMaskBackend(identity, self.inventory)

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
        return reservation_ids(self.runtime_dir, self.state_dir, RUNTIME_DIR, STATE_DIR)

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
