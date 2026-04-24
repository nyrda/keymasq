import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol, cast

import evdev

from keymasq.common.devices import (
    classify_event_device_type,
    high_res_wheel_low_res_code,
    normalize_input_classes,
    resolve_stable_path,
)
from keymasq.common.ipc import CommandType

type RecordingEvent = dict[str, object]
type RecordingPayload = dict[str, object]
type RecordingDevice = dict[str, object]


class _RecordingInputDevice(Protocol):
    def close(self) -> None: ...

    def async_read_loop(self) -> AsyncIterator[evdev.InputEvent]: ...


def _event_time_us(event: RecordingEvent) -> int:
    value = event.get("t_us", 0)
    return value if isinstance(value, int) else 0


class RecordingManager:
    def __init__(
        self,
        broadcast_callback: (
            Callable[[CommandType, RecordingPayload], Awaitable[None]] | None
        ) = None,
    ) -> None:
        self.broadcast_callback = broadcast_callback
        self._events: list[RecordingEvent] = []
        self._start_time_us: int | None = None
        self._extra_devices: list[_RecordingInputDevice] = []
        self._monitoring_tasks: list[asyncio.Task[None]] = []
        self._progress_task: asyncio.Task[None] | None = None
        self._stopped = True
        self._include_mouse_movement = False
        self._include_mouse_clicks = False
        self._record_grabbed_source_keys: set[str] = set()

    @property
    def is_recording(self) -> bool:
        return not self._stopped

    def should_record_grabbed_event(self, device_path: str, _device_types: list[str]) -> bool:
        return _physical_source_key(resolve_stable_path(device_path)) in (
            self._record_grabbed_source_keys
        )

    async def start(
        self,
        devices: list[RecordingDevice],
        include_mouse_movement: bool = False,
        include_mouse_clicks: bool = False,
    ) -> RecordingPayload:
        await self.stop()

        self._events = []
        self._start_time_us = None
        self._extra_devices = []
        self._monitoring_tasks = []
        self._stopped = False
        self._include_mouse_movement = bool(include_mouse_movement)
        self._include_mouse_clicks = bool(include_mouse_clicks)
        extra_devices, self._record_grabbed_source_keys = _build_recording_plan(devices)

        for dev in extra_devices:
            path_value = dev.get("open_path", dev.get("path"))
            if not isinstance(path_value, str) or not path_value:
                continue
            try:
                input_dev = cast(
                    _RecordingInputDevice,
                    await asyncio.to_thread(evdev.InputDevice, path_value),
                )
                self._extra_devices.append(input_dev)
                raw_classes = dev.get("device_types")
                classes = (
                    [str(value) for value in cast(list[object], raw_classes)]
                    if isinstance(raw_classes, list)
                    else None
                )
                primary = dev.get("device_type", "other")
                device_types = normalize_input_classes(
                    classes,
                    primary if isinstance(primary, str) else "other",
                )
                task = asyncio.create_task(self._read_extra_device(input_dev, device_types))
                self._monitoring_tasks.append(task)
            except Exception:
                continue

        self._progress_task = asyncio.create_task(self._monitor_progress())

        if self.broadcast_callback:
            await self.broadcast_callback(CommandType.RECORDING_STARTED, {"status": "ok"})

        return {"status": "ok"}

    def record_event(self, device_type: str, event: evdev.InputEvent) -> None:
        if self._stopped:
            return

        if event.type in (evdev.ecodes.EV_SYN, evdev.ecodes.EV_MSC):
            return

        if device_type == "mouse":
            if (
                event.type == evdev.ecodes.EV_REL
                and not self._include_mouse_movement
                and not _is_wheel_event(event)
            ):
                return
            if event.type == evdev.ecodes.EV_KEY and not self._include_mouse_clicks:
                return

        event_ts_us = int(event.timestamp() * 1_000_000)
        if self._start_time_us is None:
            self._start_time_us = event_ts_us

        t_us = max(0, event_ts_us - self._start_time_us)
        self._events.append(
            {
                "device_type": device_type,
                "type": event.type,
                "code": event.code,
                "value": event.value,
                "t_us": t_us,
            }
        )

    async def _read_extra_device(
        self, device: _RecordingInputDevice, device_types: list[str]
    ) -> None:
        try:
            async for event in device.async_read_loop():
                self.record_event(classify_event_device_type(event, device_types), event)
        except Exception:
            pass

    async def stop(self) -> RecordingPayload:
        was_recording = not self._stopped
        self._stopped = True

        for task in self._monitoring_tasks:
            task.cancel()
        self._monitoring_tasks = []

        if self._progress_task:
            self._progress_task.cancel()
            self._progress_task = None

        for device in self._extra_devices:
            try:
                device.close()
            except Exception:
                pass
        self._extra_devices = []
        self._record_grabbed_source_keys = set()

        duration_ms = int(_event_time_us(self._events[-1]) / 1000) if self._events else 0
        device_types = sorted({str(event.get("device_type", "other")) for event in self._events})

        payload: RecordingPayload = {
            "duration_ms": duration_ms,
            "device_types": device_types,
            "events": list(self._events),
        }

        if was_recording and self.broadcast_callback:
            await self.broadcast_callback(CommandType.RECORDING_STOPPED, payload)

        return {
            "duration_ms": duration_ms,
            "device_types": device_types,
            "event_count": len(self._events),
            "events": list(self._events),
        }

    async def _monitor_progress(self) -> None:
        while not self._stopped:
            await asyncio.sleep(0.5)
            if self._stopped or not self.broadcast_callback:
                continue

            duration_ms = int(_event_time_us(self._events[-1]) / 1000) if self._events else 0
            await self.broadcast_callback(
                CommandType.RECORDING_PROGRESS,
                {
                    "event_count": len(self._events),
                    "duration_ms": duration_ms,
                },
        )


