"""Short-lived privileged hardware operations, invoked by keymasq-record."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pwd
import re
import stat
from pathlib import Path
from typing import cast

from keymasq.common.types import JsonObject
from keymasq.masking.backend import (
    DeviceInUseError,
    LinuxMaskBackend,
    finish_io,
    run_host,
    save_json,
)

REQUESTS = Path("/run/keymasq/hardware-requests")
REQUEST_ID = re.compile(r"[0-9a-f]{32}\Z")
ATTACHMENT_ID = re.compile(r"[0-9a-f]{24}\Z")
MAX_REQUEST = 65536


def open_request(token: str) -> int:
    """Pin a daemon-owned request inode before reading or writing it as root."""
    if not REQUEST_ID.fullmatch(token):
        raise ValueError("Invalid hardware operation identity")
    uid = pwd.getpwnam("keymasq").pw_uid
    parent = os.open(REQUESTS, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(parent)
        if info.st_uid != uid or info.st_mode & 0o022:
            raise PermissionError("Unsafe hardware request directory")
        fd = os.open(token, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    finally:
        os.close(parent)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != uid
        or info.st_nlink != 1
        or info.st_mode & 0o077
        or info.st_size > MAX_REQUEST
    ):
        os.close(fd)
        raise PermissionError("Unsafe hardware request file")
    return fd


async def execute(message: JsonObject, root: LinuxMaskBackend) -> JsonObject:
    operation = message.get("operation")
    if operation not in {"activate", "arm", "refresh", "recover"}:
        raise ValueError("Unknown privileged hardware operation")
    identity = str(message.get("id", ""))
    if not ATTACHMENT_ID.fullmatch(identity):
        raise ValueError("Invalid physical attachment identity")
    backend = root.for_attachment(identity)
    await finish_io(backend.prepare_directories)
    # Separate jobs for different attachments may run concurrently. A global
    # recovery acquires this same lock exclusively after systemd stops them.
    with (
        (root.runtime_dir / "operations.lock").open("a") as global_lock,
        (backend.runtime_dir / "operation.lock").open("a") as lock,
    ):
        fcntl.flock(global_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if operation == "recover":
            keep = message.get("keep_rules", False)
            if not isinstance(keep, bool):
                raise ValueError("Invalid rule retention choice")
            await backend.recover(keep_rules=keep)
            return {}
        if operation == "arm":
            path = backend.state_dir / "selector.json"
            selector = (
                json.loads(await finish_io(path.read_text))
                if path.exists()
                else json.loads(await finish_io((backend.state_dir / "policy.json").read_text))[
                    "usb_selector"
                ]
            )
            attachment = await finish_io(backend.from_selector, selector)
            await backend.install_rules(attachment)
            return {}
        attachment = await finish_io(
            backend.inventory.resolve, identity, str(message.get("generation", ""))
        )
        if operation == "activate":
            # Offline arming can only reuse an identity discovered by root.
            await finish_io(
                save_json, backend.state_dir / "selector.json", backend.selector(attachment)
            )
            nodes = await backend.activate(attachment)
            current = backend.active_attachment or attachment
        else:
            nodes = await backend.refresh_interfaces(attachment)
            current = attachment
        return {"event_nodes": nodes, "generation": current.generation}


async def run_request(token: str, root: LinuxMaskBackend | None = None) -> None:
    backend = root or LinuxMaskBackend()
    await finish_io(backend.prepare_directories)
    fd = await finish_io(open_request, token)
    try:
        raw = await finish_io(os.read, fd, MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise ValueError("Hardware request is too large")
        try:
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise ValueError("Expected a hardware request object")
            result = {"status": "ok", **await execute(cast(JsonObject, message), backend)}
        except (OSError, ValueError, KeyError) as exc:
            result = {"status": "error", "message": str(exc)}
            if isinstance(exc, DeviceInUseError):
                result.update(
                    {"error_code": "device_in_use", "application": exc.application, "pid": exc.pid}
                )

        # This is the already validated inode, never a path supplied in JSON.
        def respond() -> None:
            with os.fdopen(os.dup(fd), "w") as stream:
                stream.seek(0)
                stream.truncate()
                json.dump(result, stream)
                stream.flush()
                os.fsync(stream.fileno())

        await finish_io(respond)
    finally:
        os.close(fd)


async def recover_all(root: LinuxMaskBackend | None = None) -> None:
    backend = root or LinuxMaskBackend()
    await finish_io(backend.prepare_directories)
    with (backend.runtime_dir / "operations.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        failures = []
        for identity in await finish_io(backend.reservation_ids):
            try:
                await backend.for_attachment(identity).recover()
            except (OSError, ValueError) as exc:
                failures.append(f"{identity}: {exc}")
        # Interrupted early implementations used the top-level journal.
        await backend.recover()
        if failures:
            raise OSError("Hardware recovery remains incomplete: " + "; ".join(failures))


async def recover_hardware() -> None:
    # Stop outstanding jobs before taking their undo records. Jobs do not
    # depend on keymasqd.service: foreground development uses them as well.
    await run_host("systemctl", "stop", "keymasq-hardware@*.service", timeout=10)
    await recover_all()


def main(operation: str, token: str = "") -> None:
    if os.geteuid() != 0:
        raise PermissionError("Hardware operations require root")
    if "PKEXEC_UID" in os.environ:
        raise PermissionError("Recording authorization does not authorize hardware operations")
    asyncio.run(run_request(token) if operation == "hardware-operation" else recover_hardware())
