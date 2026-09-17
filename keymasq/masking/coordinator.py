"""Masking reservations and deadlines owned by the remapping daemon."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import cast

from keymasq.common.masking import (
    HARDWARE_JOB_TIMEOUT,
    MaskPhase,
    SavedMaskLifecycle,
    is_active,
    is_recovering,
)
from keymasq.common.types import JsonObject
from keymasq.masking.backend import DeviceInUseError, MaskOperationError, save_json
from keymasq.masking.inventory import Attachment
from keymasq.masking.protocols import MaskBackend

log = logging.getLogger("keymasq.masking")
TRIAL_SECONDS = 30.0
# One bounded job plus time for quiescing and starting replacement readers.
ACTIVATION_SECONDS = HARDWARE_JOB_TIMEOUT + 15.0
# Automatic starts back off like runtime recovery; presence changes and user
# actions reset the delay so a returning device is handled promptly.
INITIAL_RETRY_DELAY_S = 2.0
MAX_RETRY_DELAY_S = 60.0
# A saved record the helper cannot arm offline. Retrying cannot repair it;
# only a connected activation records a selector the root job may trust.
INCOMPLETE_CODES = frozenset({"selector_missing", "selector_invalid"})


async def load_saved_object(path: Path) -> JsonObject:
    try:
        data = json.loads(await asyncio.to_thread(path.read_text))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return cast(JsonObject, data)


class MaskReservation:
    """One attachment's policy, transition tasks, confirmation deadline, and locks."""

    def __init__(self, backend: MaskBackend, clock: Callable[[], float] = time.monotonic) -> None:
        self.backend = backend
        self.clock = clock
        self.state: JsonObject = {"state": MaskPhase.UNMASKED}
        self.activation_deadline = 0.0
        self.confirmation_deadline: float | None = None
        self.apply_task: asyncio.Task[None] | None = None
        self.recovery_task: asyncio.Task[None] | None = None
        self.recovery_lock = asyncio.Lock()
        self.operation_lock = asyncio.Lock()
        self.policy: JsonObject = {}
        self.session_uid: int | None = None
        self.quiesced = asyncio.Event()
        self.wall_clock: Callable[[], float] = time.time
        # Reconciliation memory: the last observed presence, the earliest next
        # automatic attempt, and why the last attempt was deferred.
        self.present: bool | None = None
        self.next_attempt = 0.0
        self.retry_delay = INITIAL_RETRY_DELAY_S
        self.attention: JsonObject = {}

    async def load_policy(self) -> None:
        policy = await load_saved_object(self.backend.state_dir / "policy.json")
        if policy:
            uid = policy.get("owner_uid")
            seen = policy.get("last_seen")
            if (
                not isinstance(policy.get("id"), str)
                or (self.state.get("id") is not None and policy["id"] != self.state["id"])
                or not isinstance(policy.get("persist"), bool)
                or not isinstance(uid, int)
                or isinstance(uid, bool)
                or uid < 0
                or ("usb_selector" in policy and not isinstance(policy["usb_selector"], dict))
                or (seen is not None and not isinstance(seen, int | float))
                or isinstance(seen, bool)
            ):
                raise ValueError("policy.json contains an invalid masking preference")
        self.policy = policy

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
        seen = self.wall_clock() if self.active or self.present else self.policy.get("last_seen")
        if seen is not None:
            policy["last_seen"] = seen
        await asyncio.to_thread(save_json, self.backend.state_dir / "policy.json", policy)
        self.policy = policy

    async def record_seen(self) -> None:
        """Remember when the saved hardware was last connected, for absent rows."""
        if not self.policy or self.policy.get("owner_uid") != self.session_uid:
            return
        policy: JsonObject = {**self.policy, "last_seen": self.wall_clock()}
        await asyncio.to_thread(save_json, self.backend.state_dir / "policy.json", policy)
        self.policy = policy

    def last_scan_before_attempt(self, last_scan: float, now: float) -> bool:
        return last_scan < self.next_attempt <= now

    def reset_backoff(self) -> None:
        self.next_attempt = 0.0
        self.retry_delay = INITIAL_RETRY_DELAY_S
        if self.attention:
            self.attention = {}
            if not self.active:
                self.state.pop("error", None)

    def defer_automatic_start(self, exc: Exception) -> None:
        """Record why automatic masking stopped and when it may try again."""
        identity = self.policy.get("id", self.state.get("id"))
        code = str(getattr(exc, "code", "") or "automatic_start_failed")
        self.attention = {"code": code, "message": str(exc)}
        self.state["error"] = str(exc)
        if code in INCOMPLETE_CODES:
            # Nothing changes until the hardware is connected and confirmed.
            self.next_attempt = math.inf
            log.warning("Saved mask %s cannot be armed while disconnected: %s", identity, exc)
            return
        self.next_attempt = self.clock() + self.retry_delay
        log.warning(
            "Automatic masking for %s failed; retrying in %.0fs: %s",
            identity,
            self.retry_delay,
            exc,
        )
        self.retry_delay = min(self.retry_delay * 2, MAX_RETRY_DELAY_S)

    async def arm_saved_usb(self, present: bool = False) -> None:
        selector = self.policy.get("usb_selector")
        if not isinstance(selector, dict) or await asyncio.to_thread(
            getattr, self.backend, "armed"
        ):
            return
        if not await asyncio.to_thread(self.backend.saved_selector_available):
            if present:
                # A connected activation records the root selector and installs
                # the same rules itself; there is nothing to arm ahead of it.
                return
            raise MaskOperationError(
                "selector_missing",
                "This saved mask has no root-recorded selector; "
                "connect the device and confirm masking again",
            )
        try:
            attachment = await asyncio.to_thread(self.backend.inventory.from_selector, selector)
        except (KeyError, TypeError, ValueError) as exc:
            raise MaskOperationError(
                "selector_invalid", f"The saved selector cannot be used: {exc}"
            ) from exc
        await self.backend.install_rules(attachment)

    async def autostart(self, attachments: list[Attachment] | None = None) -> None:
        """Converge a saved mask toward its policy given the current hardware."""
        if (
            self.active
            or self.recovery_lock.locked()
            or await asyncio.to_thread(self.backend.journal.exists)
            or not self.policy.get("persist")
            or self.session_uid is None
            or self.policy.get("owner_uid") != self.session_uid
        ):
            return
        suspended = self.backend.state_dir / "suspended"
        if await asyncio.to_thread(suspended.exists):
            reason = (await asyncio.to_thread(suspended.read_text)).strip()
            if reason not in {"lifecycle_stop", "service_stopped"}:
                return
            # A clean shutdown can interrupt automatic reacquisition. The
            # authenticated owner's confirmed policy still authorizes startup.
            await asyncio.to_thread(suspended.unlink, missing_ok=True)
        if attachments is None:
            attachments = await asyncio.to_thread(self.backend.inventory.scan)
        attachment = next(
            (item for item in attachments if item.identity == self.policy["id"]), None
        )
        present = attachment is not None and attachment.supported
        if present != self.present:
            # A presence transition is new information: retry immediately,
            # even after a failure that could not be repaired while absent.
            self.present = present
            self.reset_backoff()
            if present:
                await self.record_seen()
        if self.clock() < self.next_attempt:
            return
        try:
            await self.arm_saved_usb(present)
            if attachment is not None and present:
                await self._request(
                    {
                        "command": "mask",
                        "id": attachment.identity,
                        "generation": attachment.generation,
                    }
                )
                self.state["automatic"] = True
            # Otherwise the hardware and its driver may still be appearing;
            # armed rules already cover its next connection.
        except (ValueError, OSError) as exc:
            self.defer_automatic_start(exc)
            return
        self.reset_backoff()

    @property
    def active(self) -> bool:
        return is_active(self.state.get("state"))

    @property
    def deadline(self) -> float:
        if self.confirmation_deadline is None:
            return self.activation_deadline
        if self.state.get("state") == MaskPhase.TRIAL:
            return self.confirmation_deadline
        # Reacquiring interfaces during a manual trial must not extend the
        # user's existing confirmation window.
        return min(self.activation_deadline, self.confirmation_deadline)

    @property
    def activation_timed_out(self) -> bool:
        return (
            self.state.get("state") in {MaskPhase.APPLYING, MaskPhase.ACQUIRING}
            and self.clock() >= self.activation_deadline
            and (
                self.confirmation_deadline is None
                or self.activation_deadline < self.confirmation_deadline
            )
        )

    def check_deadline(self) -> None:
        if self.clock() >= self.deadline:
            if self.activation_timed_out:
                raise ValueError("Hardware activation timed out; access is being restored")
            raise ValueError("The hardware trial expired; access is being restored")

    async def restore_if_expired(self) -> bool:
        if self.state.get("state") == MaskPhase.MASKED or self.clock() < self.deadline:
            return False
        if self.activation_timed_out:
            self.state["error"] = "Device input did not become ready before activation timed out"
        # Keep the existing terminal expiry reason; error text distinguishes
        # activation failure from a missed confirmation.
        await self._restore("trial_expired")
        return True

    async def status(self) -> JsonObject:
        paused = await asyncio.to_thread((self.backend.state_dir / "suspended").exists)
        return self.status_with_pause(paused)

    def status_with_pause(self, paused: bool) -> JsonObject:
        """Build status on the event loop after reading the filesystem flags."""
        explicitly_stopped = paused and self.state.get("reason") in {
            "user_restore",
            "admin_restore",
            "offline_recovery",
        }
        enabled = self.active or bool(self.policy.get("persist") and not explicitly_stopped)
        retry_in = self.next_attempt - self.clock()
        return {
            **self.state,
            "enabled": enabled,
            "remaining_seconds": max(0, int(self.deadline - self.clock() + 0.999))
            if self.state.get("state") == MaskPhase.TRIAL
            else 0,
            "remapping_suspended": paused,
            "persist": self.policy.get("persist", True),
            "has_saved_mask": bool(self.policy),
            "saved_id": self.policy.get("id"),
            "lifecycle": self.lifecycle(enabled, paused),
            "attention_code": str(self.attention.get("code", "")),
            "next_retry_seconds": int(retry_in + 0.999)
            if self.attention and math.isfinite(retry_in) and retry_in > 0
            else 0,
            "last_seen": self.policy.get("last_seen"),
        }

    def lifecycle(self, enabled: bool, paused: bool) -> SavedMaskLifecycle:
        phase = self.state.get("state")
        if phase == MaskPhase.MASKED:
            return SavedMaskLifecycle.MASKED
        if phase == MaskPhase.TRIAL and not self.state.get("automatic"):
            return SavedMaskLifecycle.TRIAL
        if self.active:
            return SavedMaskLifecycle.ACTIVATING
        if is_recovering(phase):
            return SavedMaskLifecycle.RECOVERING
        if (
            not enabled
            or paused
            or self.session_uid is None
            or self.policy.get("owner_uid") != self.session_uid
        ):
            return SavedMaskLifecycle.OFF
        code = self.attention.get("code")
        if code in INCOMPLETE_CODES:
            return SavedMaskLifecycle.SAVED_INCOMPLETE
        if code:
            return SavedMaskLifecycle.ATTENTION
        if self.present is False:
            return SavedMaskLifecycle.WAITING_FOR_DEVICE
        return SavedMaskLifecycle.STARTING

    async def request(self, message: JsonObject) -> JsonObject:
        async with self.operation_lock:
            return await self._request(message)

    async def _request(self, message: JsonObject) -> JsonObject:
        """Dispatch while operation_lock is held; handlers never acquire it again."""
        command = message.get("command")
        if command == "status":
            return await self.status()
        handlers = {
            "startup": self._startup,
            "poll": self._poll,
            "inventory": self._inventory,
            "mask": self._mask,
            "quiesced": self._quiesced,
            "ready": self._confirm,
            "keep": self._confirm,
            "persistence": self._persistence,
            "restore": self._restore_request,
            "resume": self._resume,
        }
        if not isinstance(command, str) or command not in handlers:
            raise ValueError("Unknown hardware masking operation")
        return await handlers[command](message)

    async def _startup(self, message: JsonObject) -> JsonObject:
        uid = message.get("uid")
        if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
            raise ValueError("An authenticated user ID is required")
        self.session_uid = uid
        await self.autostart()
        return await self.status()

    async def _poll(self, _message: JsonObject) -> JsonObject:
        await self.autostart()
        return await self.status()

    async def _inventory(self, _message: JsonObject) -> JsonObject:
        result = await self.status()
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

    async def _mask(self, message: JsonObject) -> JsonObject:
        if (
            self.active
            or self.recovery_lock.locked()
            or await asyncio.to_thread(self.backend.journal.exists)
        ):
            raise ValueError("This attachment already has a mask or pending recovery")
        attachment = await asyncio.to_thread(
            self.backend.inventory.resolve,
            str(message.get("id", "")),
            str(message.get("generation", "")),
        )
        await asyncio.to_thread(self.backend.inventory.validate, attachment)
        confirmed = (
            message.get("persist") is True
            and self.policy.get("id") == attachment.identity
            and self.policy.get("owner_uid") == self.session_uid
        )
        self.present = True
        self.reset_backoff()
        if confirmed:
            await self.save_policy(True)
        await asyncio.to_thread((self.backend.state_dir / "suspended").unlink, missing_ok=True)
        self.state = {
            **attachment.as_json(),
            "state": MaskPhase.APPLYING,
            "id": attachment.identity,
            "generation": attachment.generation,
            "name": attachment.name,
            "main_hid": attachment.main_hid if attachment.is_deck else "",
            "attachment_path": str(attachment.syspath),
            "quiesce_required": True,
            "token": uuid.uuid4().hex,
        }
        if attachment.transport == "usb":
            self.state["usb_selector"] = self.backend.inventory.selector(attachment)
        if confirmed:
            self.state["automatic"] = True
        self.activation_deadline = self.clock() + ACTIVATION_SECONDS
        self.confirmation_deadline = None
        self.quiesced.clear()

        self.apply_task = asyncio.create_task(
            self._apply(attachment), name="hardware-mask-activation"
        )
        return await self.status()

    async def _apply(self, attachment: Attachment) -> None:
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
                    "state": MaskPhase.ACQUIRING,
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
            # The daemon coordinator performs serialized recovery.
            self.activation_deadline = self.clock()

    async def _quiesced(self, message: JsonObject) -> JsonObject:
        self.check_deadline()
        if self.state.get("state") != MaskPhase.APPLYING or message.get("token") != self.state.get(
            "token"
        ):
            raise ValueError("This hardware trial has ended")
        self.state["quiesce_required"] = False
        self.quiesced.set()
        return await self.status()

    async def _confirm(self, message: JsonObject) -> JsonObject:
        command = message.get("command")
        if message.get("token") != self.state.get("token") or not self.active:
            raise ValueError("This hardware trial has ended")
        self.check_deadline()
        expected = MaskPhase.ACQUIRING if command == "ready" else MaskPhase.TRIAL
        if self.state.get("state") != expected:
            raise ValueError("The replacement controller is not ready for confirmation")
        if command == "keep":
            await self.save_policy(message.get("persist", True))
        if self.state.get("state") != expected:
            raise ValueError("This hardware trial has ended")
        self.state["state"] = (
            MaskPhase.MASKED
            if command == "keep" or self.state.get("automatic")
            else MaskPhase.TRIAL
        )
        if self.state["state"] == MaskPhase.TRIAL and self.confirmation_deadline is None:
            self.confirmation_deadline = self.clock() + TRIAL_SECONDS
        if self.state["state"] == MaskPhase.MASKED:
            if self.state.get("automatic") and self.policy:
                await self.save_policy(self.policy.get("persist", True))
            await asyncio.to_thread(
                save_json, self.backend.state_dir / "selection.json", self.state
            )
        return await self.status()

    async def _persistence(self, message: JsonObject) -> JsonObject:
        if self.active:
            if self.state.get("state") != MaskPhase.MASKED or message.get(
                "token"
            ) != self.state.get("token"):
                raise ValueError(
                    "Confirm the hardware trial before changing its startup preference"
                )
        elif not self.policy or message.get("id") != self.policy.get("id"):
            raise ValueError("No confirmed hardware mask was selected")
        if self.policy and self.policy.get("owner_uid") != self.session_uid:
            raise ValueError("This startup preference belongs to another user")
        await self.save_policy(message.get("persist"))
        return await self.status()

    async def _restore_request(self, message: JsonObject) -> JsonObject:
        reason = "user_restore"
        if message.get("reason") == "lifecycle_stop":
            reason = "lifecycle_stop"
        if message.get("reason") == "replacement_unavailable":
            reason = "replacement_unavailable"
            self.state["error"] = "The replacement controller stopped; restoring hardware access"
        await self._restore(reason)
        return await self.status()

    async def _resume(self, _message: JsonObject) -> JsonObject:
        if (
            self.active
            or self.recovery_lock.locked()
            or await asyncio.to_thread(self.backend.journal.exists)
        ):
            raise ValueError("Restore the current mask before resuming remapping")
        await asyncio.to_thread((self.backend.state_dir / "suspended").unlink, missing_ok=True)
        return await self.status()

    async def restore(self, reason: str) -> None:
        async with self.operation_lock:
            await self._restore(reason)

    async def owner_disconnected(self) -> None:
        async with self.operation_lock:
            # A clean supervisor stop closes the daemon while recovery runs.
            # Recheck after that recovery finishes before classifying the loss.
            if (
                self.active
                or await asyncio.to_thread(self.backend.journal.exists)
                or await asyncio.to_thread(getattr, self.backend, "armed")
            ):
                await self._restore("daemon_disconnected")

    async def _restore(self, reason: str) -> None:
        async with self.recovery_lock:
            if (
                reason in {"lifecycle_stop", "service_stopped"}
                and self.state.get("state") == MaskPhase.RECOVERY_FAILED
            ):
                # Shutdown must not replace the reason for unfinished recovery,
                # especially an explicit user stop.
                reason = str(self.state.get("reason", reason))
            had_mask = (
                self.active
                or await asyncio.to_thread(self.backend.journal.exists)
                or await asyncio.to_thread(getattr, self.backend, "armed")
                or is_recovering(self.state.get("state"))
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
                had_mask
                and self.state.get("state") != MaskPhase.MASKED
                and not self.state.get("automatic")
            )
            if not keep_rules and (not clean or interrupted_trial):
                await asyncio.to_thread(
                    (self.backend.state_dir / "suspended").write_text, reason + "\n"
                )
            if not had_mask:
                if not clean:
                    self.state.update({"state": MaskPhase.RESTORED, "reason": reason})
                    await asyncio.to_thread(
                        save_json, self.backend.state_dir / "selection.json", self.state
                    )
                return
            self.state.update({"state": MaskPhase.RESTORING, "reason": reason})
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
                self.state.update({"state": MaskPhase.RECOVERY_FAILED, "error": str(exc)})
                log.exception("Hardware restoration failed; recovery will retry")
                raise
            suspended = self.backend.state_dir / "suspended"
            if (
                await asyncio.to_thread(suspended.exists)
                and (await asyncio.to_thread(suspended.read_text)).strip()
                == MaskPhase.RECOVERY_FAILED
            ):
                await asyncio.to_thread(suspended.write_text, reason + "\n")
            self.state.update({"state": MaskPhase.RESTORED, "event_nodes": []})
            await asyncio.to_thread(
                save_json, self.backend.state_dir / "selection.json", self.state
            )

    async def monitor_once(self) -> None:
        async with self.operation_lock:
            await self._monitor_once()

    def refresh_interfaces(self, attachment: Attachment) -> None:
        """Quiesce and reacquire a receiver's readers while retaining its kernel drivers."""
        if self.state.get("state") in {MaskPhase.MASKED, MaskPhase.TRIAL}:
            self.activation_deadline = self.clock() + ACTIVATION_SECONDS
        if self.state.get("state") == MaskPhase.MASKED or self.state.get("automatic"):
            self.state["automatic"] = True
            self.confirmation_deadline = None
        self.state.update(
            {"state": MaskPhase.APPLYING, "quiesce_required": True, "token": uuid.uuid4().hex}
        )
        self.quiesced.clear()

        async def refresh() -> None:
            try:
                await self.quiesced.wait()
                nodes = await self.backend.refresh_interfaces(attachment)
                roles = await asyncio.to_thread(self.backend.inventory.endpoint_roles, attachment)
                self.state.update(
                    {"state": MaskPhase.ACQUIRING, "event_nodes": nodes, "endpoint_roles": roles}
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Masked hardware interface refresh failed")
                self.state["error"] = str(exc)
                self.activation_deadline = self.clock()

        self.apply_task = asyncio.create_task(refresh(), name="hardware-mask-refresh")

    def can_refresh_interfaces(self, attachment: Attachment, roles: list[str]) -> bool:
        previous = cast(list[str], self.state.get("endpoint_roles", []))
        return attachment.transport == "usb" and {
            role for role in previous if role == "usb" or role.endswith(":hidraw")
        } == {role for role in roles if role == "usb" or role.endswith(":hidraw")}

    async def _monitor_once(self) -> None:
        if self.state.get("state") == MaskPhase.RECOVERY_FAILED:
            # Preserve why recovery began so the daemon can resume after a
            # transient repair failure. A retry must not kill a healthy owner.
            await self._restore(str(self.state.get("reason", "retry")))
        elif self.active:
            if self.state.get("error"):
                await self._restore("activation_failed")
                return
            if await self.restore_if_expired():
                return
            if self.state.get("state") == MaskPhase.APPLYING:
                return  # USB re-enumeration intentionally removes this generation.
            try:
                attachment = await asyncio.to_thread(
                    self.backend.inventory.resolve,
                    str(self.state.get("id", "")),
                    str(self.state.get("generation", "")),
                )
            except ValueError:
                await self._restore("hardware_disconnected")
                return
            if "endpoint_roles" in self.state:
                roles = await asyncio.to_thread(self.backend.inventory.endpoint_roles, attachment)
                if roles != self.state["endpoint_roles"]:
                    if self.can_refresh_interfaces(attachment, roles):
                        self.refresh_interfaces(attachment)
                    else:
                        await self._restore("hardware_interfaces_changed")
                    return
            others = await asyncio.to_thread(
                self.backend.inventory.other_steam_controllers, str(self.state.get("main_hid", ""))
            )
            if others:
                await self._restore("controller_scope_changed")
            else:
                await self.restore_if_expired()


class MaskCoordinator:
    """Coordinate an independent transaction for each attachment inside keymasqd."""

    def __init__(self, backend: MaskBackend, clock: Callable[[], float] = time.monotonic) -> None:
        self.backend = backend
        self.clock = clock
        self.reservations: dict[str, MaskReservation] = {}
        self.session_uid: int | None = None
        self.operation_lock = asyncio.Lock()
        # With kernel hotplug notifications the owner raises this interval and
        # marks changes instead; without them every heartbeat rescans sysfs.
        self.scan_interval = 0.0
        self.hardware_changed = True
        self.last_scan: float | None = None

    def mark_hardware_changed(self) -> None:
        self.hardware_changed = True

    async def reservation(self, identity: str) -> MaskReservation:
        if identity not in self.reservations:
            backend = self.backend.for_attachment(identity)
            await asyncio.to_thread(backend.prepare_directories)
            self.reservations[identity] = MaskReservation(backend, self.clock)
            self.reservations[identity].state["id"] = identity
        return self.reservations[identity]

    def _has_pending_reservations(self) -> bool:
        return any(
            item.active
            or is_recovering(item.state.get("state"))
            or any(
                task is not None and not task.done()
                for task in (item.apply_task, item.recovery_task)
            )
            for item in list(self.reservations.values())
        )

    async def needs_recovery(self) -> bool:
        if self._has_pending_reservations():
            return True
        backends = {identity: item.backend for identity, item in self.reservations.items()}
        for identity in await asyncio.to_thread(self.backend.reservation_ids):
            if identity not in backends:
                backends[identity] = self.backend.for_attachment(identity)
        for backend in backends.values():
            if await asyncio.to_thread(backend.needs_recovery):
                return True
        # Admission can advance while the filesystem probes yield.
        return self._has_pending_reservations()

    async def initialize(self) -> None:
        await asyncio.to_thread(self.backend.prepare_directories)
        for identity in await asyncio.to_thread(self.backend.reservation_ids):
            item = await self.reservation(identity)
            invalid_saved_state = False
            try:
                await item.load_policy()
                selection = await load_saved_object(item.backend.state_dir / "selection.json")
                if "reason" in selection and not isinstance(selection["reason"], str):
                    raise ValueError("selection.json contains an invalid recovery reason")
                item.state = {
                    **selection,
                    "id": identity,
                    "state": MaskPhase.RESTORED,
                    "event_nodes": [],
                }
            except (OSError, ValueError) as exc:
                log.warning("Invalid saved masking state for %s: %s", identity, exc)
                invalid_saved_state = True
                item.policy = {}
                item.state = {
                    "id": identity,
                    "state": MaskPhase.RESTORED,
                    "error": f"Saved masking state could not be loaded: {exc}",
                }
            suspended = item.backend.state_dir / "suspended"
            pause_reason = (
                (await asyncio.to_thread(suspended.read_text)).strip()
                if await asyncio.to_thread(suspended.exists)
                else ""
            )
            if invalid_saved_state:
                # Corrupt preferences cannot authorize automatic masking. Still
                # recover the physical transaction and initialize other devices.
                pause_reason = "invalid_saved_state"
            if pause_reason == MaskPhase.RECOVERY_FAILED:
                # Failed cleanup used to replace the original pause reason.
                # Once recovery succeeds, use the saved reason again.
                pause_reason = str(item.state.get("reason") or "service_restart")
            if not pause_reason and await asyncio.to_thread(item.backend.journal.exists):
                pause_reason = "service_restart"
                await asyncio.to_thread(suspended.write_text, pause_reason + "\n")
            try:
                await item.backend.recover()
            except (OSError, ValueError) as exc:
                item.state = {
                    "id": identity,
                    "state": MaskPhase.RECOVERY_FAILED,
                    "error": str(exc),
                    "reason": pause_reason or "service_restart",
                }
                continue
            if pause_reason:
                await asyncio.to_thread(suspended.write_text, pause_reason + "\n")
                item.state["reason"] = pause_reason

    async def status(self) -> JsonObject:
        reservations = list(self.reservations.values())
        paths = [self.backend.state_dir / "suspended"] + [
            item.backend.state_dir / "suspended" for item in reservations
        ]
        paused = await asyncio.to_thread(lambda: [path.exists() for path in paths])
        return {
            "masks": [
                item.status_with_pause(flag)
                for item, flag in zip(reservations, paused[1:], strict=True)
            ],
            "remapping_suspended": paused[0],
        }

    def scan_due(self) -> bool:
        if self.hardware_changed or self.last_scan is None:
            return True
        now = self.clock()
        elapsed = now - self.last_scan
        if elapsed < 0 or elapsed >= self.scan_interval:
            return True  # A clock that moved backwards must not stall scans.
        # A deferred automatic start whose delay elapsed since the last scan.
        return any(
            item.attention and item.last_scan_before_attempt(self.last_scan, now)
            for item in self.reservations.values()
        )

    async def autostart(self, *, force: bool = False) -> None:
        if self.session_uid is None or await asyncio.to_thread(
            (self.backend.state_dir / "suspended").exists
        ):
            return
        if not force and not self.scan_due():
            return
        self.hardware_changed = False
        self.last_scan = self.clock()
        attachments = await asyncio.to_thread(self.backend.inventory.scan)
        for item in list(self.reservations.values()):
            item.session_uid = self.session_uid
            if item.operation_lock.locked():
                continue
            try:
                async with item.operation_lock:
                    await item.autostart(attachments)
            except (OSError, ValueError):
                log.exception("Cannot start saved mask %s", item.policy.get("id"))

    async def request(self, message: JsonObject) -> JsonObject:
        command = message.get("command")
        if command == "startup":
            uid = message.get("uid")
            if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
                raise ValueError("An authenticated user ID is required")
            self.session_uid = uid
        if command in {"startup", "poll"}:
            await self.autostart(force=command == "startup")
            return await self.status()
        if command in {"status", "inventory"}:
            result = await self.status()
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
                            await item._restore("user_restore")

                    results = await asyncio.gather(
                        *(unmask(item) for item in list(self.reservations.values())),
                        return_exceptions=True,
                    )
                    for result in results:
                        if isinstance(result, BaseException):
                            raise OSError(f"Some devices still need recovery: {result}") from result
                    return await self.status()
                reason = (
                    "lifecycle_stop"
                    if message.get("reason") == "lifecycle_stop"
                    else "user_restore"
                )
                await self.restore(reason)
            else:
                items = list(self.reservations.values())
                journals = [item.backend.journal for item in items]
                journaled = await asyncio.to_thread(lambda: any(path.exists() for path in journals))
                if journaled or any(item.active for item in items):
                    raise ValueError("Hardware recovery is still in progress")
                for item in self.reservations.values():
                    await item.request({"command": "resume"})
                await asyncio.to_thread(
                    (self.backend.state_dir / "suspended").unlink, missing_ok=True
                )
            return await self.status()
        identity = str(message.get("id", ""))
        if command == "mask":
            if await asyncio.to_thread((self.backend.state_dir / "suspended").exists):
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
                    if item.active or await asyncio.to_thread(item.backend.journal.exists):
                        raise ValueError("This attachment already has a mask or pending recovery")
                    item.session_uid = self.session_uid
                    await item.save_policy(True)
                    await item._request({"command": "resume"})
                    item.reset_backoff()  # An explicit request retries at once.
                    await item.autostart()
                return await self.status()
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
        if command == "restore":
            if message.get("token") != item.state.get("token"):
                raise ValueError("This hardware mask has changed; refresh the hardware list")
            if (
                message.get("reason") == "replacement_unavailable"
                and item.state.get("transport") == "usb"
                and item.state.get("state")
                in {MaskPhase.TRIAL, MaskPhase.MASKED, MaskPhase.ACQUIRING}
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
                            return await self.status()
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
                item.state.update({"state": MaskPhase.RESTORING, "reason": reason})

                async def recover() -> None:
                    try:
                        await item.restore(reason)
                    except (OSError, ValueError):
                        log.exception("Hardware recovery will retry for %s", identity)

                item.recovery_task = asyncio.create_task(recover(), name=f"mask-recover-{identity}")
        else:
            await item.request(message)
        return await self.status()

    async def restore(self, reason: str) -> None:
        if reason not in {
            "lifecycle_stop",
            "service_stopped",
            "daemon_disconnected",
            "daemon_unresponsive",
        }:
            await asyncio.to_thread(
                (self.backend.state_dir / "suspended").write_text, reason + "\n"
            )
        results = await asyncio.gather(
            *(item.restore(reason) for item in list(self.reservations.values())),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise OSError(f"Some hardware still needs recovery: {result}") from result

    async def owner_disconnected(self) -> None:
        # Closing a clean owner must not replace an explicit per-device stop
        # with an automatically retryable failure.
        results = await asyncio.gather(
            *(item.owner_disconnected() for item in list(self.reservations.values())),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                log.error("Disconnected owner's hardware still needs recovery: %s", result)

    async def monitor_once(self) -> None:
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