def _is_wheel_event(event: evdev.InputEvent) -> bool:
    if event.type != evdev.ecodes.EV_REL:
        return False
    return event.code in (evdev.ecodes.REL_WHEEL, evdev.ecodes.REL_HWHEEL) or (
        high_res_wheel_low_res_code(int(event.code)) is not None
    )


def _str_value(value: object, default: str = "") -> str:
    return default if value is None else str(value)


def _physical_source_key(stable_path: str) -> str:
    return f"physical:{stable_path}"


def _recording_device_kind(device: RecordingDevice) -> str:
    return _str_value(
        device.get("recording_kind", device.get("kind", "physical")),
        "physical",
    )


def _recording_device_path(device: RecordingDevice) -> str:
    return _str_value(device.get("open_path", device.get("path", "")), "")


def _recording_device_source_key(device: RecordingDevice) -> str:
    source_stable_path = _str_value(device.get("source_stable_path"), "")
    if source_stable_path:
        return _physical_source_key(source_stable_path)

    stable_path = _str_value(device.get("stable_path"), "")
    if stable_path:
        return _physical_source_key(stable_path)

    path = _recording_device_path(device)
    if path:
        return _physical_source_key(resolve_stable_path(path))

    return _str_value(device.get("recording_id"), "")


def _build_recording_plan(
    devices: list[RecordingDevice],
) -> tuple[list[RecordingDevice], set[str]]:
    selected = [device for device in devices if _recording_device_path(device)]
    passthrough_sources = {
        _recording_device_source_key(device)
        for device in selected
        if _recording_device_kind(device) == "keymasq_passthrough"
    }
    passthrough_sources.discard("")

    extra_devices: list[RecordingDevice] = []
    grabbed_source_keys: set[str] = set()

    for device in selected:
        kind = _recording_device_kind(device)
        source_key = _recording_device_source_key(device)
        if kind == "physical" and source_key in passthrough_sources:
            continue
        if kind == "physical" and bool(device.get("grabbed_by_keymasq", False)):
            if source_key:
                grabbed_source_keys.add(source_key)
            continue
        extra_devices.append(device)

    return extra_devices, grabbed_source_keys
