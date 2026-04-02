from unittest.mock import AsyncMock

import evdev
import pytest

from keyforge.keyforged.recording import RecordingManager


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

    assert result["event_count"] == 1
    assert result["events"][0]["type"] == evdev.ecodes.EV_KEY


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
    assert callback.await_args_list[1].args[1]["events"][0]["code"] == evdev.ecodes.KEY_A


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
    recorder._stopped = False
    recorder._events = [
        {"device_type": "keyboard", "type": 1, "code": 30, "value": 1, "t_us": 2500}
    ]
    callback.side_effect = lambda *_args: setattr(recorder, "_stopped", True)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("keyforge.keyforged.recording.asyncio.sleep", AsyncMock())
        await recorder._monitor_progress()

    callback.assert_awaited_once()
    assert callback.await_args.args[0].value == "recording_progress"
    assert callback.await_args.args[1] == {"event_count": 1, "duration_ms": 2}


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

    assert [event["device_type"] for event in result["events"]] == ["keyboard", "mouse"]
