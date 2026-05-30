import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

import evdev

from keymasq.common.devices import (
    classify_event_device_type,
    high_res_wheel_low_res_code,
    normalize_input_classes,
    resolve_stable_path,
)
from keymasq.common.ipc import CommandType
from keymasq.common.paths import STATE_DIR
from keymasq.keymasqd.evdev_clock import set_evdev_clock_monotonic
from keymasq.keymasqd.recording_spool import RecordingSnapshot, RecordingSpool

type RecordingEvent = dict[str, object]
type RecordingPayload = dict[str, object]
type RecordingDevice = dict[str, object]
PENDING_RECORDING_TTL_S = 30 * 60
log = logging.getLogger("keymasqd.recording")


class _RecordingInputDevice(Protocol):
    def close(self) -> None: ...

    def async_read_loop(self) -> AsyncIterator[evdev.InputEvent]: ...


class RecordingManager:
    def __init__(
        self,
        broadcast_callback: (
            Callable[[CommandType, RecordingPayload], Awaitable[None]] | None
        ) = None,
        *,
        spool_dir: Path | None = None,
    ) -> None:
        self.broadcast_callback = broadcast_callback
        self._spool_dir = spool_dir or STATE_DIR / "recording-spool"
        self._spool: RecordingSpool | None = None
        self._pending_recordings: dict[str, RecordingSnapshot] = {}
        self._pending_recording_created_at: dict[str, float] = {}
        self._claimed_recordings: dict[str, RecordingSnapshot] = {}
        self._claimed_recording_created_at: dict[str, float] = {}
        self._claimed_recording_discard_requested: set[str] = set()
        self._pending_recording_lock = asyncio.Lock()
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
        await self.discard_expired_pending_recordings()
        previous = await self.stop()
        pending_id = previous.get("pending_recording_id")
        if isinstance(pending_id, str) and pending_id:
            await self.discard_pending_recording(pending_id)

        self._spool = RecordingSpool(self._spool_dir)
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
                input_dev = await asyncio.to_thread(_open_recording_input_device, path_value)
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
        spool = self._spool
        if spool is None:
            return

        spool.append(
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

        spool = self._spool
        if spool is None:
            return {"status": "ok"}

        try:
            snapshot = await spool.finish()
        except Exception:
            await spool.discard()
            raise
        finally:
            self._spool = None

        async with self._pending_recording_lock:
            self._pending_recordings[snapshot.recording_id] = snapshot
            self._pending_recording_created_at[snapshot.recording_id] = (
                asyncio.get_running_loop().time()
            )

        payload: RecordingPayload = {
            "pending_recording_id": snapshot.recording_id,
            "duration_ms": snapshot.duration_ms,
            "device_types": snapshot.device_types,
            "event_count": snapshot.event_count,
        }

        if was_recording and self.broadcast_callback:
            await self.broadcast_callback(CommandType.RECORDING_STOPPED, payload)

        return {
            "status": "ok",
            **payload,
        }

    async def pending_recording(self, recording_id: str) -> RecordingSnapshot:
        async with self._pending_recording_lock:
            snapshot = self._pending_recordings.get(recording_id)
            if snapshot is None:
                raise FileNotFoundError("Pending recording not found")
            return snapshot

    async def claim_pending_recording(self, recording_id: str) -> RecordingSnapshot:
        async with self._pending_recording_lock:
            snapshot = self._pending_recordings.pop(recording_id, None)
            created_at = self._pending_recording_created_at.pop(recording_id, None)
            if snapshot is None:
                raise FileNotFoundError("Pending recording not found")
            self._claimed_recordings[recording_id] = snapshot
            self._claimed_recording_created_at[recording_id] = (
                created_at
                if created_at is not None
                else asyncio.get_running_loop().time()
            )
            self._claimed_recording_discard_requested.discard(recording_id)
            return snapshot

    async def release_pending_recording_claim(
        self,
        recording_id: str,
        *,
        saved: bool,
    ) -> None:
        cleanup_snapshot: RecordingSnapshot | None = None
        async with self._pending_recording_lock:
            snapshot = self._claimed_recordings.pop(recording_id, None)
            created_at = self._claimed_recording_created_at.pop(recording_id, None)
            discard_requested = recording_id in self._claimed_recording_discard_requested
            self._claimed_recording_discard_requested.discard(recording_id)
            if snapshot is None:
                return
            if saved or discard_requested:
                cleanup_snapshot = snapshot
            else:
                self._pending_recordings[recording_id] = snapshot
                self._pending_recording_created_at[recording_id] = (
                    created_at
                    if created_at is not None
                    else asyncio.get_running_loop().time()
                )

        if cleanup_snapshot is not None:
            cleanup_snapshot.cleanup()

    async def discard_pending_recording(self, recording_id: str) -> None:
        cleanup_snapshot: RecordingSnapshot | None = None
        async with self._pending_recording_lock:
            snapshot = self._pending_recordings.pop(recording_id, None)
            self._pending_recording_created_at.pop(recording_id, None)
            if snapshot is not None:
                cleanup_snapshot = snapshot
            elif recording_id in self._claimed_recordings:
                self._claimed_recording_discard_requested.add(recording_id)

        if cleanup_snapshot is not None:
            cleanup_snapshot.cleanup()

    async def discard_all_pending_recordings(self) -> None:
        snapshots = await self._pop_all_pending_recordings()
        for snapshot in snapshots:
            snapshot.cleanup()

    async def discard_expired_pending_recordings(
        self,
        *,
        ttl_s: float = PENDING_RECORDING_TTL_S,
    ) -> None:
        now = asyncio.get_running_loop().time()
        expired_pending_ids: list[str] = []
        expired_claimed_ids: list[str] = []
        snapshots: list[RecordingSnapshot] = []
        async with self._pending_recording_lock:
            ttl = max(0.0, float(ttl_s))
            for recording_id, created_at in self._pending_recording_created_at.items():
                if now - created_at >= ttl:
                    expired_pending_ids.append(recording_id)
            for recording_id, created_at in self._claimed_recording_created_at.items():
                if now - created_at >= ttl:
                    expired_claimed_ids.append(recording_id)

            for recording_id in expired_pending_ids:
                snapshot = self._pending_recordings.pop(recording_id, None)
                if snapshot is not None:
                    snapshots.append(snapshot)
                self._pending_recording_created_at.pop(recording_id, None)
            for recording_id in expired_claimed_ids:
                snapshot = self._claimed_recordings.pop(recording_id, None)
                if snapshot is not None:
                    snapshots.append(snapshot)
                self._claimed_recording_created_at.pop(recording_id, None)
                self._claimed_recording_discard_requested.discard(recording_id)

        for snapshot in snapshots:
            snapshot.cleanup()

    def cleanup_spool_dir(self, *, older_than_s: float | None = None) -> None:
        if not self._spool_dir.exists():
            return
        cutoff = (
            datetime.now() - timedelta(seconds=max(0.0, float(older_than_s)))
            if older_than_s is not None
            else None
        )
        for path in self._spool_dir.glob("recording-*.jsonl"):
            try:
                if cutoff is not None:
                    mtime = datetime.fromtimestamp(path.stat().st_mtime)
                    if mtime > cutoff:
                        continue
                path.unlink(missing_ok=True)
            except OSError:
                continue

    async def _pop_all_pending_recordings(self) -> list[RecordingSnapshot]:
        async with self._pending_recording_lock:
            snapshots = list(self._pending_recordings.values())
            self._pending_recordings.clear()
            self._pending_recording_created_at.clear()
            self._claimed_recording_discard_requested.update(self._claimed_recordings)
            return snapshots

    async def _monitor_progress(self) -> None:
        while not self._stopped:
            await asyncio.sleep(0.5)
            if self._stopped or not self.broadcast_callback:
                continue

            spool = self._spool
            duration_ms = spool.duration_ms if spool is not None else 0
            event_count = spool.event_count if spool is not None else 0
            await self.broadcast_callback(
                CommandType.RECORDING_PROGRESS,
                {
                    "event_count": event_count,
                    "duration_ms": duration_ms,
                },
        )


def _open_recording_input_device(path: str) -> _RecordingInputDevice:
    device = cast(object, evdev.InputDevice(path))
    set_evdev_clock_monotonic(device, device_path=path, logger=log)
    return cast(_RecordingInputDevice, device)


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
