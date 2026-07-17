import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import evdev
import pytest

from keymasq.common.ipc import CommandType
from keymasq.keymasqd.recording import RecordingManager
from keymasq.keymasqd.recording_spool import RecordingSnapshot, RecordingSpool


async def _recorded_events(
    recorder: RecordingManager,
    result: dict[str, object],
) -> list[dict[str, object]]:
    recording_id = str(result.get("pending_recording_id", ""))
    snapshot = await recorder.pending_recording(recording_id)
    return list(snapshot.iter_events())


def _add_pending_recording_snapshot(
    recorder: RecordingManager,
    tmp_path: Path,
    *,
    recording_id: str = "recording-1",
    t_us: int = 0,
    event_count: int = 1,
    device_types: list[str] | None = None,
) -> tuple[RecordingSnapshot, Path]:
    path_id = (
        recording_id
        if recording_id.startswith("recording-")
        else f"recording-{recording_id}"
    )
    path = tmp_path / f"{path_id}.jsonl"
    path.write_text(f'{{"t_us":{t_us}}}\n' * event_count, encoding="utf-8")
    snapshot = RecordingSnapshot(
        recording_id=recording_id,
        duration_ms=0,
        device_types=list(device_types) if device_types is not None else ["keyboard"],
        event_count=event_count,
        spool_path=path,
        memory_events=(),
    )
    recorder._pending_recordings[snapshot.recording_id] = snapshot
    return snapshot, path


@pytest.mark.asyncio
async def test_recording_event_filtering_for_mouse_controls():
    recorder = RecordingManager()

    await recorder.start(
        [],
        include_mouse_movement=False,
        include_mouse_clicks=False,
    )

    move = evdev.InputEvent(10, 100, evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 50)
    recorder.record_event("mouse", move)
    recorder.record_event(
        "mouse", evdev.InputEvent(10, 200, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 1)
    )

    recorder.record_event(
        "mouse", evdev.InputEvent(10, 300, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 0)
    )

    keyboard = evdev.InputEvent(10, 400, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)
    recorder.record_event("keyboard", keyboard)

    result = await recorder.stop()
    events = await _recorded_events(recorder, result)

    assert result["event_count"] == 1
    assert events[0]["type"] == evdev.ecodes.EV_KEY


@pytest.mark.asyncio
async def test_recording_keeps_wheel_events_without_mouse_movement_enabled() -> None:
    recorder = RecordingManager()

    await recorder.start(
        [],
        include_mouse_movement=False,
        include_mouse_clicks=False,
    )

    recorder.record_event(
        "mouse",
        evdev.InputEvent(10, 100, evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL, 1),
    )
    rel_wheel_hi_res = getattr(evdev.ecodes, "REL_WHEEL_HI_RES", None)
    if rel_wheel_hi_res is not None:
        recorder.record_event(
            "mouse",
            evdev.InputEvent(10, 200, evdev.ecodes.EV_REL, int(rel_wheel_hi_res), 120),
        )
    recorder.record_event(
        "mouse",
        evdev.InputEvent(10, 300, evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 50),
    )

    result = await recorder.stop()
    events = await _recorded_events(recorder, result)

    assert [event["code"] for event in events] == [
        evdev.ecodes.REL_WHEEL,
        *([int(rel_wheel_hi_res)] if rel_wheel_hi_res is not None else []),
    ]


@pytest.mark.asyncio
async def test_recording_callback_fires_on_start_and_stop(monkeypatch):
    callback = AsyncMock()
    recorder = RecordingManager(broadcast_callback=callback)

    await recorder.start([])
    recorder.record_event(
        "keyboard", evdev.InputEvent(1, 1, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)
    )

    stop_result = await recorder.stop()

    assert callback.await_count == 2
    assert stop_result["event_count"] == 1
    assert callback.await_args_list[1].args[0].value == "recording_stopped"
    assert "events" not in callback.await_args_list[1].args[1]
    assert (await _recorded_events(recorder, stop_result))[0]["code"] == evdev.ecodes.KEY_A


@pytest.mark.asyncio
async def test_recording_start_ignores_invalid_device_path(monkeypatch):
    def _bad_device(_path: str):
        raise OSError("bad path")

    monkeypatch.setattr(evdev, "InputDevice", _bad_device)

    recorder = RecordingManager()
    result = await recorder.start([{"path": "/does/not/exist"}])

    assert result == {"status": "ok"}
    stop_result = await recorder.stop()

    assert stop_result["event_count"] == 0


