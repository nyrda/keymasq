"""Connect physical reservations to the existing remapping runtime."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

from keymasq.common.devices import resolve_stable_path
from keymasq.common.ipc import CommandType
from keymasq.common.masking import MaskPhase, is_recovering
from keymasq.common.types import JsonObject
from keymasq.keymasqd.input_sources.discovery import SOURCE_PREFIX
from keymasq.keymasqd.runtime import device_path_resolver
from keymasq.keymasqd.runtime.grab.release import (
    cancel_pending_hardware_release,
    cancel_pending_interface_release,
    release_interface_unlocked,
)
from keymasq.keymasqd.runtime.grabbed_device.types import InputAccessMode
from keymasq.masking.client import SystemdMaskBackend
from keymasq.masking.coordinator import MaskCoordinator
from keymasq.masking.inventory import HardwareInventory

if TYPE_CHECKING:
    from keymasq.keymasqd.device_manager import DeviceManager

log = logging.getLogger("keymasqd.masking")
RESERVATION_PREFIX = "@masked:"
INITIAL_RETRY_DELAY_S = 2.0
MAX_RETRY_DELAY_S = 60.0
HEALTHY_RETRY_RESET_S = 30.0
AUTOMATIC_RECOVERY_REASONS = {
    "replacement_unavailable",
    "activation_failed",
    "hardware_disconnected",
    "hardware_interfaces_changed",
    "controller_scope_changed",
    "daemon_unresponsive",
    "daemon_disconnected",
    "service_restart",
}
MASK_COMMANDS = {
    CommandType.HARDWARE_INVENTORY: "inventory",
    CommandType.MASK_HARDWARE: "mask",
    CommandType.KEEP_HARDWARE_MASK: "keep",
    CommandType.RESTORE_HARDWARE: "restore",
    CommandType.RESUME_HARDWARE: "resume",
    CommandType.SET_HARDWARE_MASK_PERSISTENCE: "persistence",
}


class MaskRuntime:
    """Physical readers and retry state belonging to one daemon-owned reservation."""

    def __init__(
        self, manager: DeviceManager, identity: str, request: Callable[..., Awaitable[JsonObject]]
    ) -> None:
        self.manager = manager
        self.identity = identity
        self.reservation_id = RESERVATION_PREFIX + identity
        self.request = request
        self.acquisition: asyncio.Task[None] | None = None
        self.update_task: asyncio.Task[None] | None = None
        self.token = ""
        self.attachment_path = ""
        self.raw_only = False
        self.transition_active = False
        self.reapply_after_restore = False
        self.clock = time.monotonic
        self.retry_at: float | None = None
        self.retry_delay = INITIAL_RETRY_DELAY_S
        self.healthy_since: float | None = None

    def owns(self, device: object, reserved_paths: set[str]) -> bool:
        from keymasq.keymasqd.runtime.grabbed_device.device import GrabbedDevice

        grabbed = cast(GrabbedDevice, device)
        if os.path.realpath(grabbed.path) in reserved_paths:
            return True
        if grabbed.path.startswith(SOURCE_PREFIX) and self.attachment_path:
            from keymasq.keymasqd.input_sources.evdev_adapter import NativeInputDevice

            source = grabbed.device
            return isinstance(source, NativeInputDevice) and Path(
                source.binding.endpoint.hid_parent
            ).is_relative_to(self.attachment_path)
        return False

    async def release_runtime(self) -> None:
        update = self.update_task
        if update is not None and update is not asyncio.current_task() and not update.done():
            update.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await update
        task = self.acquisition
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        released: set[str] = set()
        async with self.manager._op_lock:  # pyright: ignore[reportPrivateUsage]
            if (
                self.reservation_id in self.manager.mask_registry.reservation_paths
                or self.transition_active
            ):
                self.reapply_after_restore = True
            paths = {
                os.path.realpath(path)
                for path in self.manager.mask_registry.reservation_paths.get(
                    self.reservation_id, []
                )
            }
            for hardware_id, devices in list(self.manager.grabbed_devices.items()):
                for device in list(devices):
                    if not self.owns(device, paths):
                        continue
                    released.add(os.path.realpath(device.path))
                    cancel_pending_hardware_release(self.manager, hardware_id)
                    cancel_pending_interface_release(self.manager, hardware_id, device.path)
                    await release_interface_unlocked(self.manager, hardware_id, device.path)
            self.manager.mask_registry.release(self.reservation_id, released)
        self.raw_only = False
        self.transition_active = False
        self.manager.mask_registry.blocked_attachments.discard(self.attachment_path)

    async def update(self, status: JsonObject) -> None:
        self.attachment_path = str(status.get("attachment_path", self.attachment_path))
        if status.get("state") == MaskPhase.APPLYING and status.get("quiesce_required"):
            await self.quiesce(status)
        if status.get("state") == MaskPhase.ACQUIRING and status.get("token") != self.token:
            self.token = str(status.get("token", ""))
            self.acquisition = asyncio.create_task(
                self.acquire(status), name=f"masked-input-{self.identity}"
            )
        if (
            status.get("state") in {MaskPhase.TRIAL, MaskPhase.MASKED}
            and not await self.runtime_ready()
        ):
            log.error("Reserved input or output stopped for %s; restoring access", self.identity)
            await self.release_runtime()
            status = await self.request(
                "restore", {"reason": "replacement_unavailable", "token": status.get("token")}
            )
        if (is_recovering(status.get("state")) or status.get("state") == MaskPhase.RESTORED) and (
            self.reservation_id in self.manager.mask_registry.reservation_paths
            or self.transition_active
        ):
            await self.release_runtime()
        if status.get("state") == MaskPhase.RESTORED and self.reapply_after_restore:
            self.reapply_after_restore = False
            if not self.manager.masking_suspended:
                self.manager.broadcast_hardware_mask_ready()
        await self.recover_automatically(status)

    async def quiesce(self, status: JsonObject) -> None:
        """Stop only readers belonging to the attachment before its HID rebind."""
        from keymasq.keymasqd.input_sources.evdev_adapter import NativeInputDevice

        if (
            self.reservation_id in self.manager.mask_registry.reservation_paths
            or self.acquisition is not None
        ):
            await self.release_runtime()
        attachment = Path(str(status["attachment_path"]))
        self.transition_active = True
        self.attachment_path = str(attachment)
        self.manager.mask_registry.blocked_attachments.add(str(attachment))
        async with self.manager._op_lock:  # pyright: ignore[reportPrivateUsage]
            for hardware_id, devices in list(self.manager.grabbed_devices.items()):
                for device in list(devices):
                    if isinstance(device.device, NativeInputDevice):
                        parent = Path(device.device.binding.endpoint.hid_parent)
                    else:
                        parent = (
                            Path("/sys/class/input")
                            / Path(os.path.realpath(device.path)).name
                            / "device"
                        )
                    if not (await asyncio.to_thread(parent.resolve)).is_relative_to(attachment):
                        continue
                    cancel_pending_hardware_release(self.manager, hardware_id)
                    cancel_pending_interface_release(self.manager, hardware_id, device.path)
                    await release_interface_unlocked(self.manager, hardware_id, device.path)
        await self.request("quiesced", {"token": status.get("token")})

    async def acquire(self, status: JsonObject) -> None:
        try:
            nodes = status.get("event_nodes", [])
            if not isinstance(nodes, list):
                raise ValueError("Invalid hardware input inventory")
            paths = [str(path) for path in cast(list[object], nodes)]
            self.transition_active = True
            # The quiesce handshake released the affected readers before
            # rebind. Keep unrelated devices running throughout acquisition.
            self.manager.mask_registry.blocked_attachments.discard(self.attachment_path)
            self.raw_only = not paths
            self.manager.mask_registry.register(self.reservation_id, paths, self.attachment_path)
            # Own every interface under the reservation even before hardware
            # configuration exists. Later profile requests reuse these grabs.
            if paths:
                await self.manager.grab_device(
                    self.reservation_id, paths, {}, force_grab_unmapped=True
                )
            if not await self.runtime_ready():
                raise OSError("The reserved input runtime could not be started")
            await self.request("ready", {"token": status.get("token")})
            self.transition_active = False
            self.reapply_after_restore = False
            self.manager.broadcast_hardware_mask_ready()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Failed to acquire reserved hardware; restoring access")
            await self.release_runtime()
            await self.request(
                "restore", {"reason": "replacement_unavailable", "token": status.get("token")}
            )

    async def recover_automatically(self, status: JsonObject) -> None:
        """Resume after completed failure recovery, without overriding an explicit stop."""
        now = self.clock()
        if not status.get("remapping_suspended"):
            self.retry_at = None
            if status.get("state") == MaskPhase.MASKED:
                if self.healthy_since is None:
                    self.healthy_since = now
                elif now - self.healthy_since >= HEALTHY_RETRY_RESET_S:
                    self.retry_delay = INITIAL_RETRY_DELAY_S
            else:
                self.healthy_since = None
            return
        self.healthy_since = None
        retryable = status.get("reason") in AUTOMATIC_RECOVERY_REASONS or (
            status.get("reason") == "trial_expired" and bool(status.get("automatic"))
        )
        if status.get("state") != MaskPhase.RESTORED or not retryable:
            self.retry_at = None
            return
        if self.retry_at is None:
            self.retry_at = now + self.retry_delay
            self.retry_delay = min(self.retry_delay * 2, MAX_RETRY_DELAY_S)
            self.manager.broadcast_hardware_recovery(retrying=True)
            return
        if now < self.retry_at:
            return
        # Only the authenticated daemon owner can clear the helper's pause.
        # The next heartbeat retries the user's saved mask; the session event
        # reapplies normal routes even when no persistent mask was requested.
        self.retry_at = None
        status = await self.request("resume")
        if not status.get("remapping_suspended"):
            self.manager.broadcast_hardware_mask_ready()

    async def runtime_ready(self) -> bool:
        # Route changes and adoption hold this lock. Do not treat their stopped
        # readers or temporary output teardown as a failed reservation.
        async with self.manager._op_lock:  # pyright: ignore[reportPrivateUsage]
            expected = {
                os.path.realpath(path)
                for path in self.manager.mask_registry.reservation_paths.get(
                    self.reservation_id, []
                )
            }
            if not expected:
                return self.raw_only
            ready: set[str] = set()
            for devices in self.manager.grabbed_devices.values():
                for device in devices:
                    if not self.owns(device, expected):
                        continue
                    if device.path.startswith(SOURCE_PREFIX):
                        expected.add(os.path.realpath(device.path))
                    if (
                        not device.running
                        or device.device is None
                        or device.task is None
                        or device.task.done()
                    ):
                        continue
                    if device.access_mode is not InputAccessMode.OBSERVE:
                        if device.default_output is not None:
                            route = device.default_route
                            if route is None:
                                continue
                            # Removing an output intentionally silences its route.
                            # Keep input reserved for mappings and inspectors;
                            # only a configured output that failed needs recovery.
                            if (
                                device.default_output
                                in self.manager.output_state.virtual_device_specs
                            ):
                                target = route.target(device)
                                if target is None or target.uinput is None:
                                    continue
                        elif device.uinput is None:
                            continue
                    ready.add(os.path.realpath(device.path))
            return expected <= ready


class HardwareMasking:
    """Own daemon lifecycle and polling; connect reservations to input runtimes."""

    def __init__(self, manager: DeviceManager) -> None:
        self.manager = manager
        self.coordinator = MaskCoordinator(SystemdMaskBackend())
        self.initialized = False
        self.lock = asyncio.Lock()
        self.task: asyncio.Task[None] | None = None
        self.state: JsonObject = {"masks": []}
        self.session_uid: int | None = None
        self.needs_startup = True
        self.monitor_stop = asyncio.Event()
        self.last_progress = time.monotonic()
        self.runtimes: dict[str, MaskRuntime] = {}

    def runtime(self, identity: str) -> MaskRuntime:
        if identity not in self.runtimes:

            async def request(command: str, data: JsonObject | None = None) -> JsonObject:
                result = await self.request(command, {**(data or {}), "id": identity})
                return next(
                    (
                        item
                        for item in cast(list[JsonObject], result.get("masks", []))
                        if item.get("id") == identity
                    ),
                    cast(JsonObject, {}),
                )

            self.runtimes[identity] = MaskRuntime(self.manager, identity, request)
        return self.runtimes[identity]

    def start_monitor(self) -> None:
        if self.task is None:
            self.monitor_stop.clear()
            self.last_progress = time.monotonic()
            self.task = asyncio.create_task(self.monitor(), name="hardware-mask-owner")

    async def stop_monitor(self) -> Exception | asyncio.CancelledError | None:
        if self.task is not None:
            self.monitor_stop.set()
            try:
                await self.task
            except (Exception, asyncio.CancelledError) as exc:
                log.exception("Hardware masking monitor failed")
                return exc
            finally:
                self.task = None
        return None

    async def startup(self) -> None:
        if self.session_uid is not None and (
            self.needs_startup or self.coordinator.session_uid != self.session_uid
        ):
            status = await self.request("startup", {"uid": self.session_uid})
            self.needs_startup = False
            self.manager.masking_suspended = bool(status.get("remapping_suspended"))

    async def request(self, command: str, data: JsonObject | None = None) -> JsonObject:
        # Finish the serialized coordinator request before cancellation can
        # begin recovery of its in-flight hardware transaction.
        exchange = asyncio.create_task(self._request(command, data))
        try:
            return await asyncio.shield(exchange)
        except asyncio.CancelledError:
            with contextlib.suppress(OSError, ValueError, TimeoutError):
                await exchange
            raise

    async def _request(self, command: str, data: JsonObject | None = None) -> JsonObject:
        async with self.lock:
            if not self.initialized:
                await self.coordinator.initialize()
                self.initialized = True
            result = await self.coordinator.request({**(data or {}), "command": command})
            self.state = result
            return result

    async def handle(self, command: CommandType, data: JsonObject, *, uid: int) -> JsonObject:
        if self.session_uid is not None and self.session_uid != uid:
            await self.close()
        self.session_uid = uid
        operation = MASK_COMMANDS[command]
        if operation == "restore":
            identity = str(data.get("id", ""))
            if identity:
                status = await self.request("status")
                current = next(
                    (
                        item
                        for item in cast(list[JsonObject], status.get("masks", []))
                        if item.get("id") == identity
                    ),
                    cast(JsonObject, {}),
                )
                if not current or data.get("token") != current.get("token"):
                    raise ValueError("This hardware mask has changed; refresh the hardware list")
                if identity in self.runtimes:
                    await self.runtimes[identity].release_runtime()
            else:
                await self.release_runtime()
        try:
            await self.startup()
            result = await self.request(operation, data)
        except OSError:
            if operation != "inventory":
                raise
            attachments = await asyncio.to_thread(HardwareInventory().scan)
            self.start_monitor()
            return {
                "devices": [item.as_json() for item in attachments],
                "available": False,
                "message": "Install the hardware operation units to enable masking",
                "remapping_suspended": self.manager.masking_suspended,
            }
        result["available"] = True
        if operation in {"mask", "resume"}:
            self.manager.masking_suspended = bool(result.get("remapping_suspended"))
        self.start_monitor()
        return result

    async def release_runtime(self) -> None:
        for item in list(self.runtimes.values()):
            await item.release_runtime()

    async def monitor_once(self) -> None:
        await self.startup()
        await self.coordinator.monitor_once()
        status = await self.request("poll")
        paused = bool(status.get("remapping_suspended"))
        if paused and not self.manager.masking_suspended:
            self.manager.masking_suspended = True
            await self.release_runtime()
            await self.manager.release_all_devices()
            self.manager.broadcast_hardware_recovery()
        self.manager.masking_suspended = paused
        for item in cast(list[JsonObject], status.get("masks", [])):
            if not item.get("id"):
                continue
            runtime = self.runtime(str(item["id"]))
            task = runtime.update_task
            if task is not None and not task.done():
                continue
            if task is not None and not task.cancelled() and task.exception() is not None:
                log.warning("Mask update failed for %s: %s", runtime.identity, task.exception())
                await runtime.release_runtime()
            runtime.update_task = asyncio.create_task(
                runtime.update(item), name=f"mask-update-{runtime.identity}"
            )

    async def monitor(self) -> None:
        while not self.monitor_stop.is_set():
            try:
                await self.monitor_once()
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, TimeoutError):
                log.warning("Hardware masking operation failed", exc_info=True)
                await self.release_runtime()
            self.last_progress = time.monotonic()
            try:
                await asyncio.wait_for(self.monitor_stop.wait(), 1)
            except TimeoutError:
                pass

    async def recover_if_needed(self) -> bool:
        """Leave an ordinary emergency reset ordinary when no masking state exists."""
        try:
            if (
                not self.manager.mask_registry.has_runtime_state
                and not await self.coordinator.needs_recovery()
            ):
                return False
        except OSError:
            # An unreadable recovery record must not prevent input release.
            log.exception("Cannot inspect masking state; attempting emergency recovery")
        await self.restore()
        return True

    async def restore(self, reason: str = "user_restore") -> None:
        errors: list[Exception] = []

        async def attempt(label: str, cleanup: Callable[[], Awaitable[object]]) -> None:
            try:
                await cleanup()
            except Exception as exc:
                errors.append(exc)
                log.exception("Hardware recovery failed while %s", label)

        global_stop = reason != "lifecycle_stop"
        if global_stop:
            self.manager.masking_suspended = True
            await attempt("neutralizing ordinary input", self.manager.neutralize_runtime)
            await attempt("releasing ordinary input", self.manager.release_all_devices)
        try:
            await attempt("releasing masked readers", self.release_runtime)
            await attempt(
                "restoring physical access", lambda: self.request("restore", {"reason": reason})
            )
        finally:
            if global_stop:
                self.manager.broadcast_hardware_recovery()
        if errors:
            raise errors[0]

    async def suspend(self) -> None:
        monitor_error = await self.stop_monitor()
        try:
            await self.restore("lifecycle_stop")
        finally:
            self.needs_startup = True
        if monitor_error is not None:
            raise monitor_error

    def resume(self) -> None:
        if self.session_uid is not None:
            self.start_monitor()

    async def close(self, *, restore_hardware: bool = True) -> None:
        errors: list[Exception | asyncio.CancelledError] = []

        async def attempt(cleanup: Callable[[], Awaitable[object]]) -> None:
            try:
                await cleanup()
            except Exception as exc:
                errors.append(exc)
                log.exception("Hardware masking cleanup failed")

        try:
            monitor_error = await self.stop_monitor()
            if monitor_error is not None:
                errors.append(monitor_error)
            if self.initialized and restore_hardware:
                await attempt(lambda: self.restore("lifecycle_stop"))
            else:
                for reservation in self.coordinator.reservations.values():
                    task = reservation.apply_task
                    if task is not None:
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await attempt(lambda task=task: task)
                await attempt(self.release_runtime)
        finally:
            self.session_uid = None
            self.coordinator.session_uid = None
            self.needs_startup = True
        if errors:
            raise errors[0]


async def adopt_masked_interfaces(
    manager: DeviceManager,
    hardware_id: str,
    paths: list[str],
    interfaces: list[JsonObject] | None,
    deps: device_path_resolver.DevicePathResolverDeps,
) -> list[JsonObject] | None:
    """Attach configured sources to reserved readers before applying output selection.

    Called under the device manager operation lock. Unconfigured interfaces stay
    in the reservation bucket until a hardware config selects their paths.
    """
    if not manager.mask_registry.hardware_paths or hardware_id.startswith(RESERVATION_PREFIX):
        return interfaces
    descriptors = interfaces or device_path_resolver.interface_descriptors_from_paths(paths)
    resolved = await asyncio.to_thread(
        device_path_resolver.resolve_evdev_interfaces,
        descriptors,
        deps=deps,
        hardware_id=hardware_id,
    )
    requested = {resolve_stable_path(item.path) for item in resolved}
    reserved_devices = [
        device
        for owner, devices in manager.grabbed_devices.items()
        if owner.startswith(RESERVATION_PREFIX)
        for device in devices
    ]
    for device in reserved_devices:
        if resolve_stable_path(device.path) not in requested:
            continue
        if not device_path_resolver.device_matches_hardware_model(device.device, hardware_id):
            continue
        await device.reset_mapping_runtime_state()
        device.release_tracked_outputs()
        _move_reserved_interface(manager, device, hardware_id)
    reserved = manager.mask_registry.hardware_paths.get(hardware_id)
    if not reserved:
        return interfaces
    # Preserve explicit source IDs, including gamepad and motion IDs from setup.
    result = list(descriptors)
    for device in manager.grabbed_devices.get(hardware_id, []):
        if resolve_stable_path(device.path) not in requested:
            result.append({"id": device.interface_id, "path": device.path})
    return result


def _move_reserved_interface(manager: DeviceManager, device: object, hardware_id: str) -> None:
    from keymasq.keymasqd.runtime.grabbed_device.device import GrabbedDevice

    grabbed = cast(GrabbedDevice, device)
    previous = grabbed.hardware_id
    cancel_pending_hardware_release(manager, previous)
    cancel_pending_interface_release(manager, previous, grabbed.path)
    manager.grabbed_devices[previous].remove(grabbed)
    previous_paths = manager.mask_registry.hardware_paths.get(previous, [])
    path_key = resolve_stable_path(grabbed.path)
    moved_paths = [path for path in previous_paths if resolve_stable_path(path) == path_key]
    manager.mask_registry.hardware_paths[previous] = [
        path for path in previous_paths if path not in moved_paths
    ]
    manager.mask_registry.hardware_paths.setdefault(hardware_id, []).extend(
        moved_paths or [grabbed.path]
    )
    manager.grabbed_devices.setdefault(hardware_id, []).append(grabbed)
    grabbed.hardware_id = hardware_id
    grabbed.mapping_getter = lambda: manager.active_mappings.get(grabbed.hardware_id, {})
    # These interfaces remain attached; topology must not reapply an old owner.
    manager.grab_state.desired_grabs.pop(previous, None)
    manager.grab_state.desired_paths.pop(previous, None)
    if not manager.grabbed_devices[previous]:
        manager.grabbed_devices.pop(previous)
        manager.mask_registry.hardware_paths.pop(previous, None)
        manager.active_mappings.pop(previous, None)


async def release_masked_configuration(manager: DeviceManager, hardware_id: str) -> JsonObject:
    """Release logical remapping while retaining the physical reservation."""
    previous_mapping = manager.active_mappings.pop(hardware_id, {})
    for device in list(manager.grabbed_devices.get(hardware_id, [])):
        await device.reset_mapping_runtime_state(previous_mapping=previous_mapping)
        device.release_tracked_outputs()
        device.update_button_map({}, {}, {})
        device.update_analog_inputs({})
        device.update_motion_sensors({})
        await device.update_default_output(None)
        if not hardware_id.startswith(RESERVATION_PREFIX):
            reservation_id = reservation_for_device(manager, device)
            if reservation_id is not None:
                _move_reserved_interface(manager, device, reservation_id)
    return {"released": True, "reserved": True, "hardware_id": hardware_id}


def reservation_for_device(manager: DeviceManager, device: object) -> str | None:
    from keymasq.keymasqd.input_sources.evdev_adapter import NativeInputDevice
    from keymasq.keymasqd.runtime.grabbed_device.device import GrabbedDevice

    grabbed = cast(GrabbedDevice, device)
    key = os.path.realpath(grabbed.path)
    for identity, paths in manager.mask_registry.reservation_paths.items():
        if key in {os.path.realpath(path) for path in paths}:
            return identity
        source = grabbed.device
        attachment = manager.mask_registry.reservation_attachments.get(identity)
        if (
            attachment
            and isinstance(source, NativeInputDevice)
            and Path(source.binding.endpoint.hid_parent).is_relative_to(attachment)
        ):
            return identity
    return None


def input_attachment_path(path: str) -> Path:
    if path.startswith(SOURCE_PREFIX):
        from keymasq.keymasqd.input_sources.discovery import binding_for_path

        return Path(binding_for_path(path).endpoint.hid_parent)
    return (Path("/sys/class/input") / Path(os.path.realpath(path)).name / "device").resolve()
