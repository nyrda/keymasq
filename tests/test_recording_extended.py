from pathlib import Path
from unittest.mock import AsyncMock

import evdev
import pytest

from keymasq.keymasqd.recording import RecordingManager
from keymasq.keymasqd.recording_spool import RecordingSpool


def _recorded_events(
    recorder: RecordingManager,
    result: dict[str, object],
) -> list[dict[str, object]]:
    recording_id = str(result.get("pending_recording_id", ""))
    return list(recorder.pending_recording(recording_id).iter_events())


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
    events = _recorded_events(recorder, result)

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
    events = _recorded_events(recorder, result)

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
    assert _recorded_events(recorder, stop_result)[0]["code"] == evdev.ecodes.KEY_A


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
    events = _recorded_events(recorder, result)

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
