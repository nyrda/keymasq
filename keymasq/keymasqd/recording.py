import asyncio
import json
import logging
import os
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
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
from keymasq.common.models import normalize_macro_recording_slot
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
        self._recording_slot = 0

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
        recording_slot: int = 0,
    ) -> RecordingPayload:
        await self.discard_expired_pending_recordings()
        previous = await self.stop()
        pending_id = previous.get("pending_recording_id")
        if (
            isinstance(pending_id, str)
            and pending_id
            and not normalize_macro_recording_slot(previous.get("recording_slot"))
        ):
            await self.discard_pending_recording(pending_id)

        self._spool = RecordingSpool(self._spool_dir)
        self._start_time_us = None
        self._extra_devices = []
        self._monitoring_tasks = []
        self._stopped = False
        self._include_mouse_movement = bool(include_mouse_movement)
        self._include_mouse_clicks = bool(include_mouse_clicks)
        self._recording_slot = normalize_macro_recording_slot(recording_slot)
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

        started_payload: RecordingPayload = {"status": "ok"}
        if self._recording_slot:
            started_payload["recording_slot"] = int(self._recording_slot)

        if self.broadcast_callback:
            await self.broadcast_callback(CommandType.RECORDING_STARTED, started_payload)

        return started_payload

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
        recording_slot = int(self._recording_slot)
        self._recording_slot = 0

        spool = self._spool
        if spool is None:
            return {"status": "ok"}

        try:
            snapshot = await spool.finish()
            if recording_slot:
                snapshot = await asyncio.to_thread(
                    self._persist_slot_snapshot,
                    recording_slot,
                    snapshot,
                )
        except Exception:
            await spool.discard()
            raise
        finally:
            self._spool = None

        cleanup_snapshots: list[RecordingSnapshot] = []
        async with self._pending_recording_lock:
            if recording_slot:
                cleanup_snapshots.extend(
                    self._discard_existing_slot_recordings_locked(
                        recording_slot,
                        keep_recording_id=snapshot.recording_id,
                    )
                )
            self._pending_recordings[snapshot.recording_id] = snapshot
            self._pending_recording_created_at[snapshot.recording_id] = (
                asyncio.get_running_loop().time()
            )

        for old_snapshot in cleanup_snapshots:
            old_snapshot.cleanup()

        payload: RecordingPayload = {
            "pending_recording_id": snapshot.recording_id,
            "duration_ms": snapshot.duration_ms,
            "device_types": snapshot.device_types,
            "event_count": snapshot.event_count,
        }
        if recording_slot:
            payload["recording_slot"] = recording_slot

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

    async def list_pending_recordings(self) -> list[RecordingPayload]:
        async with self._pending_recording_lock:
            snapshots = list(self._pending_recordings.values())
        return [
            self._pending_recording_meta(snapshot)
            for snapshot in snapshots
            if normalize_macro_recording_slot(snapshot.recording_slot)
        ]

    def _pending_recording_meta(self, snapshot: RecordingSnapshot) -> RecordingPayload:
        payload: RecordingPayload = {
            "pending_recording_id": snapshot.recording_id,
            "duration_ms": int(snapshot.duration_ms),
            "duration_us": int(snapshot.duration_ms) * 1000,
            "device_types": list(snapshot.device_types),
            "event_count": int(snapshot.event_count),
        }
        if snapshot.recording_slot:
            payload["recording_slot"] = int(snapshot.recording_slot)
        return payload

    def _slot_meta_path(self, slot: int) -> Path:
        return self._spool_dir / f"slot-{int(slot)}.json"

    def _slot_event_path(self, slot: int, recording_id: str) -> Path:
        safe_id = "".join(ch for ch in str(recording_id) if ch.isalnum() or ch in {"-", "_"})
        return self._spool_dir / f"slot-{int(slot)}-{safe_id}.jsonl"

    def _persist_slot_snapshot(
        self,
        recording_slot: int,
        snapshot: RecordingSnapshot,
    ) -> RecordingSnapshot:
        slot = normalize_macro_recording_slot(recording_slot)
        if not slot:
            return snapshot

        self._spool_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self._spool_dir, 0o700)

        event_path = self._slot_event_path(slot, snapshot.recording_id)
        meta_path = self._slot_meta_path(slot)
        fd, raw_event_tmp = tempfile.mkstemp(
            prefix=f".slot-{slot}-",
            suffix=".jsonl.tmp",
            dir=self._spool_dir,
            text=True,
        )
        event_tmp = Path(raw_event_tmp)
        meta_tmp = meta_path.with_name(f".{meta_path.name}.tmp-{os.getpid()}")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for event in snapshot.iter_events():
                    handle.write(json.dumps(event, separators=(",", ":")))
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            event_tmp.chmod(0o600)

            meta: RecordingPayload = {
                "pending_recording_id": snapshot.recording_id,
                "recording_slot": int(slot),
                "duration_ms": int(snapshot.duration_ms),
                "device_types": list(snapshot.device_types),
                "event_count": int(snapshot.event_count),
                "event_file": event_path.name,
                "created_at": int(time.time()),
            }
            with meta_tmp.open("w", encoding="utf-8") as handle:
                json.dump(meta, handle, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            meta_tmp.chmod(0o600)

            os.replace(event_tmp, event_path)
            os.replace(meta_tmp, meta_path)
            self._fsync_spool_dir()
        except Exception:
            event_tmp.unlink(missing_ok=True)
            meta_tmp.unlink(missing_ok=True)
            raise

        snapshot.cleanup()
        return RecordingSnapshot(
            recording_id=snapshot.recording_id,
            duration_ms=int(snapshot.duration_ms),
            device_types=list(snapshot.device_types),
            event_count=int(snapshot.event_count),
            spool_path=event_path,
            memory_events=(),
            recording_slot=int(slot),
            cleanup_paths=(meta_path,),
        )

    async def load_persisted_slot_recordings(self) -> None:
        await asyncio.to_thread(self._load_persisted_slot_recordings)

    def _load_persisted_slot_recordings(self) -> None:
        if not self._spool_dir_exists():
            return

        loaded_event_paths: set[Path] = set()
        try:
            meta_paths = sorted(self._spool_dir.glob("slot-*.json"))
        except OSError as exc:
            log.warning(
                "Unable to inspect recording slot metadata in %s: %s",
                self._spool_dir,
                exc,
            )
            return

        for meta_path in meta_paths:
            try:
                decoded = json.loads(meta_path.read_text(encoding="utf-8"))
                if not isinstance(decoded, dict):
                    raise ValueError("slot metadata must be an object")
                meta = cast(dict[str, object], decoded)
                slot = normalize_macro_recording_slot(meta.get("recording_slot"))
                recording_id = str(meta.get("pending_recording_id", "") or "")
                event_file = str(meta.get("event_file", "") or "")
                if not slot or not recording_id or not event_file:
                    raise ValueError("slot metadata is incomplete")
                event_path = (self._spool_dir / event_file).resolve()
                spool_dir = self._spool_dir.resolve()
                if event_path.parent != spool_dir or not event_path.is_file():
                    raise ValueError("slot event file is unavailable")
                raw_device_types = meta.get("device_types", [])
                device_types = (
                    [
                        str(value)
                        for value in cast(list[object], raw_device_types)
                        if isinstance(value, str)
                    ]
                    if isinstance(raw_device_types, list)
                    else []
                )
                snapshot = RecordingSnapshot(
                    recording_id=recording_id,
                    duration_ms=_int_value(meta.get("duration_ms", 0)),
                    device_types=device_types,
                    event_count=_int_value(meta.get("event_count", 0)),
                    spool_path=event_path,
                    memory_events=(),
                    recording_slot=int(slot),
                    cleanup_paths=(meta_path,),
                )
                self._pending_recordings[recording_id] = snapshot
                self._pending_recording_created_at[recording_id] = time.monotonic()
                loaded_event_paths.add(event_path)
            except Exception as exc:
                log.warning("Ignoring invalid recording slot metadata %s: %s", meta_path, exc)
                try:
                    meta_path.unlink(missing_ok=True)
                except OSError:
                    pass

        try:
            event_paths = list(self._spool_dir.glob("slot-*-*.jsonl"))
        except OSError as exc:
            log.warning(
                "Unable to inspect recording slot event files in %s: %s",
                self._spool_dir,
                exc,
            )
            return

        for path in event_paths:
            try:
                if path.resolve() not in loaded_event_paths:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    def _discard_existing_slot_recordings_locked(
        self,
        slot: int,
        *,
        keep_recording_id: str,
    ) -> list[RecordingSnapshot]:
        normalized_slot = normalize_macro_recording_slot(slot)
        if not normalized_slot:
            return []

        cleanup_snapshots: list[RecordingSnapshot] = []
        for recording_id, snapshot in list(self._pending_recordings.items()):
            if recording_id == keep_recording_id:
                continue
            if normalize_macro_recording_slot(snapshot.recording_slot) != normalized_slot:
                continue
            cleanup_snapshots.append(replace(snapshot, cleanup_paths=()))
            self._pending_recordings.pop(recording_id, None)
            self._pending_recording_created_at.pop(recording_id, None)

        for recording_id, snapshot in self._claimed_recordings.items():
            if recording_id == keep_recording_id:
                continue
            if normalize_macro_recording_slot(snapshot.recording_slot) == normalized_slot:
                self._claimed_recordings[recording_id] = replace(snapshot, cleanup_paths=())
                self._claimed_recording_discard_requested.add(recording_id)

        return cleanup_snapshots

    def _fsync_spool_dir(self) -> None:
        dir_fd: int | None = None
        try:
            dir_fd = os.open(self._spool_dir, os.O_RDONLY)
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            if dir_fd is not None:
                os.close(dir_fd)

    def _spool_dir_exists(self) -> bool:
        try:
            return self._spool_dir.exists()
        except OSError as exc:
            log.warning(
                "Unable to inspect recording spool directory %s: %s",
                self._spool_dir,
                exc,
            )
            return False

    async def claim_pending_recording(self, recording_id: str) -> RecordingSnapshot:
        async with self._pending_recording_lock:
            snapshot = self._pending_recordings.pop(recording_id, None)
            self._pending_recording_created_at.pop(recording_id, None)
            if snapshot is None:
                raise FileNotFoundError("Pending recording not found")
            self._claimed_recordings[recording_id] = snapshot
            self._claimed_recording_created_at[recording_id] = (
                asyncio.get_running_loop().time()
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
                if snapshot is not None and normalize_macro_recording_slot(
                    snapshot.recording_slot
                ):
                    self._pending_recordings[recording_id] = snapshot
                    continue
                if snapshot is not None:
                    snapshots.append(snapshot)
                self._pending_recording_created_at.pop(recording_id, None)
            for recording_id in expired_claimed_ids:
                snapshot = self._claimed_recordings.pop(recording_id, None)
                discard_requested = recording_id in self._claimed_recording_discard_requested
                if (
                    snapshot is not None
                    and normalize_macro_recording_slot(snapshot.recording_slot)
                    and not discard_requested
                ):
                    self._claimed_recordings[recording_id] = snapshot
                    continue
                if snapshot is not None:
                    snapshots.append(snapshot)
                self._claimed_recording_created_at.pop(recording_id, None)
                self._claimed_recording_discard_requested.discard(recording_id)

        for snapshot in snapshots:
            snapshot.cleanup()

    def cleanup_spool_dir(self, *, older_than_s: float | None = None) -> None:
        if not self._spool_dir_exists():
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
            snapshots = [
                snapshot
                for snapshot in self._pending_recordings.values()
                if not normalize_macro_recording_slot(snapshot.recording_slot)
            ]
            self._pending_recordings = {
                recording_id: snapshot
                for recording_id, snapshot in self._pending_recordings.items()
                if normalize_macro_recording_slot(snapshot.recording_slot)
            }
            self._pending_recording_created_at = {
                recording_id: created_at
                for recording_id, created_at in self._pending_recording_created_at.items()
                if recording_id in self._pending_recordings
            }
            self._claimed_recording_discard_requested.update(
                recording_id
                for recording_id, snapshot in self._claimed_recordings.items()
                if not normalize_macro_recording_slot(snapshot.recording_slot)
            )
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


def _int_value(value: object, default: int = 0) -> int:
    try:
        return int(cast(int | float | str, value))
    except (TypeError, ValueError):
        return default


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