@pytest.mark.asyncio
async def test_recording_start_logs_expected_device_open_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _bad_device(_path: str):
        raise OSError("bad path")

    monkeypatch.setattr(evdev, "InputDevice", _bad_device)

    recorder = RecordingManager()
    with caplog.at_level(logging.DEBUG, logger="keymasqd.recording"):
        result = await recorder.start([{"path": "/does/not/exist"}])

    assert result == {"status": "ok"}
    assert "Failed to open extra recording device /does/not/exist" in caplog.text
    await recorder.stop()


@pytest.mark.asyncio
async def test_recording_start_logs_unexpected_device_open_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _broken_device(_path: str):
        raise RuntimeError("driver bug")

    monkeypatch.setattr(evdev, "InputDevice", _broken_device)

    recorder = RecordingManager()
    with caplog.at_level(logging.ERROR, logger="keymasqd.recording"):
        result = await recorder.start([{"path": "/dev/input/event99"}])

    assert result == {"status": "ok"}
    assert "Unexpected failure opening extra recording device /dev/input/event99" in caplog.text
    await recorder.stop()


@pytest.mark.asyncio
async def test_recording_stop_skips_callback_when_not_recording():
    callback = AsyncMock()
    recorder = RecordingManager(broadcast_callback=callback)

    await recorder.stop()

    callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_recording_event_filters_sync_and_msc_events() -> None:
    recorder = RecordingManager()

    await recorder.start([])
    recorder.record_event("keyboard", evdev.InputEvent(1, 1, evdev.ecodes.EV_SYN, 0, 0))
    recorder.record_event("keyboard", evdev.InputEvent(1, 1, evdev.ecodes.EV_MSC, 0, 0))

    result = await recorder.stop()

    assert result["event_count"] == 0


