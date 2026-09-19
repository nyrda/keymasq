"""Short-lived privileged hardware operations, invoked by keymasq-record."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pwd
import re
import stat
from pathlib import Path
from typing import cast

from keymasq.common.types import JsonObject
from keymasq.masking.backend import (
    DeviceAbsentError,
    DeviceInUseError,
    LinuxMaskBackend,
    MaskOperationError,
    finish_io,
    run_host,
    save_json,
)

REQUESTS = Path("/run/keymasq/hardware-requests")
REQUEST_ID = re.compile(r"[0-9a-f]{32}\Z")
ATTACHMENT_ID = re.compile(r"[0-9a-f]{24}\Z")
INPUT_NODE_NAME = re.compile(r"(event|js)[0-9]{1,5}\Z")
SYS_CLASS_INPUT = Path("/sys/class/input")
MAX_REQUEST = 65536
MAX_TRIGGER_NODES = 64
# The daemon waits 15s for a named-node job and 30s for a subsystem one (see
# keymasqd/runtime/source_hiding.py). udevadm must give up first, leaving room
# for the unit and helper start, so the daemon normally reads a real result.
# A job that outlives the daemon's wait is harmless: a trigger only replays
# the flag files, which stay the source of truth.
NODE_TRIGGER_TIMEOUT_S = 8.0
SUBSYSTEM_TRIGGER_TIMEOUT_S = 20.0
log = logging.getLogger("keymasq.masking")


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


def trigger_nodes(message: JsonObject) -> list[str] | None:
    """Validate a source-hiding udev trigger; the daemon supplies only node names."""
    raw_names = message.get("names")
    if raw_names is None:
        return None
    if not isinstance(raw_names, list) or not raw_names or len(raw_names) > MAX_TRIGGER_NODES:
        raise ValueError("Invalid input node list for udev trigger")
    names: list[str] = []
    for raw_name in cast(list[object], raw_names):
        if not isinstance(raw_name, str) or not INPUT_NODE_NAME.fullmatch(raw_name):
            raise ValueError("Invalid input node name for udev trigger")
        if raw_name not in names:
            names.append(raw_name)
    # A node that disappeared before the job ran has nothing left to re-evaluate.
    return [name for name in names if (SYS_CLASS_INPUT / name).exists()]


async def trigger_input(message: JsonObject, root: LinuxMaskBackend) -> JsonObject:
    """Re-run udev rules so 99-keymasq-hide-grabbed.rules sees changed hiding flags.

    Writing a sysfs uevent file needs root, which is the only privilege source
    hiding needs; the flag files themselves belong to the daemon.
    """
    nodes = await finish_io(trigger_nodes, message)
    if nodes is not None and not nodes:
        return {"triggered": []}
    await finish_io(root.prepare_directories)
    with (root.runtime_dir / "operations.lock").open("a") as global_lock:
        fcntl.flock(global_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        await run_host(
            "udevadm",
            "trigger",
            "--subsystem-match=input",
            "--action=change",
            *(f"--sysname-match={name}" for name in nodes or ()),
            "--settle",
            timeout=SUBSYSTEM_TRIGGER_TIMEOUT_S if nodes is None else NODE_TRIGGER_TIMEOUT_S,
        )
    return {"triggered": nodes if nodes is not None else ["input"]}


async def execute(message: JsonObject, root: LinuxMaskBackend) -> JsonObject:
    operation = message.get("operation")
    if operation == "trigger":
        return await trigger_input(message, root)
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
            try:
                raw = await finish_io(path.read_text)
            except FileNotFoundError as exc:
                # Saved by an earlier build, or never activated as root. Only a
                # connected activation can record a selector this job may trust.
                raise MaskOperationError(
                    "selector_missing",
                    "This saved mask has no root-recorded selector; "
                    "connect the device and confirm masking again",
                ) from exc
            try:
                # A damaged record is permanent until a connected activation
                # rewrites it; JSON errors are ValueErrors and belong here too.
                selector = json.loads(raw)
                if not isinstance(selector, dict):
                    raise ValueError("selector.json must contain a JSON object")
                attachment = await finish_io(
                    backend.inventory.from_selector, cast(JsonObject, selector)
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise MaskOperationError(
                    "selector_invalid", f"The saved selector cannot be used: {exc}"
                ) from exc
            await backend.install_rules(attachment)
            return {}
        try:
            attachment = await finish_io(
                backend.inventory.resolve, identity, str(message.get("generation", ""))
            )
        except ValueError as exc:
            raise DeviceAbsentError(str(exc)) from exc
        if operation == "activate":
            # Offline arming can only reuse an identity discovered by root.
            await finish_io(
                save_json,
                backend.state_dir / "selector.json",
                backend.inventory.selector(attachment),
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
        except Exception as exc:
            log.exception("Hardware operation failed")
            result = {"status": "error", "message": str(exc)}
            code = getattr(exc, "code", None)
            if isinstance(code, str) and code:
                result["error_code"] = code
            if isinstance(exc, DeviceInUseError):
                result.update({"application": exc.application, "pid": exc.pid})

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
