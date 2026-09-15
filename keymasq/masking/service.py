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
from keymasq.masking.backend import SOCKET_PATH, DeviceInUseError, LinuxMaskBackend, save_json
from keymasq.masking.inventory import Attachment

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


class MaskReservation:
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
        self.recovery_task: asyncio.Task[None] | None = None
        self.recovery_lock = asyncio.Lock()
        self.operation_lock = asyncio.Lock()
        self.policy: JsonObject = {}
        self.session_uid: int | None = None
        self.quiesced = asyncio.Event()

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
        selector = self.state.get("usb_selector", self.policy.get("usb_selector"))
        if isinstance(selector, dict):
            policy["usb_selector"] = selector
        await asyncio.to_thread(save_json, self.backend.state_dir / "policy.json", policy)
        self.policy = policy

    async def arm_saved_usb(self) -> None:
        selector = self.policy.get("usb_selector")
        if isinstance(selector, dict) and not self.backend.armed:
            await self.backend.install_rules(self.backend.from_selector(selector))

    async def autostart(self) -> None:
        if (
            self.active
            or self.recovery_lock.locked()
            or self.backend.journal.exists()
            or not self.policy.get("persist")
            or self.session_uid is None
            or self.policy.get("owner_uid") != self.session_uid
        ):
            return
        suspended = self.backend.state_dir / "suspended"
        if suspended.exists():
            reason = (await asyncio.to_thread(suspended.read_text)).strip()
            if reason not in {"lifecycle_stop", "service_stopped"}:
                return
            # A clean shutdown can interrupt automatic reacquisition. The
            # authenticated owner's confirmed policy still authorizes startup.
            await asyncio.to_thread(suspended.unlink, missing_ok=True)
        await self.arm_saved_usb()
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
        paused = (self.backend.state_dir / "suspended").exists()
        explicitly_stopped = paused and self.state.get("reason") in {
            "user_restore",
            "admin_restore",
            "offline_recovery",
        }
        return {
            **self.state,
            "enabled": self.active or bool(self.policy.get("persist") and not explicitly_stopped),
            "remaining_seconds": max(0, int(self.deadline - self.clock() + 0.999))
            if self.state.get("state") in {"applying", "acquiring", "trial"}
            else 0,
            "remapping_suspended": paused,
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
                raise ValueError("This attachment already has a mask or pending recovery")
            attachment = await asyncio.to_thread(
                self.backend.inventory.resolve,
                str(message.get("id", "")),
                str(message.get("generation", "")),
            )
            self.backend.validate(attachment)
            confirmed = (
                message.get("persist") is True
                and self.policy.get("id") == attachment.identity
                and self.policy.get("owner_uid") == self.session_uid
            )
            if confirmed:
                await self.save_policy(True)
            await asyncio.to_thread((self.backend.state_dir / "suspended").unlink, missing_ok=True)
            self.state = {
                **attachment.as_json(),
                "state": "applying",
                "id": attachment.identity,
                "generation": attachment.generation,
                "name": attachment.name,
                "main_hid": attachment.main_hid if attachment.is_deck else "",
                "attachment_path": str(attachment.syspath),
                "quiesce_required": True,
                "token": uuid.uuid4().hex,
            }
            if attachment.transport == "usb":
                self.state["usb_selector"] = self.backend.selector(attachment)
            if confirmed:
                self.state["automatic"] = True
            self.deadline = self.clock() + TRIAL_SECONDS
            self.last_heartbeat = self.clock()
            self.quiesced.clear()

            async def apply() -> None:
                try:
                    await self.quiesced.wait()
                    nodes = await self.backend.activate(attachment)
                    current = self.backend.active_attachment or attachment
                    roles = await asyncio.to_thread(self.backend.inventory.endpoint_roles, current)
                    self.state.update(
                        {
                            **current.as_json(),
                            "main_hid": current.main_hid if current.is_deck else "",
                            "attachment_path": str(current.syspath),
                            "state": "acquiring",
                            "event_nodes": nodes,
                            "endpoint_roles": roles,
                        }
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("Hardware masking failed")
                    self.state["error"] = str(exc)
                    if isinstance(exc, DeviceInUseError):
                        self.state["error_code"] = "device_in_use"
                        self.state["blocking_application"] = exc.application
                    # The independent monitor performs serialized recovery.
                    self.deadline = self.clock()

            self.apply_task = asyncio.create_task(apply(), name="hardware-mask-activation")
            return self.status()
        if command == "quiesced":
            if self.clock() >= self.deadline:
                raise ValueError("The hardware trial expired; access is being restored")
            if self.state.get("state") != "applying" or message.get("token") != self.state.get(
                "token"
            ):
                raise ValueError("This hardware trial has ended")
            self.state["quiesce_required"] = False
            self.quiesced.set()
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
                if self.state.get("automatic") and self.policy:
                    await self.save_policy(self.policy.get("persist", True))
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
            if self.active or self.backend.journal.exists() or self.backend.armed:
                await self._restore("daemon_disconnected", stop_owner=True)

    async def _restore(self, reason: str, *, stop_owner: bool) -> None:
        async with self.recovery_lock:
            had_mask = (
                self.active
                or self.backend.journal.exists()
                or self.backend.armed
                or self.state.get("state") in {"restoring", "recovery_failed"}
            )
            clean = reason in {"lifecycle_stop", "service_stopped"}
            keep_rules = (
                reason in {"hardware_disconnected", "hardware_interfaces_changed"}
                and self.state.get("transport") == "usb"
                and bool(self.policy.get("persist"))
                and self.policy.get("owner_uid") == self.session_uid
                and bool(self.policy.get("usb_selector"))
            )
            interrupted_trial = (
                had_mask and self.state.get("state") != "masked" and not self.state.get("automatic")
            )
            if not keep_rules and (not clean or interrupted_trial):
                await asyncio.to_thread(
                    (self.backend.state_dir / "suspended").write_text, reason + "\n"
                )
            if not had_mask:
                if not clean:
                    self.state.update({"state": "restored", "reason": reason})
                    await asyncio.to_thread(
                        save_json, self.backend.state_dir / "selection.json", self.state
                    )
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
                if keep_rules:
                    await self.backend.recover(keep_rules=True)
                else:
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

    def refresh_interfaces(self, attachment: Attachment) -> None:
        """Quiesce and reacquire a receiver's readers while retaining its kernel drivers."""
        if self.state.get("state") == "masked" or self.state.get("automatic"):
            self.state["automatic"] = True
            self.deadline = self.clock() + TRIAL_SECONDS
        self.state.update(
            {"state": "applying", "quiesce_required": True, "token": uuid.uuid4().hex}
        )
        self.quiesced.clear()

        async def refresh() -> None:
            try:
                await self.quiesced.wait()
                nodes = await self.backend.refresh_interfaces(attachment)
                roles = await asyncio.to_thread(self.backend.inventory.endpoint_roles, attachment)
                self.state.update(
                    {"state": "acquiring", "event_nodes": nodes, "endpoint_roles": roles}
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Masked hardware interface refresh failed")
                self.state["error"] = str(exc)
                self.deadline = self.clock()

        self.apply_task = asyncio.create_task(refresh(), name="hardware-mask-refresh")

    def can_refresh_interfaces(self, attachment: Attachment, roles: list[str]) -> bool:
        previous = cast(list[str], self.state.get("endpoint_roles", []))
        return attachment.transport == "usb" and {
            role for role in previous if role == "usb" or role.endswith(":hidraw")
        } == {role for role in roles if role == "usb" or role.endswith(":hidraw")}

    async def _monitor_once(self) -> None:
        if self.state.get("state") == "recovery_failed":
            # Preserve why recovery began so the daemon can resume after a
            # transient repair failure. A retry must not kill a healthy owner.
            await self._restore(str(self.state.get("reason", "retry")), stop_owner=False)
        elif self.active:
            if self.clock() - self.last_heartbeat > LEASE_SECONDS:
                await self._restore("daemon_unresponsive", stop_owner=True)
                return
            if self.state.get("error"):
                await self._restore("activation_failed", stop_owner=False)
                return
            if self.state.get("state") == "applying":
                if self.clock() >= self.deadline:
                    await self._restore("trial_expired", stop_owner=False)
                return  # USB re-enumeration intentionally removes this generation.
            if self.state.get("state") != "masked" and self.clock() >= self.deadline:
                await self._restore("trial_expired", stop_owner=False)
                return
            try:
                attachment = await asyncio.to_thread(
                    self.backend.inventory.resolve,
                    str(self.state.get("id", "")),
                    str(self.state.get("generation", "")),
                )
            except ValueError:
                await self._restore("hardware_disconnected", stop_owner=False)
                return
            if "endpoint_roles" in self.state:
                roles = await asyncio.to_thread(self.backend.inventory.endpoint_roles, attachment)
                if roles != self.state["endpoint_roles"]:
                    if self.can_refresh_interfaces(attachment, roles):
                        self.refresh_interfaces(attachment)
                    else:
                        await self._restore("hardware_interfaces_changed", stop_owner=False)
                    return
            others = await asyncio.to_thread(
                self.backend.other_steam_controllers, str(self.state.get("main_hid", ""))
            )
            if others:
                await self._restore("controller_scope_changed", stop_owner=False)
            elif self.state.get("state") != "masked" and self.clock() >= self.deadline:
                failed = bool(self.state.get("error"))
                await self._restore(
                    "activation_failed" if failed else "trial_expired", stop_owner=False
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


class MaskSupervisor:
    """Own one daemon lease and an independent transaction for each attachment."""

    def __init__(
        self, backend: LinuxMaskBackend, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.backend = backend
        self.clock = clock
        self.reservations: dict[str, MaskReservation] = {}
        self.session_uid: int | None = None
        self.owner_pidfd: int | None = None
        self.last_heartbeat = clock()
        self.operation_lock = asyncio.Lock()

    async def reservation(self, identity: str) -> MaskReservation:
        if identity not in self.reservations:
            backend = self.backend.for_attachment(identity)
            await asyncio.to_thread(backend.prepare_directories)
            self.reservations[identity] = MaskReservation(backend, self.clock)
            self.reservations[identity].state["id"] = identity
        return self.reservations[identity]

    async def initialize(self) -> None:
        await asyncio.to_thread(self.backend.prepare_directories)
        for identity in await asyncio.to_thread(self.backend.reservation_ids):
            item = await self.reservation(identity)
            await item.load_policy()
            selection = item.backend.state_dir / "selection.json"
            if selection.exists():
                item.state = {
                    **cast(JsonObject, json.loads(await asyncio.to_thread(selection.read_text))),
                    "state": "restored",
                    "event_nodes": [],
                }
            if item.backend.journal.exists():
                await asyncio.to_thread(
                    (item.backend.state_dir / "suspended").write_text, "service_restart\n"
                )
                item.state["reason"] = "service_restart"
            try:
                await item.backend.recover()
            except (OSError, ValueError) as exc:
                item.state = {
                    "id": identity,
                    "state": "recovery_failed",
                    "error": str(exc),
                    "reason": "service_restart",
                }
                continue

    def status(self) -> JsonObject:
        return {
            "masks": [item.status() for item in self.reservations.values()],
            "remapping_suspended": (self.backend.state_dir / "suspended").exists(),
        }

    async def autostart(self) -> None:
        if self.session_uid is None or self.status()["remapping_suspended"]:
            return
        for item in list(self.reservations.values()):
            item.session_uid = self.session_uid
            item.last_heartbeat = self.last_heartbeat
            if item.operation_lock.locked():
                continue
            try:
                async with item.operation_lock:
                    await item.autostart()
            except (OSError, ValueError):
                log.exception("Cannot start saved mask %s", item.policy.get("id"))

    async def request(self, message: JsonObject) -> JsonObject:
        command = message.get("command")
        if command == "startup":
            uid = message.get("uid")
            if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
                raise ValueError("An authenticated user ID is required")
            self.session_uid = uid
        if command in {"startup", "heartbeat"}:
            self.last_heartbeat = self.clock()
            for item in self.reservations.values():
                item.last_heartbeat = self.last_heartbeat
            await self.autostart()
            return self.status()
        if command in {"status", "inventory"}:
            result = self.status()
            if command == "inventory":
                attachments = await asyncio.to_thread(self.backend.inventory.scan)
                devices = [item.as_json() for item in attachments]
                present = {item.identity for item in attachments}
                for identity, item in self.reservations.items():
                    if identity not in present:
                        devices.append(
                            {
                                **item.state,
                                "id": identity,
                                "supported": False,
                                "unsupported_reason": "Not connected",
                            }
                        )
                result["devices"] = devices
            return result
        if command in {"restore", "resume"} and not message.get("id"):
            if command == "restore":
                if message.get("persist") is False:

                    async def unmask(item: MaskReservation) -> None:
                        async with item.operation_lock:
                            item.session_uid = self.session_uid
                            if item.policy.get("owner_uid") == self.session_uid and item.policy:
                                await item.save_policy(False)
                            await item._restore("user_restore", stop_owner=False)

                    results = await asyncio.gather(
                        *(unmask(item) for item in list(self.reservations.values())),
                        return_exceptions=True,
                    )
                    for result in results:
                        if isinstance(result, BaseException):
                            raise OSError(f"Some devices still need recovery: {result}") from result
                    return self.status()
                reason = (
                    "lifecycle_stop"
                    if message.get("reason") == "lifecycle_stop"
                    else "user_restore"
                )
                await self.restore(reason, stop_owner=False)
            else:
                if any(
                    item.active or item.backend.journal.exists()
                    for item in self.reservations.values()
                ):
                    raise ValueError("Hardware recovery is still in progress")
                for item in self.reservations.values():
                    await item.request({"command": "resume"})
                await asyncio.to_thread(
                    (self.backend.state_dir / "suspended").unlink, missing_ok=True
                )
            return self.status()
        identity = str(message.get("id", ""))
        if command == "mask":
            if self.status()["remapping_suspended"]:
                raise ValueError("Resume remapping after administrative recovery before masking")
            # A previously confirmed attachment can be enabled while unplugged.
            # Its saved identity authorizes takeover when it next appears.
            if not message.get("generation") and message.get("persist") is True:
                item = self.reservations.get(identity)
                if (
                    item is None
                    or not item.policy
                    or item.policy.get("owner_uid") != self.session_uid
                ):
                    raise ValueError(
                        "Connect this device before enabling masking for the first time"
                    )
                async with item.operation_lock:
                    if item.active or item.backend.journal.exists():
                        raise ValueError("This attachment already has a mask or pending recovery")
                    item.session_uid = self.session_uid
                    await item.save_policy(True)
                    await item._request({"command": "resume"})
                    await item.autostart()
                return self.status()
            # Resolve before creating any persistent paths from a client request.
            await asyncio.to_thread(
                self.backend.inventory.resolve, identity, str(message.get("generation", ""))
            )
            async with self.operation_lock:
                item = await self.reservation(identity)
        else:
            item = self.reservations.get(identity)
            if item is None:
                raise ValueError("Select an existing hardware mask")
        item.session_uid = self.session_uid
        item.last_heartbeat = self.last_heartbeat
        if command == "restore":
            if message.get("token") != item.state.get("token"):
                raise ValueError("This hardware mask has changed; refresh the hardware list")
            if (
                message.get("reason") == "replacement_unavailable"
                and item.state.get("transport") == "usb"
                and item.state.get("state") in {"trial", "masked", "acquiring"}
            ):
                # A reader can notice radio hotplug before the helper monitor.
                # Reacquire under the existing rules instead of restoring access.
                async with item.operation_lock:
                    try:
                        attachment = await asyncio.to_thread(
                            item.backend.inventory.resolve,
                            identity,
                            str(item.state.get("generation", "")),
                        )
                        roles = await asyncio.to_thread(
                            item.backend.inventory.endpoint_roles, attachment
                        )
                        nodes = await asyncio.to_thread(
                            item.backend.inventory.event_nodes, attachment
                        )
                    except (OSError, ValueError):
                        pass
                    else:
                        if item.can_refresh_interfaces(attachment, roles) and (
                            roles != item.state.get("endpoint_roles")
                            or nodes != item.state.get("event_nodes")
                        ):
                            item.refresh_interfaces(attachment)
                            return self.status()
            if message.get("persist") is False:
                async with item.operation_lock:
                    if item.policy and item.policy.get("owner_uid") == self.session_uid:
                        await item.save_policy(False)
            if item.recovery_task is None or item.recovery_task.done():
                reason = (
                    "replacement_unavailable"
                    if message.get("reason") == "replacement_unavailable"
                    else "user_restore"
                )
                item.state.update({"state": "restoring", "reason": reason})

                async def recover() -> None:
                    try:
                        await item.restore(reason, stop_owner=False)
                    except (OSError, ValueError):
                        log.exception("Hardware recovery will retry for %s", identity)

                item.recovery_task = asyncio.create_task(recover(), name=f"mask-recover-{identity}")
        else:
            await item.request(message)
        return self.status()

    async def restore(self, reason: str, *, stop_owner: bool) -> None:
        if reason not in {
            "lifecycle_stop",
            "service_stopped",
            "daemon_disconnected",
            "daemon_unresponsive",
        }:
            await asyncio.to_thread(
                (self.backend.state_dir / "suspended").write_text, reason + "\n"
            )
        had_mask = any(
            item.active
            or item.backend.journal.exists()
            or item.backend.armed
            or item.state.get("state") in {"restoring", "recovery_failed"}
            for item in self.reservations.values()
        )
        if stop_owner and had_mask and self.owner_pidfd is not None:
            with contextlib.suppress(ProcessLookupError):
                signal.pidfd_send_signal(self.owner_pidfd, signal.SIGKILL)
        results = await asyncio.gather(
            *(item.restore(reason, stop_owner=False) for item in list(self.reservations.values())),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise OSError(f"Some hardware still needs recovery: {result}") from result

    async def owner_disconnected(self) -> None:
        # Closing a clean owner must not replace an explicit per-device stop
        # with an automatically retryable failure.
        if self.owner_pidfd is not None and any(
            item.active or item.backend.journal.exists() or item.backend.armed
            for item in self.reservations.values()
        ):
            with contextlib.suppress(ProcessLookupError):
                signal.pidfd_send_signal(self.owner_pidfd, signal.SIGKILL)
        results = await asyncio.gather(
            *(item.owner_disconnected() for item in list(self.reservations.values())),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                log.error("Disconnected owner's hardware still needs recovery: %s", result)

    async def monitor_once(self) -> None:
        if self.clock() - self.last_heartbeat > LEASE_SECONDS and any(
            item.active or item.backend.armed for item in self.reservations.values()
        ):
            await self.restore("daemon_unresponsive", stop_owner=True)
            return
        results = await asyncio.gather(
            *(
                item.monitor_once()
                for item in list(self.reservations.values())
                if not item.operation_lock.locked()
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                log.error("Hardware recovery will retry: %s", result)

    async def monitor(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            try:
                await self.monitor_once()
            except Exception:
                log.exception("Hardware recovery monitor will retry")
            notify("WATCHDOG=1")

    async def recover_offline(self, *, suspend: bool) -> None:
        await self.initialize()
        await self.restore("admin_restore" if suspend else "service_stopped", stop_owner=False)


async def serve(supervisor: MaskSupervisor) -> None:
    await supervisor.initialize()
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
    server = await asyncio.start_unix_server(handle, str(SOCKET_PATH), limit=16 * 1024 * 1024)
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
            asyncio.run(MaskSupervisor(backend).recover_offline(suspend=args.recover))
        else:
            asyncio.run(serve(MaskSupervisor(backend)))


if __name__ == "__main__":
    main()
