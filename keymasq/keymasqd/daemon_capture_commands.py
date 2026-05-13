import asyncio
import uuid
from typing import Protocol, cast

from keymasq.common.combos import is_combo_pulse_evdev
from keymasq.common.ipc import CommandType
from keymasq.keymasqd.daemon_helpers import (
    JsonObject,
    JsonObjectList,
    float_like,
    int_like,
    json_object_list,
    str_list,
)

MIN_CAPTURE_TIMEOUT_S = 1.0
MAX_CAPTURE_TIMEOUT_S = 15.0


class _GrabbedDeviceRef(Protocol):
    path: str


class _CaptureCommandDeviceManager(Protocol):
    grabbed_devices: dict[str, list[_GrabbedDeviceRef]]

    async def list_devices(self) -> JsonObject: ...

    def begin_combo_capture(
        self, token: str, hardware_ids: set[str], notify_event: asyncio.Event
    ) -> None: ...

    def end_combo_capture(self, token: str) -> None: ...

    def read_combo_capture(self, token: str) -> JsonObject: ...


class _CaptureCommandRecordingManager(Protocol):
    async def start(
        self,
        devices: JsonObjectList,
        include_mouse_movement: bool = False,
        include_mouse_clicks: bool = False,
    ) -> JsonObject: ...

    async def stop(self) -> JsonObject: ...


class _CaptureCommandCaptureManager(Protocol):
    def begin(self, hardware_id: str, evdev_paths: list[str] | None = None) -> JsonObject: ...

    def read(self, token: str) -> JsonObject: ...

    def begin_combo(self, *args: object, **kwargs: object) -> JsonObject: ...

    def authorize_combo_capture(self) -> object: ...

    def register_combo_notifier(
        self, token: str, loop: asyncio.AbstractEventLoop, notify_event: asyncio.Event
    ) -> None: ...

    def read_combo_nowait(self, token: str) -> JsonObject: ...

    def end(self, token: str) -> JsonObject: ...


class _CaptureCommandDaemon(Protocol):
    device_manager: _CaptureCommandDeviceManager
    recording_manager: _CaptureCommandRecordingManager
    capture_manager: _CaptureCommandCaptureManager


CaptureCommandDaemon = _CaptureCommandDaemon


async def handle_capture_command(
    daemon: _CaptureCommandDaemon,
    command_type: CommandType,
    data: JsonObject,
) -> JsonObject | None:
    if command_type == CommandType.START_RECORDING:
        devices = cast(JsonObjectList, data.get("devices", []))
        recording_ids = str_list(data.get("recording_ids", []))
        if recording_ids:
            devices = await resolve_recording_devices(daemon, recording_ids)
        return await daemon.recording_manager.start(
            devices,
            include_mouse_movement=bool(data.get("include_mouse_movement", False)),
            include_mouse_clicks=bool(data.get("include_mouse_clicks", False)),
        )

    if command_type == CommandType.STOP_RECORDING:
        return await daemon.recording_manager.stop()

    if command_type == CommandType.CAPTURE_BEGIN:
        hardware_id = str(data.get("hardware_id", ""))
        evdev_paths = str_list(data.get("evdev_paths", []))
        if evdev_paths:
            return await asyncio.to_thread(daemon.capture_manager.begin, hardware_id, evdev_paths)
        return await asyncio.to_thread(daemon.capture_manager.begin, hardware_id)

    if command_type == CommandType.CAPTURE_READ:
        token = str(data.get("token", ""))
        return await asyncio.to_thread(daemon.capture_manager.read, token)

    if command_type == CommandType.CAPTURE_END:
        token = str(data.get("token", ""))
        return await asyncio.to_thread(daemon.capture_manager.end, token)

    if command_type == CommandType.CAPTURE_COMBO:
        hardware_ids = {
            str(hardware_id).lower()
            for hardware_id in str_list(data.get("hardware_ids", []))
            if str(hardware_id).strip()
        }
        timeout_s = float_like(data.get("timeout_s", 15.0), 15.0)
        return await capture_combo(daemon, hardware_ids, timeout_s)

    return None


async def resolve_recording_devices(
    daemon: _CaptureCommandDaemon,
    recording_ids: list[str],
) -> JsonObjectList:
    wanted = {str(recording_id) for recording_id in recording_ids if str(recording_id)}
    if not wanted:
        return []

    result = await daemon.device_manager.list_devices()
    devices: JsonObjectList = []
    for device in json_object_list(result.get("devices", [])):
        recording_id = str(device.get("recording_id", "") or "")
        if recording_id in wanted:
            devices.append(device)
    return devices


