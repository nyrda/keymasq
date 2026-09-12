"""Connect physical reservations to the existing remapping runtime."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import TYPE_CHECKING, cast

from keymasq.common.devices import resolve_stable_path
from keymasq.common.ipc import CommandType
from keymasq.common.types import JsonObject
from keymasq.keymasqd.runtime import device_path_resolver
from keymasq.keymasqd.runtime.grab.release import (
    cancel_pending_hardware_release,
    cancel_pending_interface_release,
)
from keymasq.keymasqd.runtime.grabbed_device.types import InputAccessMode
from keymasq.masking.backend import SOCKET_PATH
from keymasq.masking.inventory import HardwareInventory

if TYPE_CHECKING:
    from keymasq.keymasqd.device_manager import DeviceManager

log = logging.getLogger("keymasqd.masking")
DECK_RESERVATION_ID = "28de:1205@masked"
MASK_COMMANDS = {
    CommandType.HARDWARE_INVENTORY: "inventory",
    CommandType.MASK_HARDWARE: "mask",
    CommandType.KEEP_HARDWARE_MASK: "keep",
    CommandType.RESTORE_HARDWARE: "restore",
    CommandType.RESUME_HARDWARE: "resume",
    CommandType.SET_HARDWARE_MASK_PERSISTENCE: "persistence",
}


class HardwareMasking:
    def __init__(self, manager: DeviceManager) -> None:
        self.manager = manager
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.lock = asyncio.Lock()
        self.task: asyncio.Task[None] | None = None
        self.acquisition: asyncio.Task[None] | None = None
        self.token = ""
        self.state: JsonObject = {"state": "unmasked"}
        self.session_uid: int | None = None
        self.needs_startup = True
        self.monitor_stop = asyncio.Event()

    def start_monitor(self) -> None:
        if self.task is None:
            self.monitor_stop.clear()
            self.task = asyncio.create_task(self.monitor(), name="hardware-mask-owner")

    async def stop_monitor(self) -> None:
        if self.task is not None:
            self.monitor_stop.set()
            await self.task
            self.task = None

    async def startup(self) -> None:
        if self.needs_startup and self.session_uid is not None:
            status = await self.request("startup", {"uid": self.session_uid})
            self.needs_startup = False
            self.manager.masking_suspended = (
                bool(status.get("remapping_suspended")) or status.get("state") == "applying"
            )

    async def request(self, command: str, data: JsonObject | None = None) -> JsonObject:
        # Finish an in-flight exchange before cancellation can begin recovery;
        # otherwise its reply could be mistaken for the subsequent restore reply.
        exchange = asyncio.create_task(self._request(command, data))
        try:
            return await asyncio.shield(exchange)
        except asyncio.CancelledError:
            with contextlib.suppress(OSError, ValueError, TimeoutError):
                await exchange
            raise

    async def _request(self, command: str, data: JsonObject | None = None) -> JsonObject:
        async with self.lock:
            if self.writer is None:
                self.reader, self.writer = await asyncio.open_unix_connection(str(SOCKET_PATH))
            assert self.reader is not None
            assert self.writer is not None
            try:
                self.writer.write(
                    (json.dumps({**(data or {}), "command": command}) + "\n").encode()
                )
                await self.writer.drain()
                line = await asyncio.wait_for(self.reader.readline(), 20)
                if not line:
                    raise ConnectionError("Hardware masking service disconnected")
                result = cast(JsonObject, json.loads(line))
                if result.get("status") != "ok":
                    raise ValueError(str(result.get("message", "Hardware masking failed")))
                self.state = result
                return result
            except (OSError, TimeoutError, json.JSONDecodeError):
                self.writer.close()
                self.writer = None
                self.reader = None
                self.needs_startup = True
                raise

    async def handle(self, command: CommandType, data: JsonObject, *, uid: int) -> JsonObject:
        self.session_uid = uid
        operation = MASK_COMMANDS[command]
        if operation == "restore" and self.state.get("state") in {
            "applying",
            "acquiring",
            "trial",
            "masked",
        }:
            await self.release_runtime()
        try:
            await self.startup()
            result = await self.request(operation, data)
        except OSError:
            if operation != "inventory":
                raise OSError(
                    "Hardware masking service is unavailable; install the updated service"
                ) from None
            attachments = await asyncio.to_thread(HardwareInventory().scan)
            self.start_monitor()
            return {
                "devices": [item.as_json() for item in attachments],
                "available": False,
                "message": "Install the hardware masking service to enable masking",
                "remapping_suspended": self.manager.masking_suspended,
            }
        result["available"] = True
        if operation in {"mask", "resume"}:
            self.manager.masking_suspended = result.get("state") == "applying"
        self.start_monitor()
        return result

    async def release_runtime(self, *, notify: bool = True) -> None:
        self.manager.masking_suspended = True
        self.manager.masked_hardware_paths.clear()
        if self.acquisition is not None and not self.acquisition.done():
            self.acquisition.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.acquisition
        await self.manager.release_all_devices()
        if notify:
            self.manager.broadcast_hardware_recovery()

    async def acquire(self, status: JsonObject) -> None:
        try:
            nodes = status.get("event_nodes", [])
            if not isinstance(nodes, list) or not nodes:
                raise ValueError("No physical controller input returned from takeover")
            paths = [str(path) for path in cast(list[object], nodes)]
            # The rebind invalidates old controller readers, but auxiliary
            # interfaces may still be grabbed by the previous configuration.
            # The ready event reapplies saved hardware routes and profiles.
            self.manager.masking_suspended = True
            await self.manager.release_all_devices()
            self.manager.masking_suspended = False
            self.manager.masked_hardware_paths[DECK_RESERVATION_ID] = paths
            # Own every interface under the reservation even before hardware
            # configuration exists. Later profile requests reuse these grabs.
            await self.manager.grab_device(DECK_RESERVATION_ID, paths, {}, force_grab_unmapped=True)
            if not await self.runtime_ready():
                raise OSError("The reserved input runtime could not be started")
            await self.request("ready", {"token": status.get("token")})
            self.manager.broadcast_hardware_mask_ready()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Failed to acquire reserved hardware; restoring access")
            self.manager.masked_hardware_paths.clear()
            self.manager.masking_suspended = True
            await self.manager.release_all_devices()
            await self.request("restore", {"reason": "replacement_unavailable"})

    async def runtime_ready(self) -> bool:
        # Route changes and adoption hold this lock. Do not treat their stopped
        # readers or temporary output teardown as a failed reservation.
        async with self.manager._op_lock:  # pyright: ignore[reportPrivateUsage]
            expected = {
                os.path.realpath(path)
                for paths in self.manager.masked_hardware_paths.values()
                for path in paths
            }
            if not expected:
                return False
            ready: set[str] = set()
            for hardware_id in self.manager.masked_hardware_paths:
                for device in self.manager.grabbed_devices.get(hardware_id, []):
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
                            target = route.target(device) if route is not None else None
                            if target is None or target.uinput is None:
                                continue
                        elif device.uinput is None:
                            continue
                    ready.add(os.path.realpath(device.path))
            return expected <= ready

    async def monitor(self) -> None:
        while not self.monitor_stop.is_set():
            try:
                await self.startup()
                status = await self.request("heartbeat")
                if status.get("state") == "acquiring" and status.get("token") != self.token:
                    self.token = str(status.get("token", ""))
                    self.acquisition = asyncio.create_task(
                        self.acquire(status), name="masked-input-acquire"
                    )
                if (
                    status.get("state") in {"trial", "masked"}
                    and not self.manager.masking_suspended
                    and not await self.runtime_ready()
                ):
                    log.error("Reserved input or output stopped; restoring physical access")
                    await self.release_runtime()
                    await self.request("restore", {"reason": "replacement_unavailable"})
                if status.get("remapping_suspended") and not self.manager.masking_suspended:
                    await self.release_runtime()
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, TimeoutError):
                log.warning("Hardware masking service unavailable", exc_info=True)
                if self.manager.masked_hardware_paths:
                    await self.release_runtime()
            try:
                await asyncio.wait_for(self.monitor_stop.wait(), 1)
            except TimeoutError:
                pass

    async def restore(self, reason: str = "user_restore") -> None:
        if self.writer is None or self.state.get("state") not in {
            "applying",
            "acquiring",
            "trial",
            "masked",
        }:
            return
        await self.release_runtime(notify=reason != "lifecycle_stop")
        await self.request("restore", {"reason": reason})

    async def suspend(self) -> None:
        await self.stop_monitor()
        await self.restore("lifecycle_stop")
        self.needs_startup = True

    def resume(self) -> None:
        if self.session_uid is not None:
            self.start_monitor()

    async def close(self) -> None:
        await self.stop_monitor()
        if self.writer is not None:
            await self.restore("lifecycle_stop")
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()
            self.writer = None
            self.reader = None
        self.session_uid = None
        self.needs_startup = True


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
    if not manager.masked_hardware_paths or hardware_id == DECK_RESERVATION_ID:
        return interfaces
    descriptors = interfaces or device_path_resolver.interface_descriptors_from_paths(paths)
    resolved = await asyncio.to_thread(
        device_path_resolver.resolve_evdev_interfaces,
        descriptors,
        deps=deps,
        hardware_id=hardware_id,
    )
    requested = {resolve_stable_path(item.path) for item in resolved}
    for device in list(manager.grabbed_devices.get(DECK_RESERVATION_ID, [])):
        if resolve_stable_path(device.path) not in requested:
            continue
        await device.reset_mapping_runtime_state()
        device.release_tracked_outputs()
        _move_reserved_interface(manager, device, hardware_id)
    reserved = manager.masked_hardware_paths.get(hardware_id)
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
    previous_paths = manager.masked_hardware_paths[previous]
    path_key = resolve_stable_path(grabbed.path)
    moved_paths = [path for path in previous_paths if resolve_stable_path(path) == path_key]
    manager.masked_hardware_paths[previous] = [
        path for path in previous_paths if path not in moved_paths
    ]
    manager.masked_hardware_paths.setdefault(hardware_id, []).extend(moved_paths)
    manager.grabbed_devices.setdefault(hardware_id, []).append(grabbed)
    grabbed.hardware_id = hardware_id
    grabbed.mapping_getter = lambda: manager.active_mappings.get(grabbed.hardware_id, {})
    # These interfaces remain attached; topology must not reapply an old owner.
    manager.grab_state.desired_grabs.pop(previous, None)
    manager.grab_state.desired_paths.pop(previous, None)
    if not manager.grabbed_devices[previous]:
        manager.grabbed_devices.pop(previous)
        manager.masked_hardware_paths.pop(previous, None)
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
        if hardware_id != DECK_RESERVATION_ID:
            _move_reserved_interface(manager, device, DECK_RESERVATION_ID)
    return {"released": True, "reserved": True, "hardware_id": hardware_id}