@pytest.mark.asyncio
async def test_recording_progress_reports_latest_event() -> None:
    callback = AsyncMock()
    recorder = RecordingManager(broadcast_callback=callback)
    await recorder.start([])
    if recorder._progress_task:
        recorder._progress_task.cancel()
        recorder._progress_task = None
    recorder.record_event(
        "keyboard",
        evdev.InputEvent(1, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
    )
    recorder.record_event(
        "keyboard",
        evdev.InputEvent(1, 2500, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
    )
    callback.reset_mock()
    callback.side_effect = lambda *_args: setattr(recorder, "_stopped", True)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("keymasq.keymasqd.recording.asyncio.sleep", AsyncMock())
        await recorder._monitor_progress()

    callback.assert_awaited_once()
    assert callback.await_args.args[0].value == "recording_progress"
    assert callback.await_args.args[1] == {"event_count": 2, "duration_ms": 2}


@pytest.mark.asyncio
async def test_recording_duration_limit_stops_normally_from_progress_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback = AsyncMock()
    recorder = RecordingManager(broadcast_callback=callback, macro_recording_time_limit=1)
    await recorder.start([])
    original_progress_task = recorder._progress_task
    assert original_progress_task is not None
    original_progress_task.cancel()
    await asyncio.gather(original_progress_task, return_exceptions=True)
    recorder._recording_started_at = asyncio.get_running_loop().time() - 60
    callback.reset_mock()
    monkeypatch.setattr("keymasq.keymasqd.recording.asyncio.sleep", AsyncMock())

    progress_task = asyncio.create_task(recorder._monitor_progress())
    recorder._progress_task = progress_task
    await progress_task

    assert recorder.is_recording is False
    callback.assert_awaited_once()
    event_type, payload = callback.await_args.args
    assert event_type is CommandType.RECORDING_STOPPED
    assert payload["stop_reason"] == "duration_limit"
    assert payload["macro_recording_time_limit"] == 1
    assert isinstance(payload["pending_recording_id"], str)


@pytest.mark.asyncio
async def test_zero_recording_duration_limit_does_not_auto_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback = AsyncMock()
    recorder = RecordingManager(broadcast_callback=callback, macro_recording_time_limit=0)
    await recorder.start([])
    original_progress_task = recorder._progress_task
    assert original_progress_task is not None
    original_progress_task.cancel()
    await asyncio.gather(original_progress_task, return_exceptions=True)
    recorder._progress_task = None
    recorder._recording_started_at = asyncio.get_running_loop().time() - 24 * 60 * 60
    callback.reset_mock()
    callback.side_effect = lambda *_args: setattr(recorder, "_stopped", True)
    monkeypatch.setattr("keymasq.keymasqd.recording.asyncio.sleep", AsyncMock())

    await recorder._monitor_progress()

    callback.assert_awaited_once()
    assert callback.await_args.args[0] is CommandType.RECORDING_PROGRESS
    await recorder.abort()


@pytest.mark.asyncio
async def test_recording_classifies_combo_device_events_per_event_type() -> None:
    recorder = RecordingManager()

    await recorder.start([], include_mouse_clicks=True)
    recorder.record_event(
        "keyboard",
        evdev.InputEvent(1, 1, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
    )
    recorder.record_event(
        "mouse",
        evdev.InputEvent(1, 2, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 1),
    )

    result = await recorder.stop()
    events = await _recorded_events(recorder, result)

    assert [event["device_type"] for event in events] == ["keyboard", "mouse"]


@pytest.mark.asyncio
async def test_recording_spool_spills_past_memory_event_limit(tmp_path: Path) -> None:
    spool = RecordingSpool(tmp_path, memory_event_limit=2)
    for code in range(3):
        spool.append(
            {
                "device_type": "keyboard",
                "type": evdev.ecodes.EV_KEY,
                "code": code,
                "value": 1,
                "t_us": code * 1000,
            }
        )

    snapshot = await spool.finish()

    assert snapshot.spool_path is not None
    assert snapshot.memory_events == ()
    assert snapshot.event_count == 3
    assert [event["code"] for event in snapshot.iter_events()] == [0, 1, 2]
    snapshot.cleanup()
    assert not snapshot.spool_path.exists()


@pytest.mark.asyncio
async def test_recording_spool_duration_uses_latest_event_time(tmp_path: Path) -> None:
    spool = RecordingSpool(tmp_path)
    spool.append({"device_type": "keyboard", "t_us": 1_000_000})
    spool.append({"device_type": "keyboard", "t_us": 1_000})

    snapshot = await spool.finish()

    assert snapshot.duration_ms == 1000


@pytest.mark.asyncio
async def test_pending_recording_claim_prevents_concurrent_discard(tmp_path: Path) -> None:
    recorder = RecordingManager(spool_dir=tmp_path)
    snapshot, path = _add_pending_recording_snapshot(recorder, tmp_path)

    claimed = await recorder.claim_pending_recording(snapshot.recording_id)
    await recorder.discard_pending_recording(snapshot.recording_id)

    assert claimed is snapshot
    assert path.exists()
    assert list(claimed.iter_events()) == [{"t_us": 0}]
    await recorder.release_pending_recording_claim(snapshot.recording_id, saved=True)
    assert not path.exists()


@pytest.mark.asyncio
async def test_pending_recording_claim_restores_on_failed_save(tmp_path: Path) -> None:
    recorder = RecordingManager(spool_dir=tmp_path)
    snapshot, path = _add_pending_recording_snapshot(recorder, tmp_path)

    claimed = await recorder.claim_pending_recording(snapshot.recording_id)
    await recorder.release_pending_recording_claim(snapshot.recording_id, saved=False)

    assert claimed is snapshot
    assert await recorder.pending_recording(snapshot.recording_id) is snapshot
    assert path.exists()
    await recorder.discard_pending_recording(snapshot.recording_id)
    assert not path.exists()


@pytest.mark.asyncio
async def test_pending_recording_claim_honors_discard_on_failed_save(tmp_path: Path) -> None:
    recorder = RecordingManager(spool_dir=tmp_path)
    snapshot, path = _add_pending_recording_snapshot(recorder, tmp_path)

    await recorder.claim_pending_recording(snapshot.recording_id)
    await recorder.discard_pending_recording(snapshot.recording_id)
    await recorder.release_pending_recording_claim(snapshot.recording_id, saved=False)

    assert not path.exists()
    with pytest.raises(FileNotFoundError):
        await recorder.pending_recording(snapshot.recording_id)


@pytest.mark.asyncio
async def test_recording_spool_finish_cleans_spool_file_on_fatal_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = RecordingSpool(tmp_path, memory_event_limit=1)

    def fail_write(_chunk: list[dict[str, object]]) -> None:
        path = spool._ensure_spool_path()
        path.write_text('{"t_us":0}\n', encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(spool, "_write_chunk", fail_write)
    spool.append(
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_KEY,
            "code": 1,
            "value": 1,
            "t_us": 0,
        }
    )

    with pytest.raises(RuntimeError):
        await spool.finish()

    assert list(tmp_path.glob("recording-*.jsonl")) == []


@pytest.mark.asyncio
async def test_recording_spool_records_unexpected_flush_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = RecordingSpool(tmp_path, memory_event_limit=1)

    def fail_write(_chunk: list[dict[str, object]]) -> None:
        raise AssertionError("bad recording event")

    monkeypatch.setattr(spool, "_write_chunk", fail_write)
    spool.append({"device_type": "keyboard", "t_us": 0})

    with pytest.raises(RuntimeError, match="Recording spool failed"):
        await spool.finish()

    assert isinstance(spool.failed, AssertionError)


@pytest.mark.asyncio
async def test_recording_manager_discards_expired_pending_recordings(tmp_path: Path) -> None:
    recorder = RecordingManager(spool_dir=tmp_path)
    expired, expired_path = _add_pending_recording_snapshot(
        recorder,
        tmp_path,
        recording_id="expired",
        device_types=[],
    )
    fresh, fresh_path = _add_pending_recording_snapshot(
        recorder,
        tmp_path,
        recording_id="fresh",
        t_us=1,
        device_types=[],
    )
    now = asyncio.get_running_loop().time()
    recorder._pending_recording_created_at = {
        expired.recording_id: now - 10,
        fresh.recording_id: now,
    }

    await recorder.discard_expired_pending_recordings(ttl_s=5)

    assert not expired_path.exists()
    assert fresh_path.exists()
    assert await recorder.pending_recording(fresh.recording_id) is fresh
    fresh.cleanup()


@pytest.mark.asyncio
async def test_expired_pending_recording_claim_survives_pending_cleanup(
    tmp_path: Path,
) -> None:
    recorder = RecordingManager(spool_dir=tmp_path)
    snapshot, path = _add_pending_recording_snapshot(
        recorder,
        tmp_path,
        recording_id="claimed",
        device_types=[],
    )
    now = asyncio.get_running_loop().time()
    recorder._pending_recording_created_at[snapshot.recording_id] = now - 10

    claimed = await recorder.claim_pending_recording(snapshot.recording_id)
    await recorder.discard_expired_pending_recordings(ttl_s=5)

    assert claimed is snapshot
    assert path.exists()
    assert list(claimed.iter_events()) == [{"t_us": 0}]
    assert recorder._claimed_recordings[snapshot.recording_id] is snapshot
    await recorder.release_pending_recording_claim(snapshot.recording_id, saved=True)
    assert not path.exists()


@pytest.mark.asyncio
async def test_recording_manager_discards_expired_claimed_recordings(tmp_path: Path) -> None:
    recorder = RecordingManager(spool_dir=tmp_path)
    snapshot, path = _add_pending_recording_snapshot(
        recorder,
        tmp_path,
        recording_id="claimed",
        device_types=[],
    )
    now = asyncio.get_running_loop().time()
    recorder._pending_recording_created_at[snapshot.recording_id] = now - 10

    await recorder.claim_pending_recording(snapshot.recording_id)
    recorder._claimed_recording_created_at[snapshot.recording_id] = now - 10
    await recorder.discard_expired_pending_recordings(ttl_s=5)

    assert not path.exists()
    assert snapshot.recording_id not in recorder._claimed_recordings
    assert snapshot.recording_id not in recorder._claimed_recording_created_at


def test_recording_manager_startup_spool_cleanup(tmp_path: Path) -> None:
    recorder = RecordingManager(spool_dir=tmp_path)
    stale = tmp_path / "recording-stale.jsonl"
    other = tmp_path / "other.jsonl"
    stale.write_text("{}\n")
    other.write_text("{}\n")

    recorder.cleanup_spool_dir()

    assert not stale.exists()
    assert other.exists()