async def capture_combo(
    daemon: _CaptureCommandDaemon,
    hardware_ids: set[str],
    timeout_s: float,
) -> JsonObject:
    if not hardware_ids:
        raise ValueError("capture_combo requires at least one hardware_id")

    token = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    notify_event = asyncio.Event()
    grabbed_paths = {
        device.path
        for hardware_id, devices in daemon.device_manager.grabbed_devices.items()
        if hardware_id.lower() in hardware_ids
        for device in devices
    }
    daemon.device_manager.begin_combo_capture(token, hardware_ids, notify_event)
    try:
        authorization = daemon.capture_manager.authorize_combo_capture()
        capture_result = await asyncio.to_thread(
            daemon.capture_manager.begin_combo,
            token,
            grabbed_paths,
            True,
            hardware_ids,
            authorization,
        )
        daemon.capture_manager.register_combo_notifier(token, loop, notify_event)
        warnings = str_list(capture_result.get("warnings", []))
        capture_timeout_s = min(
            max(MIN_CAPTURE_TIMEOUT_S, float(timeout_s)),
            MAX_CAPTURE_TIMEOUT_S,
        )
        deadline = loop.time() + capture_timeout_s
        pressed: set[str] = set()
        events: list[dict[str, str]] = []

        while loop.time() < deadline:
            event = await read_capture_combo_event(daemon, token, notify_event, deadline)
            if not isinstance(event, dict):
                continue

            evdev_name = str(event.get("evdev", "") or "")
            raw_value = event.get("value")
            value = int_like(raw_value, -1) if raw_value is not None else -1
            is_pulse = is_combo_pulse_evdev(evdev_name)
            if not (
                evdev_name.startswith(("key_", "btn_")) or is_pulse
            ) or value not in {0, 1}:
                continue

            if value == 1:
                event_key = "|".join(
                    [
                        str(event.get("hardware_id", "") or ""),
                        str(event.get("source", "") or ""),
                        evdev_name,
                    ]
                )
                pressed.add(event_key)
                if not any(
                    existing.get("evdev") == evdev_name
                    and existing.get("hardware_id") == str(event.get("hardware_id", "") or "")
                    and existing.get("source") == str(event.get("source", "") or "")
                    for existing in events
                ):
                    events.append(
                        {
                            "evdev": evdev_name,
                            "hardware_id": str(event.get("hardware_id", "") or ""),
                            "source": str(event.get("source", "") or ""),
                        }
                    )
                if is_pulse:
                    return {
                        "events": events,
                        "warnings": warnings,
                    }
                continue

            if not events:
                continue
            event_key = "|".join(
                [
                    str(event.get("hardware_id", "") or ""),
                    str(event.get("source", "") or ""),
                    evdev_name,
                ]
            )
            pressed.discard(event_key)
            if not pressed:
                return {
                    "events": events,
                    "warnings": warnings,
                }

        raise TimeoutError("Combo capture timed out")
    finally:
        daemon.device_manager.end_combo_capture(token)
        await asyncio.to_thread(daemon.capture_manager.end, token)


async def read_capture_combo_event(
    daemon: _CaptureCommandDaemon,
    token: str,
    notify_event: asyncio.Event,
    deadline: float,
) -> JsonObject | None:
    loop = asyncio.get_running_loop()

    while loop.time() < deadline:
        event = drain_capture_combo_event_sources(daemon, token)
        if event is not None:
            return event

        remaining = deadline - loop.time()
        if remaining <= 0:
            return None

        if notify_event.is_set():
            notify_event.clear()
            continue

        try:
            await asyncio.wait_for(notify_event.wait(), timeout=remaining)
        except TimeoutError:
            return None
        notify_event.clear()

    return None


def drain_capture_combo_event_sources(
    daemon: _CaptureCommandDaemon,
    token: str,
) -> JsonObject | None:
    event = daemon.device_manager.read_combo_capture(token).get("event")
    if isinstance(event, dict):
        return cast(JsonObject, event)

    passive_event = daemon.capture_manager.read_combo_nowait(token).get("event")
    if isinstance(passive_event, dict):
        return cast(JsonObject, passive_event)

    return None
