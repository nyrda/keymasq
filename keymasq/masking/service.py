"""Root companion service: reservation deadlines outlive the remapping process."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import logging
import os
import pwd
import signal
import socket
import struct
import time
import uuid
from collections.abc import Callable
from typing import cast

from keymasq.common.types import JsonObject
from keymasq.masking.backend import SOCKET_PATH, LinuxMaskBackend, save_json

log = logging.getLogger("keymasq.masking")
TRIAL_SECONDS = 30.0
LEASE_SECONDS = 12.0


def notify(message: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
        connection.sendto(message.encode(), address)


class MaskSupervisor:
    def __init__(
        self, backend: LinuxMaskBackend, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.backend = backend
        self.clock = clock
        self.state: JsonObject = {"state": "unmasked"}
        self.deadline = 0.0
        self.last_heartbeat = 0.0
        self.owner_pidfd: int | None = None
        self.apply_task: asyncio.Task[None] | None = None
        self.recovery_lock = asyncio.Lock()
        self.operation_lock = asyncio.Lock()
        self.policy: JsonObject = {}
        self.session_uid: int | None = None

    async def load_policy(self) -> None:
        path = self.backend.state_dir / "policy.json"
        if path.exists():
            self.policy = cast(JsonObject, json.loads(await asyncio.to_thread(path.read_text)))

    async def save_policy(self, persist: object) -> None:
        if not isinstance(persist, bool):
            raise ValueError("The startup masking preference must be true or false")
        if self.session_uid is None:
            raise ValueError("No authenticated user session owns this mask")
        policy: JsonObject = {
            "id": self.state.get("id", self.policy.get("id")),
            "owner_uid": self.session_uid,
            "persist": persist,
        }
        await asyncio.to_thread(save_json, self.backend.state_dir / "policy.json", policy)
        self.policy = policy

    async def autostart(self) -> None:
        if (
            self.active
            or self.recovery_lock.locked()
            or self.backend.journal.exists()
            or (self.backend.state_dir / "suspended").exists()
            or not self.policy.get("persist")
            or self.session_uid is None
            or self.policy.get("owner_uid") != self.session_uid
        ):
            return
        attachments = await asyncio.to_thread(self.backend.inventory.scan)
        attachment = next(
            (item for item in attachments if item.identity == self.policy["id"]), None
        )
        if attachment is None or not attachment.supported:
            return  # Hardware and its driver may still be appearing during boot.
        try:
            await self._request(
                {
                    "command": "mask",
                    "id": attachment.identity,
                    "generation": attachment.generation,
                }
            )
            self.state["automatic"] = True
        except (ValueError, OSError) as exc:
            self.state["error"] = str(exc)
            await asyncio.to_thread(
                (self.backend.state_dir / "suspended").write_text, "automatic_start_failed\n"
            )

    @property
    def active(self) -> bool:
        return self.state.get("state") in {"applying", "acquiring", "trial", "masked"}

    def status(self) -> JsonObject:
        return {
            **self.state,
            "remaining_seconds": max(0, int(self.deadline - self.clock() + 0.999))
            if self.state.get("state") in {"applying", "acquiring", "trial"}
            else 0,
            "remapping_suspended": (self.backend.state_dir / "suspended").exists(),
            "persist": self.policy.get("persist", True),
            "has_saved_mask": bool(self.policy),
            "saved_id": self.policy.get("id"),
        }

    async def request(self, message: JsonObject) -> JsonObject:
        async with self.operation_lock:
            return await self._request(message)

    async def _request(self, message: JsonObject) -> JsonObject:
        command = message.get("command")
        if command == "startup":
            uid = message.get("uid")
            if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
                raise ValueError("An authenticated user ID is required")
            self.session_uid = uid
            await self.autostart()
            return self.status()
        if command == "heartbeat":
            self.last_heartbeat = self.clock()
            await self.autostart()
            return self.status()
        if command in {"status", "inventory"}:
            result = self.status()
            if command == "inventory":
                attachments = await asyncio.to_thread(self.backend.inventory.scan)
                result["devices"] = [item.as_json() for item in attachments]
                if self.state.get("id") and not any(
                    item.identity == self.state["id"] for item in attachments
                ):
                    result["devices"].append(
                        {
                            **self.state,
                            "supported": False,
                            "unsupported_reason": "Not connected",
                        }
                    )
            return result
        if command == "mask":
            if self.active or self.recovery_lock.locked() or self.backend.journal.exists():
                raise ValueError("Restore the current hardware mask before starting another trial")
            attachment = await asyncio.to_thread(
                self.backend.inventory.resolve,
                str(message.get("id", "")),
                str(message.get("generation", "")),
            )
            self.backend.validate(attachment)
            await asyncio.to_thread((self.backend.state_dir / "suspended").unlink, missing_ok=True)
            self.state = {
                **attachment.as_json(),
                "state": "applying",
                "id": attachment.identity,
                "generation": attachment.generation,
                "name": attachment.name,
                "main_hid": attachment.main_hid,
                "token": uuid.uuid4().hex,
            }
            self.deadline = self.clock() + TRIAL_SECONDS
            self.last_heartbeat = self.clock()

            async def apply() -> None:
                try:
                    nodes = await self.backend.activate(attachment)
                    self.state.update({"state": "acquiring", "event_nodes": nodes})
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("Hardware masking failed")
                    self.state["error"] = str(exc)
                    # The independent monitor performs serialized recovery.
                    self.deadline = self.clock()

            self.apply_task = asyncio.create_task(apply(), name="hardware-mask-activation")
            return self.status()
        if command in {"ready", "keep"}:
            if message.get("token") != self.state.get("token") or not self.active:
                raise ValueError("This hardware trial has ended")
            if self.clock() >= self.deadline:
                raise ValueError("The hardware trial expired; access is being restored")
            expected = "acquiring" if command == "ready" else "trial"
            if self.state.get("state") != expected:
                raise ValueError("The replacement controller is not ready for confirmation")
            if command == "keep":
                await self.save_policy(message.get("persist", True))
            if self.state.get("state") != expected:
                raise ValueError("This hardware trial has ended")
            self.state["state"] = (
                "masked" if command == "keep" or self.state.get("automatic") else "trial"
            )
            if self.state["state"] == "masked":
                await asyncio.to_thread(
                    save_json, self.backend.state_dir / "selection.json", self.state
                )
            return self.status()
        if command == "persistence":
            if self.active:
                if self.state.get("state") != "masked" or message.get("token") != self.state.get(
                    "token"
                ):
                    raise ValueError(
                        "Confirm the hardware trial before changing its startup preference"
                    )
            elif not self.policy or message.get("id") != self.policy.get("id"):
                raise ValueError("No confirmed hardware mask was selected")
            if self.policy and self.policy.get("owner_uid") != self.session_uid:
                raise ValueError("This startup preference belongs to another user")
            await self.save_policy(message.get("persist"))
            return self.status()
        if command == "restore":
            reason = "user_restore"
            if message.get("reason") == "lifecycle_stop":
                reason = "lifecycle_stop"
            if message.get("reason") == "replacement_unavailable":
                reason = "replacement_unavailable"
                self.state["error"] = (
                    "The replacement controller stopped; restoring hardware access"
                )
            await self._restore(reason, stop_owner=False)
            return self.status()
        if command == "resume":
            if self.active or self.recovery_lock.locked() or self.backend.journal.exists():
                raise ValueError("Restore the current mask before resuming remapping")
            await asyncio.to_thread((self.backend.state_dir / "suspended").unlink, missing_ok=True)
            return self.status()
        raise ValueError("Unknown hardware masking operation")

    async def restore(self, reason: str, *, stop_owner: bool) -> None:
        async with self.operation_lock:
            await self._restore(reason, stop_owner=stop_owner)

    async def owner_disconnected(self) -> None:
        async with self.operation_lock:
            # A clean supervisor stop closes the daemon while recovery runs.
            # Recheck after that recovery finishes before classifying the loss.
            if self.active or self.backend.journal.exists():
                await self._restore("daemon_disconnected", stop_owner=True)

    async def _restore(self, reason: str, *, stop_owner: bool) -> None:
        async with self.recovery_lock:
            had_mask = self.active or self.backend.journal.exists()
            clean = reason in {"lifecycle_stop", "service_stopped"}
            if not clean or (had_mask and self.state.get("state") != "masked"):
                await asyncio.to_thread(
                    (self.backend.state_dir / "suspended").write_text, reason + "\n"
                )
            if not had_mask:
                return
            self.state.update({"state": "restoring", "reason": reason})
            if stop_owner and self.owner_pidfd is not None:
                # Close stale evdev grabs and uinput outputs even if keymasqd is
                # SIGSTOP'ed. systemd may restart it; the suspension survives.
                with contextlib.suppress(ProcessLookupError):
                    signal.pidfd_send_signal(self.owner_pidfd, signal.SIGKILL)
            task = self.apply_task
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            try:
                await self.backend.recover()
            except Exception as exc:
                await asyncio.to_thread(
                    (self.backend.state_dir / "suspended").write_text, "recovery_failed\n"
                )
                self.state.update({"state": "recovery_failed", "error": str(exc)})
                log.exception("Hardware restoration failed; recovery will retry")
                raise
            self.state.update({"state": "restored", "event_nodes": []})
            await asyncio.to_thread(
                save_json, self.backend.state_dir / "selection.json", self.state
            )

    async def monitor_once(self) -> None:
        async with self.operation_lock:
            await self._monitor_once()

    async def _monitor_once(self) -> None:
        if self.state.get("state") == "recovery_failed":
            await self._restore("retry", stop_owner=True)
        elif self.active:
            if self.state.get("error"):
                await self._restore("activation_failed", stop_owner=False)
                return
            try:
                await asyncio.to_thread(
                    self.backend.inventory.resolve,
                    str(self.state.get("id", "")),
                    str(self.state.get("generation", "")),
                )
            except ValueError:
                await self._restore("hardware_disconnected", stop_owner=True)
                return
            others = await asyncio.to_thread(
                self.backend.other_steam_controllers, str(self.state.get("main_hid", ""))
            )
            if others:
                await self._restore("controller_scope_changed", stop_owner=True)
            elif self.clock() - self.last_heartbeat > LEASE_SECONDS:
                await self._restore("daemon_unresponsive", stop_owner=True)
            elif self.state.get("state") != "masked" and self.clock() >= self.deadline:
                failed = bool(self.state.get("error"))
                await self._restore(
                    "activation_failed" if failed else "trial_expired", stop_owner=not failed
                )

    async def monitor(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            try:
                await self.monitor_once()
            except Exception:
                log.exception("Hardware recovery monitor will retry")
                await asyncio.sleep(1)
            notify("WATCHDOG=1")


async def serve(supervisor: MaskSupervisor) -> None:
    backend = supervisor.backend
    backend.prepare_directories()
    if backend.journal.exists():
        await asyncio.to_thread((backend.state_dir / "suspended").write_text, "service_restart\n")
    await backend.recover()
    await supervisor.load_policy()
    selection = backend.state_dir / "selection.json"
    if selection.exists():
        supervisor.state = {
            **cast(JsonObject, json.loads(await asyncio.to_thread(selection.read_text))),
            "state": "restored",
            "event_nodes": [],
        }
    account = pwd.getpwnam("keymasq")
    owner: asyncio.StreamWriter | None = None
    clients: set[asyncio.StreamWriter] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal owner
        transport = writer.get_extra_info("socket")
        pid, uid, _gid = struct.unpack(
            "3i", transport.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        admin = uid == 0
        if uid not in {0, account.pw_uid} or (not admin and owner is not None):
            writer.close()
            await writer.wait_closed()
            return
        clients.add(writer)
        if not admin:
            owner = writer
            supervisor.owner_pidfd = os.pidfd_open(pid)
        try:
            while line := await reader.readline():
                try:
                    raw_message: object = json.loads(line)
                    if not isinstance(raw_message, dict):
                        raise ValueError("Expected a request object")
                    message = cast(JsonObject, raw_message)
                    if admin:
                        if message.get("command") != "restore":
                            raise ValueError("The administrative connection only restores hardware")
                        await supervisor.restore("admin_restore", stop_owner=True)
                        result = supervisor.status()
                    else:
                        result = await supervisor.request(message)
                    response: JsonObject = {"status": "ok", **result}
                except (ValueError, OSError, KeyError) as exc:
                    response = {"status": "error", "message": str(exc)}
                writer.write((json.dumps(response) + "\n").encode())
                await writer.drain()
        except (ConnectionError, ValueError):
            log.debug("Masking client disconnected", exc_info=True)
        finally:
            clients.discard(writer)
            if owner is writer:
                try:
                    await supervisor.owner_disconnected()
                finally:
                    supervisor.session_uid = None
                    owner = None
                    if supervisor.owner_pidfd is not None:
                        os.close(supervisor.owner_pidfd)
                        supervisor.owner_pidfd = None
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    SOCKET_PATH.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(handle, str(SOCKET_PATH), limit=65536)
    os.chown(SOCKET_PATH, 0, account.pw_gid)
    os.chmod(SOCKET_PATH, 0o660)
    monitor = asyncio.create_task(supervisor.monitor(), name="hardware-mask-deadlines")
    notify("READY=1")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        # Python 3.13+ waits for connected clients in Server.wait_closed().
        # Close the listener and clients explicitly before awaiting it.
        server.close()
        monitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor
        await supervisor.restore("service_stopped", stop_owner=True)
        for client in tuple(clients):
            client.close()
        await server.wait_closed()
        SOCKET_PATH.unlink(missing_ok=True)


async def administrative_restore() -> None:
    reader, writer = await asyncio.open_unix_connection(str(SOCKET_PATH))
    try:
        writer.write(b'{"command":"restore"}\n')
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readline(), 30))
        if response.get("status") != "ok":
            raise RuntimeError(response.get("message", "Restoration failed"))
        print("Hardware access restored. Remapping remains suspended until explicitly resumed.")
    finally:
        writer.close()
        await writer.wait_closed()


def main() -> None:
    parser = argparse.ArgumentParser(description="Keymasq hardware masking and recovery service")
    parser.add_argument(
        "--recover", action="store_true", help="restore hardware and suspend remapping"
    )
    parser.add_argument("--recover-offline", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("this service and its administrative recovery command require root")
    logging.basicConfig(level=logging.INFO)
    backend = LinuxMaskBackend()
    backend.prepare_directories()
    if args.recover and SOCKET_PATH.exists():
        try:
            asyncio.run(administrative_restore())
            return
        except (ConnectionRefusedError, FileNotFoundError):
            pass  # A stale socket can remain after an abrupt service failure.
    with (backend.runtime_dir / "service.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.recover or args.recover_offline:
            if args.recover or backend.journal.exists():
                (backend.state_dir / "suspended").write_text("offline_recovery\n")
            asyncio.run(backend.recover())
        else:
            asyncio.run(serve(MaskSupervisor(backend)))


if __name__ == "__main__":
    main()
