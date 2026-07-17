import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import evdev
import pytest

from keymasq.common.model.actions import (
    DEFAULT_MACRO_LOOP_STOP_BEHAVIOR,
    MappingAction,
)
from keymasq.common.model.core import ActionType, DeviceType
from keymasq.keymasqd import device_manager, recording
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.recording import RecordingManager
from keymasq.keymasqd.runtime import outputs as global_outputs
from keymasq.keymasqd.runtime.grabbed_device import device as grabbed_device
from keymasq.keymasqd.runtime.grabbed_device.device import GrabbedDevice
from keymasq.keymasqd.runtime.grabbed_device.event import pipeline
from keymasq.keymasqd.runtime.macro import controls, mouse, outputs, scheduler
from keymasq.keymasqd.runtime.macro.state import MacroEventSource, MacroRuntimeDeps
from tests.keymasqd.device_manager_support import grabbed_event_processing_deps
from tests.keymasqd.macro_backend_support import FakeRecorder, play_macro_task_helper


async def _recorded_events(
    recorder: RecordingManager,
    result: dict[str, object],
) -> list[dict[str, object]]:
    recording_id = str(result.get("pending_recording_id", ""))
    snapshot = await recorder.pending_recording(recording_id)
    return list(snapshot.iter_events())


@pytest.mark.asyncio
async def test_recording_manager_uses_relative_timestamps() -> None:
    recorder = RecordingManager(broadcast_callback=AsyncMock())
    await recorder.start([])

    first = evdev.InputEvent(10, 100, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)
    second = evdev.InputEvent(10, 500, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0)

    recorder.record_event("keyboard", first)
    recorder.record_event("keyboard", second)

    result = await recorder.stop()

    assert result["event_count"] == 2
    events = await _recorded_events(recorder, result)
    assert events[0]["t_us"] == 0
    assert events[1]["t_us"] == 400


@pytest.mark.asyncio
async def test_recording_manager_records_start_position_as_initial_natural_move() -> None:
    recorder = RecordingManager(broadcast_callback=AsyncMock())
    await recorder.start([], start_position=(123, 456))

    recorder.record_event(
        "keyboard",
        evdev.InputEvent(10, 100, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
    )

    result = await recorder.stop()
    events = await _recorded_events(recorder, result)

    assert result["event_count"] == 2
    assert result["device_types"] == ["keyboard", "mouse"]
    assert events[0] == {
        "device_type": "macro",
        "type": 0,
        "code": 0,
        "value": 0,
        "t_us": 0,
        "macro_action": "mouse_move_natural_abs",
        "x": 123,
        "y": 456,
        "speed": 100_000.0,
        "jitter": 0.0,
        "curve": "linear",
        "tolerance": 2,
        "max_duration_ms": 3000,
        "stop_on_failure": False,
    }
    assert events[1]["code"] == evdev.ecodes.KEY_A


@pytest.mark.asyncio
async def test_recording_abort_discards_active_spool_without_stop_event(tmp_path: Path) -> None:
    broadcast = AsyncMock()
    recorder = RecordingManager(broadcast_callback=broadcast, spool_dir=tmp_path)
    await recorder.start([])
    broadcast.reset_mock()
    recorder.record_event(
        "keyboard",
        evdev.InputEvent(10, 100, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
    )

    await recorder.abort()
    await recorder.abort()

    assert recorder.is_recording is False
    assert recorder._spool is None
    assert recorder._progress_task is None
    assert recorder._monitoring_tasks == []
    assert list(tmp_path.glob("recording-*.jsonl")) == []
    broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_recording_slot_survives_recording_manager_restart(tmp_path: Path) -> None:
    recorder = RecordingManager(spool_dir=tmp_path)
    await recorder.start([], recording_slot=2)
    recorder.record_event(
        "keyboard",
        evdev.InputEvent(10, 100, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
    )

    result = await recorder.stop()
    recording_id = str(result["pending_recording_id"])
    assert result["recording_slot"] == 2

    restored = RecordingManager(spool_dir=tmp_path)
    await restored.load_persisted_slot_recordings()
    recordings = await restored.list_pending_recordings()
    assert recordings == [
        {
            "pending_recording_id": recording_id,
            "duration_ms": 0,
            "duration_us": 0,
            "device_types": ["keyboard"],
            "event_count": 1,
            "recording_slot": 2,
        }
    ]

    snapshot = await restored.pending_recording(recording_id)
    assert snapshot.recording_slot == 2
    assert list(snapshot.iter_events())[0]["code"] == evdev.ecodes.KEY_A

    await restored.discard_pending_recording(recording_id)
    assert list(tmp_path.glob("slot-*")) == []


@pytest.mark.asyncio
async def test_starting_new_recording_preserves_implicit_slot_stop(
    tmp_path: Path,
) -> None:
    recorder = RecordingManager(spool_dir=tmp_path)
    await recorder.start([], recording_slot=2)
    recorder.record_event(
        "keyboard",
        evdev.InputEvent(10, 100, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
    )

    await recorder.start([], recording_slot=3)
    try:
        recordings = await recorder.list_pending_recordings()
        assert len(recordings) == 1
        assert recordings[0]["recording_slot"] == 2

        snapshot = await recorder.pending_recording(str(recordings[0]["pending_recording_id"]))
        assert snapshot.recording_slot == 2
        assert list(snapshot.iter_events())[0]["code"] == evdev.ecodes.KEY_A
    finally:
        await recorder.stop()


@pytest.mark.asyncio
async def test_recording_slot_overwrite_replaces_pending_snapshot(tmp_path: Path) -> None:
    recorder = RecordingManager(spool_dir=tmp_path)
    await recorder.start([], recording_slot=2)
    recorder.record_event(
        "keyboard",
        evdev.InputEvent(10, 100, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
    )
    first = await recorder.stop()
    first_id = str(first["pending_recording_id"])

    await recorder.start([], recording_slot=2)
    recorder.record_event(
        "keyboard",
        evdev.InputEvent(10, 200, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
    )
    second = await recorder.stop()
    second_id = str(second["pending_recording_id"])

    assert await recorder.list_pending_recordings() == [
        {
            "pending_recording_id": second_id,
            "duration_ms": 0,
            "duration_us": 0,
            "device_types": ["keyboard"],
            "event_count": 1,
            "recording_slot": 2,
        }
    ]
    with pytest.raises(FileNotFoundError):
        await recorder.pending_recording(first_id)

    restored = RecordingManager(spool_dir=tmp_path)
    await restored.load_persisted_slot_recordings()
    snapshot = await restored.pending_recording(second_id)
    assert list(snapshot.iter_events())[0]["code"] == evdev.ecodes.KEY_B


@pytest.mark.asyncio
async def test_recording_slot_overwrite_preserves_new_meta_after_old_claim_release(
    tmp_path: Path,
) -> None:
    recorder = RecordingManager(spool_dir=tmp_path)
    await recorder.start([], recording_slot=2)
    recorder.record_event(
        "keyboard",
        evdev.InputEvent(10, 100, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
    )
    first = await recorder.stop()
    first_id = str(first["pending_recording_id"])
    await recorder.claim_pending_recording(first_id)

    await recorder.start([], recording_slot=2)
    recorder.record_event(
        "keyboard",
        evdev.InputEvent(10, 200, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
    )
    second = await recorder.stop()
    second_id = str(second["pending_recording_id"])
    await recorder.release_pending_recording_claim(first_id, saved=False)

    restored = RecordingManager(spool_dir=tmp_path)
    await restored.load_persisted_slot_recordings()
    recordings = await restored.list_pending_recordings()
    assert recordings[0]["pending_recording_id"] == second_id
    snapshot = await restored.pending_recording(second_id)
    assert list(snapshot.iter_events())[0]["code"] == evdev.ecodes.KEY_B


@pytest.mark.asyncio
async def test_recording_slot_overwrite_expires_abandoned_old_claim(
    tmp_path: Path,
) -> None:
    recorder = RecordingManager(spool_dir=tmp_path)
    await recorder.start([], recording_slot=2)
    recorder.record_event(
        "keyboard",
        evdev.InputEvent(10, 100, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
    )
    first = await recorder.stop()
    first_id = str(first["pending_recording_id"])
    first_snapshot = await recorder.claim_pending_recording(first_id)
    first_path = first_snapshot.spool_path
    assert first_path is not None
    assert first_path.exists()

    await recorder.start([], recording_slot=2)
    recorder.record_event(
        "keyboard",
        evdev.InputEvent(10, 200, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
    )
    second = await recorder.stop()
    second_id = str(second["pending_recording_id"])
    second_snapshot = await recorder.pending_recording(second_id)
    second_path = second_snapshot.spool_path
    assert second_path is not None

    await recorder.discard_expired_pending_recordings(ttl_s=0)

    assert first_id not in recorder._claimed_recordings
    assert first_id not in recorder._claimed_recording_created_at
    assert first_id not in recorder._claimed_recording_discard_requested
    assert not first_path.exists()
    assert second_path.exists()

    restored = RecordingManager(spool_dir=tmp_path)
    await restored.load_persisted_slot_recordings()
    recordings = await restored.list_pending_recordings()
    assert recordings[0]["pending_recording_id"] == second_id
    snapshot = await restored.pending_recording(second_id)
    assert list(snapshot.iter_events())[0]["code"] == evdev.ecodes.KEY_B


@pytest.mark.asyncio
async def test_invalid_recording_slot_is_treated_as_unslotted(tmp_path: Path) -> None:
    recorder = RecordingManager(spool_dir=tmp_path)

    start_result = await recorder.start([], recording_slot=99)
    recorder.record_event(
        "keyboard",
        evdev.InputEvent(10, 100, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
    )
    result = await recorder.stop()

    assert start_result == {"status": "ok"}
    assert "recording_slot" not in result
    assert await recorder.list_pending_recordings() == []
    assert list(tmp_path.glob("slot-*")) == []

    snapshot = await recorder.pending_recording(str(result["pending_recording_id"]))
    assert snapshot.recording_slot == 0


@pytest.mark.asyncio
async def test_incomplete_slot_metadata_scan_preserves_uncertain_event_files(
    tmp_path: Path,
) -> None:
    event_path = tmp_path / "slot-2-recording.jsonl"
    event_path.write_text('{"t_us":0}\n', encoding="utf-8")
    invalid_meta = tmp_path / "slot-2.json"
    invalid_meta.write_text("not-json\n", encoding="utf-8")

    recorder = RecordingManager(spool_dir=tmp_path)
    await recorder.load_persisted_slot_recordings()

    assert not event_path.exists()
    assert not invalid_meta.exists()
    quarantined = list((tmp_path / "quarantine").glob("slot-2.json.invalid-*"))
    assert len(quarantined) == 1
    quarantined_events = list(
        (tmp_path / "quarantine").glob("slot-2-recording.jsonl.uncertain-*")
    )
    assert len(quarantined_events) == 1


@pytest.mark.asyncio
async def test_transient_unreadable_slot_metadata_preserves_event_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_path = tmp_path / "slot-2-recording.jsonl"
    event_path.write_text('{"t_us":0}\n', encoding="utf-8")
    meta_path = tmp_path / "slot-2.json"
    meta_path.write_text('{"recording_slot":2}\n', encoding="utf-8")
    original_read_text = Path.read_text

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == meta_path:
            raise PermissionError("temporarily unreadable")
        return original_read_text(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", read_text)

    recorder = RecordingManager(spool_dir=tmp_path)
    await recorder.load_persisted_slot_recordings()

    assert meta_path.exists()
    assert event_path.exists()


@pytest.mark.asyncio
async def test_transient_metadata_failure_preserves_events_from_mixed_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_event_path = tmp_path / "slot-2-invalid.jsonl"
    invalid_event_path.write_text('{"t_us":0}\n', encoding="utf-8")
    invalid_meta_path = tmp_path / "slot-2.json"
    invalid_meta_path.write_text("not-json\n", encoding="utf-8")
    unreadable_event_path = tmp_path / "slot-3-recording.jsonl"
    unreadable_event_path.write_text('{"t_us":0}\n', encoding="utf-8")
    unreadable_meta_path = tmp_path / "slot-3.json"
    unreadable_meta_path.write_text('{"recording_slot":3}\n', encoding="utf-8")
    original_read_text = Path.read_text

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == unreadable_meta_path:
            raise PermissionError("temporarily unreadable")
        return original_read_text(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", read_text)

    recorder = RecordingManager(spool_dir=tmp_path)
    await recorder.load_persisted_slot_recordings()

    assert not invalid_meta_path.exists()
    assert not invalid_event_path.exists()
    assert unreadable_meta_path.exists()
    assert unreadable_event_path.exists()
    quarantined_events = list(
        (tmp_path / "quarantine").glob("slot-2-invalid.jsonl.uncertain-*")
    )
    assert len(quarantined_events) == 1


@pytest.mark.asyncio
async def test_recording_manager_drops_msc_and_syn_events() -> None:
    recorder = RecordingManager(broadcast_callback=AsyncMock())
    await recorder.start([])

    key_down = evdev.InputEvent(10, 100, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)
    msc_scan = evdev.InputEvent(10, 101, evdev.ecodes.EV_MSC, evdev.ecodes.MSC_SCAN, 458756)
    syn = evdev.InputEvent(10, 102, evdev.ecodes.EV_SYN, evdev.ecodes.SYN_REPORT, 0)
    key_up = evdev.InputEvent(10, 500, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0)

    recorder.record_event("keyboard", key_down)
    recorder.record_event("keyboard", msc_scan)
    recorder.record_event("keyboard", syn)
    recorder.record_event("keyboard", key_up)

    result = await recorder.stop()
    events = await _recorded_events(recorder, result)

    assert result["event_count"] == 2
    assert all(event["type"] == evdev.ecodes.EV_KEY for event in events)


@pytest.mark.asyncio
async def test_recording_ignores_start_stop_mapping_buttons() -> None:
    recorder = FakeRecorder()
    event_callback = AsyncMock()

    start_mapping = {
        "btn_start": MappingAction(action_type=ActionType.START_MACRO_RECORDING),
    }
    gd_start = GrabbedDevice(
        path="/dev/input/event0",
        hardware_id="test",
        button_map={"btn_start": "key_f13"},
        mapping_getter=lambda: start_mapping,
        event_callback=event_callback,
        device_type=DeviceType.KEYBOARD,
        recording_manager=recorder,
    )

    start_event = evdev.InputEvent(1, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1)
    await pipeline.process_event(gd_start, start_event, deps=grabbed_event_processing_deps())
    assert len(recorder.calls) == 0

    normal_mapping = {
        "btn_macro": MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
    }
    keyboard_uinput = MagicMock()
    gd_normal = GrabbedDevice(
        path="/dev/input/event0",
        hardware_id="test",
        button_map={"btn_macro": "key_f14"},
        mapping_getter=lambda: normal_mapping,
        event_callback=event_callback,
        device_type=DeviceType.KEYBOARD,
        keyboard_uinput=keyboard_uinput,
        recording_manager=recorder,
    )

    normal_event = evdev.InputEvent(1, 200, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1)
    await pipeline.process_event(gd_normal, normal_event, deps=grabbed_event_processing_deps())

    assert len(recorder.calls) == 1
    assert recorder.calls[0][0] == "keyboard"
    assert recorder.calls[0][1].code == evdev.ecodes.KEY_F14


class _QuietInputDevice:
    def close(self) -> None:
        pass

    async def async_read_loop(self):
        while True:
            await asyncio.sleep(60)
            yield evdev.InputEvent(1, 1, evdev.ecodes.EV_SYN, 0, 0)


@pytest.mark.asyncio
async def test_recording_manager_sets_monotonic_clock_on_extra_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = RecordingManager()
    fake_device = _QuietInputDevice()
    clock_calls: list[tuple[object, str | None]] = []

    monkeypatch.setattr(recording.evdev, "InputDevice", lambda _path: fake_device)
    monkeypatch.setattr(
        recording,
        "set_evdev_clock_monotonic",
        lambda device, **kwargs: clock_calls.append((device, kwargs.get("device_path"))) or True,
    )

    await recorder.start([{"path": "/dev/input/event10", "device_type": "keyboard"}])
    await recorder.stop()

    assert clock_calls == [(fake_device, "/dev/input/event10")]


def test_grabbed_device_input_sets_monotonic_clock_for_recordable_raw_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_device = object()
    clock_calls: list[tuple[object, str | None]] = []

    monkeypatch.setattr(grabbed_device.evdev, "InputDevice", lambda _path: fake_device)
    monkeypatch.setattr(
        grabbed_device,
        "set_evdev_clock_monotonic",
        lambda device, **kwargs: clock_calls.append((device, kwargs.get("device_path"))) or True,
    )

    assert grabbed_device._device_input("/dev/input/event0") is fake_device
    assert clock_calls == [(fake_device, "/dev/input/event0")]


def _grabbed_recording_device(
    recorder: RecordingManager,
    *,
    stable_path: str = "/dev/input/by-id/raw-kbd",
) -> GrabbedDevice:
    device = GrabbedDevice(
        path="/dev/input/event0",
        hardware_id="test",
        button_map={"btn_macro": "key_f14"},
        mapping_getter=lambda: {
            "btn_macro": MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
        },
        event_callback=AsyncMock(),
        device_type=DeviceType.KEYBOARD,
        keyboard_uinput=MagicMock(),
        recording_manager=recorder,
    )
    device.stable_path = stable_path
    return device


@pytest.mark.asyncio
async def test_recording_manager_records_selected_grabbed_raw_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = RecordingManager()
    monkeypatch.setattr(
        recording,
        "resolve_stable_path",
        lambda path: "/dev/input/by-id/raw-kbd" if path == "/dev/input/event0" else path,
    )

    await recorder.start(
        [
            {
                "path": "/dev/input/event0",
                "stable_path": "/dev/input/by-id/raw-kbd",
                "recording_id": "physical:/dev/input/by-id/raw-kbd",
                "recording_kind": "physical",
                "device_type": "keyboard",
                "device_types": ["keyboard"],
                "grabbed_by_keymasq": True,
            }
        ]
    )

    await pipeline.process_event(
        _grabbed_recording_device(recorder),
        evdev.InputEvent(1, 200, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
        deps=grabbed_event_processing_deps(),
    )
    result = await recorder.stop()

    assert result["event_count"] == 1


@pytest.mark.asyncio
async def test_recording_manager_prefers_selected_passthrough_over_grabbed_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = RecordingManager()
    opened_paths: list[str] = []

    def fake_input_device(path: str):
        opened_paths.append(path)
        return _QuietInputDevice()

    monkeypatch.setattr(recording.evdev, "InputDevice", fake_input_device)
    monkeypatch.setattr(
        recording,
        "resolve_stable_path",
        lambda path: "/dev/input/by-id/raw-kbd" if path == "/dev/input/event0" else path,
    )

    await recorder.start(
        [
            {
                "path": "/dev/input/event0",
                "stable_path": "/dev/input/by-id/raw-kbd",
                "recording_id": "physical:/dev/input/by-id/raw-kbd",
                "recording_kind": "physical",
                "device_type": "keyboard",
                "device_types": ["keyboard"],
                "grabbed_by_keymasq": True,
            },
            {
                "path": "/dev/input/event10",
                "stable_path": "/dev/input/event10",
                "recording_id": "keymasq:passthrough:test:kbd",
                "recording_kind": "keymasq_passthrough",
                "source_stable_path": "/dev/input/by-id/raw-kbd",
                "device_type": "keyboard",
                "device_types": ["keyboard"],
            },
        ]
    )

    await pipeline.process_event(
        _grabbed_recording_device(recorder),
        evdev.InputEvent(1, 200, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
        deps=grabbed_event_processing_deps(),
    )
    result = await recorder.stop()

    assert opened_paths == ["/dev/input/event10"]
    assert result["event_count"] == 0


def test_recording_plan_falls_back_for_empty_primary_device_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(recording, "resolve_stable_path", lambda path: path)

    extra_devices, grabbed_source_keys = recording._build_recording_plan(
        [
            {
                "open_path": "",
                "path": "/dev/input/event0",
                "stable_path": "/dev/input/by-id/raw-kbd",
                "recording_kind": "",
                "kind": "physical",
                "grabbed_by_keymasq": True,
            },
            {
                "open_path": "",
                "path": "/dev/input/event10",
                "recording_kind": "",
                "kind": "keymasq_passthrough",
                "source_stable_path": "/dev/input/by-id/raw-kbd",
            },
        ]
    )

    assert [recording._recording_device_path(device) for device in extra_devices] == [
        "/dev/input/event10"
    ]
    assert grabbed_source_keys == set()


@pytest.mark.asyncio
async def test_recording_manager_keeps_unrelated_grabbed_raw_when_passthrough_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = RecordingManager()
    monkeypatch.setattr(recording.evdev, "InputDevice", lambda _path: _QuietInputDevice())
    monkeypatch.setattr(
        recording,
        "resolve_stable_path",
        lambda path: "/dev/input/by-id/raw-kbd" if path == "/dev/input/event0" else path,
    )

    await recorder.start(
        [
            {
                "path": "/dev/input/event0",
                "stable_path": "/dev/input/by-id/raw-kbd",
                "recording_id": "physical:/dev/input/by-id/raw-kbd",
                "recording_kind": "physical",
                "device_type": "keyboard",
                "device_types": ["keyboard"],
                "grabbed_by_keymasq": True,
            },
            {
                "path": "/dev/input/event10",
                "recording_id": "keymasq:passthrough:other:kbd",
                "recording_kind": "keymasq_passthrough",
                "source_stable_path": "/dev/input/by-id/other-kbd",
                "device_type": "keyboard",
                "device_types": ["keyboard"],
            },
        ]
    )

    await pipeline.process_event(
        _grabbed_recording_device(recorder),
        evdev.InputEvent(1, 200, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
        deps=grabbed_event_processing_deps(),
    )
    result = await recorder.stop()

    assert result["event_count"] == 1


@pytest.mark.asyncio
async def test_recording_manager_snapshot_does_not_skip_action_execution() -> None:
    class _FlakyRecordingGrabbedDevice(GrabbedDevice):
        def __init__(self, *args: object, recorder: FakeRecorder, **kwargs: object) -> None:
            super().__init__(*args, recording_manager=recorder, **kwargs)
            self._recording_manager_read_count = 0

        def __getattribute__(self, name: str) -> object:
            if name == "recording_manager":
                read_count = object.__getattribute__(self, "_recording_manager_read_count")
                object.__setattr__(
                    self,
                    "_recording_manager_read_count",
                    int(read_count) + 1,
                )
                if int(read_count) >= 1:
                    return None
            return super().__getattribute__(name)

    recorder = FakeRecorder()
    event_callback = AsyncMock()
    keyboard_uinput = MagicMock()
    mapping = {
        "btn_macro": MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
    }
    grabbed = _FlakyRecordingGrabbedDevice(
        path="/dev/input/event0",
        hardware_id="test",
        button_map={"btn_macro": "key_f14"},
        mapping_getter=lambda: mapping,
        event_callback=event_callback,
        device_type=DeviceType.KEYBOARD,
        keyboard_uinput=keyboard_uinput,
        recorder=recorder,
    )

    await pipeline.process_event(
        grabbed,
        evdev.InputEvent(1, 200, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
        deps=grabbed_event_processing_deps(),
    )

    assert len(recorder.calls) == 1
    assert keyboard_uinput.write.call_args_list[0].args == (
        evdev.ecodes.EV_KEY,
        evdev.ecodes.KEY_A,
        1,
    )


@pytest.mark.asyncio
async def test_play_macro_allows_concurrent_playback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DeviceManager()
    manager.output_state.keyboard_uinput = MagicMock()

    started: list[str] = []
    finished: list[str] = []

    async def fake_play_macro_task(_manager: DeviceManager, **kwargs) -> None:
        name = kwargs.get("macro_name", "")
        started.append(name)
        await asyncio.sleep(0.05)
        finished.append(name)

    monkeypatch.setattr(scheduler, "play_macro_task", fake_play_macro_task)
    macro_events = [{"t_us": 0, "macro_action": "wait", "duration_us": 0}]
    await manager.play_macro(macro_events=macro_events, macro_name="first")
    await asyncio.sleep(0)
    await manager.play_macro(macro_events=macro_events, macro_name="second")
    await asyncio.sleep(0.1)

    assert "first" in started
    assert "second" in started
    assert "first" in finished
    assert "second" in finished


@pytest.mark.asyncio
async def test_stored_macro_playback_uses_and_releases_revision_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DeviceManager()
    manager.output_state.keyboard_uinput = MagicMock()
    closed = MagicMock()
    snapshot = SimpleNamespace(
        meta={"event_count": 1, "duration_us": 0, "revision": 1},
        iter_events=lambda: iter([{"t_us": 0, "type": 1, "code": 30, "value": 1}]),
        close=closed,
    )
    manager.macro_store = SimpleNamespace(open_snapshot=MagicMock(return_value=snapshot))

    async def fake_play_macro_task(_manager: DeviceManager, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(scheduler, "play_macro_task", fake_play_macro_task)

    result = await manager.play_macro(macro_name="stored")
    await asyncio.gather(*manager.macro_state.tasks.values())
    await asyncio.sleep(0)

    assert result == {"status": "ok"}
    manager.macro_store.open_snapshot.assert_called_once_with("stored")
    closed.assert_called_once()


@pytest.mark.asyncio
async def test_play_macro_uses_daemon_lifetime_outputs_without_active_grab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DeviceManager()
    created: list[bool] = []
    destroyed: list[bool] = []

    def fake_create_global_uinputs(_manager: DeviceManager, **_kwargs: object) -> None:
        created.append(True)
        _manager.output_state.keyboard_uinput = MagicMock()
        _manager.output_state.device_count += 1

    def fake_destroy_global_uinputs(_manager: DeviceManager, **_kwargs: object) -> None:
        destroyed.append(True)
        _manager.output_state.device_count = max(0, _manager.output_state.device_count - 1)
        if _manager.output_state.device_count == 0:
            _manager.output_state.keyboard_uinput = None

    monkeypatch.setattr(global_outputs, "create_global_uinputs", fake_create_global_uinputs)
    monkeypatch.setattr(global_outputs, "destroy_global_uinputs", fake_destroy_global_uinputs)

    manager.initialize_output_devices()
    result = await manager.play_macro(macro_events=[], macro_name="adhoc")
    await asyncio.sleep(0.02)

    assert result["status"] == "ok"
    assert created == [True]
    assert destroyed == []
    assert manager.output_state.keyboard_uinput is not None
    manager.shutdown_output_devices()
    assert destroyed == [True]
    assert manager.output_state.keyboard_uinput is None


@pytest.mark.asyncio
async def test_play_macro_can_skip_stored_lookup_for_empty_explicit_events() -> None:
    manager = DeviceManager()
    manager.output_state.keyboard_uinput = MagicMock()
    get_meta = MagicMock(side_effect=AssertionError("stored macro lookup should be skipped"))
    manager.macro_store = SimpleNamespace(get_meta=get_meta, iter_events=MagicMock())

    result = await manager.play_macro(
        macro_events=[],
        macro_name="recording-slot-2",
        load_stored_macro=False,
    )

    assert result == {"status": "ok"}
    get_meta.assert_not_called()


@pytest.mark.asyncio
async def test_play_macro_task_helper_uses_existing_loop_stop_behavior_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DeviceManager()
    manager.macro_state.instance_meta[1] = {"loop_stop_behavior": "cancel_run"}
    manager.macro_state.instance_meta[2] = {"loop_stop_behavior": DEFAULT_MACRO_LOOP_STOP_BEHAVIOR}
    observed: list[str] = []

    async def fake_play_macro_task(
        observed_manager: DeviceManager,
        **kwargs: object,
    ) -> None:
        instance_id = cast(int, kwargs["instance_id"])
        meta = observed_manager.macro_state.instance_meta[instance_id]
        observed.append(str(meta["loop_stop_behavior"]))

    monkeypatch.setattr(scheduler, "play_macro_task", fake_play_macro_task)

    base_kwargs = {
        "macro_events": [],
        "macro_name": "loop",
        "replay_mouse_movement": True,
        "replay_mouse_clicks": True,
        "speed": 1.0,
        "loop_mode": "hold",
        "loop_count": 1,
        "move_to_start": False,
        "start_x": 0,
        "start_y": 0,
        "block_mouse_movement": False,
    }
    await play_macro_task_helper(
        manager,
        instance_id=1,
        **base_kwargs,
    )
    await play_macro_task_helper(
        manager,
        instance_id=2,
        **base_kwargs,
    )

    assert observed == ["cancel_run", DEFAULT_MACRO_LOOP_STOP_BEHAVIOR]


@pytest.mark.asyncio
async def test_play_macro_can_move_mouse_to_saved_start() -> None:
    manager = DeviceManager()
    manager.output_state.mouse_uinput = MagicMock()

    await scheduler.play_macro_task(
        manager,
        instance_id=1,
        macro_events=[],
        macro_name="with_start",
        replay_mouse_movement=True,
        replay_mouse_clicks=True,
        speed=1.0,
        loop_mode="none",
        loop_count=1,
        move_to_start=True,
        start_x=640,
        start_y=360,
        block_mouse_movement=False,
        deps=device_manager._macro_runtime_deps(),
    )

    writes = manager.output_state.mouse_uinput.write.call_args_list
    assert any(c.args == (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, -2147483648) for c in writes)
    assert any(c.args == (evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, -2147483648) for c in writes)
    assert any(c.args == (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 640) for c in writes)
    assert any(c.args == (evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, 360) for c in writes)


@pytest.mark.asyncio
async def test_play_macro_block_mouse_movement_uses_suppression_safeguard() -> None:
    manager = DeviceManager()

    begin_mouse_rel_suppression = MagicMock()
    end_mouse_rel_suppression = MagicMock()
    await scheduler.play_macro_task(
        manager,
        instance_id=1,
        macro_events=[],
        macro_name="blocked",
        replay_mouse_movement=True,
        replay_mouse_clicks=True,
        speed=1.0,
        loop_mode="none",
        loop_count=1,
        move_to_start=False,
        start_x=0,
        start_y=0,
        block_mouse_movement=True,
        deps=device_manager._macro_runtime_deps(),
        acquire_mouse_inhibit_fn=begin_mouse_rel_suppression,
        release_mouse_inhibit_fn=end_mouse_rel_suppression,
        begin_mouse_suppression_fn=begin_mouse_rel_suppression,
    )

    assert begin_mouse_rel_suppression.called
    assert end_mouse_rel_suppression.called


@pytest.mark.asyncio
async def test_play_macro_block_mouse_movement_renews_suppression_for_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DeviceManager()
    clock = {"now": 100.0}

    class _FakeLoop:
        def time(self) -> float:
            return clock["now"]

    async def fake_sleep(duration: float) -> None:
        clock["now"] += duration

    begin_mouse_rel_suppression = MagicMock()
    end_mouse_rel_suppression = MagicMock()

    async def run_control_action(
        observed_manager: DeviceManager,
        event: dict[str, object],
        *,
        renew_mouse_suppression: bool,
        deps: MacroRuntimeDeps,
    ) -> float:
        return await controls.run_macro_control_action(
            observed_manager,
            event,
            renew_mouse_suppression=renew_mouse_suppression,
            deps=deps,
            renew_mouse_suppression_fn=begin_mouse_rel_suppression,
        )

    monkeypatch.setattr(device_manager.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(device_manager.asyncio, "get_running_loop", lambda: _FakeLoop())

    await scheduler.play_macro_task(
        manager,
        instance_id=1,
        macro_events=[{"t_us": 0, "macro_action": "wait", "duration_us": 10_000_000}],
        macro_name="blocked_wait",
        replay_mouse_movement=True,
        replay_mouse_clicks=True,
        speed=1.0,
        loop_mode="none",
        loop_count=1,
        move_to_start=False,
        start_x=0,
        start_y=0,
        block_mouse_movement=True,
        deps=device_manager._macro_runtime_deps(),
        control_action_fn=run_control_action,
        acquire_mouse_inhibit_fn=begin_mouse_rel_suppression,
        release_mouse_inhibit_fn=end_mouse_rel_suppression,
        begin_mouse_suppression_fn=begin_mouse_rel_suppression,
        renew_mouse_suppression_fn=begin_mouse_rel_suppression,
    )

    timeouts = [call.kwargs["timeout_s"] for call in begin_mouse_rel_suppression.call_args_list]
    assert any(timeout == pytest.approx(11.0) for timeout in timeouts)
    assert end_mouse_rel_suppression.called


@pytest.mark.asyncio
async def test_play_macro_honors_empty_macro_duration_as_scaled_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DeviceManager()
    clock = {"now": 100.0}
    sleep_calls: list[float] = []

    class _FakeLoop:
        def time(self) -> float:
            return clock["now"]

    async def fake_sleep(duration: float) -> None:
        sleep_calls.append(duration)
        clock["now"] += duration

    monkeypatch.setattr(device_manager.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(device_manager.asyncio, "get_running_loop", lambda: _FakeLoop())

    await scheduler.play_macro_task(
        manager,
        instance_id=1,
        macro_events=[],
        macro_event_source=MacroEventSource(
            event_count=0,
            duration_us=10_000_000,
            iter_events=lambda: iter(()),
        ),
        macro_name="empty_space",
        replay_mouse_movement=True,
        replay_mouse_clicks=True,
        speed=2.0,
        loop_mode="none",
        loop_count=1,
        move_to_start=False,
        start_x=0,
        start_y=0,
        block_mouse_movement=False,
        deps=device_manager._macro_runtime_deps(),
    )

    assert sleep_calls == [pytest.approx(5.0), 0]


@pytest.mark.asyncio
async def test_play_macro_releases_runtime_state_when_snapshot_close_fails() -> None:
    manager = DeviceManager()
    manager.macro_state.allocate_instance(
        loop_mode="none",
        source_key=("keyboard", "key_a"),
        macro_name="close_failure",
        loop_stop_behavior="finish_run",
    )
    release_held = MagicMock()

    def fail_close() -> None:
        raise OSError("close failed")

    await scheduler.play_macro_task(
        manager,
        instance_id=1,
        macro_events=[],
        macro_event_source=MacroEventSource(
            event_count=0,
            duration_us=0,
            iter_events=lambda: iter(()),
            close=fail_close,
        ),
        macro_name="close_failure",
        replay_mouse_movement=True,
        replay_mouse_clicks=True,
        speed=1.0,
        loop_mode="none",
        loop_count=1,
        move_to_start=False,
        start_x=0,
        start_y=0,
        block_mouse_movement=False,
        deps=device_manager._macro_runtime_deps(),
        release_held_fn=release_held,
    )

    release_held.assert_called_once_with(
        manager,
        1,
        deps=device_manager._macro_runtime_deps(),
    )
    assert 1 not in manager.macro_state.instance_meta


@pytest.mark.asyncio
async def test_play_macro_early_return_suppresses_snapshot_close_failure() -> None:
    manager = DeviceManager()

    def fail_close() -> None:
        raise OSError("close failed")

    result = await manager.play_macro(
        macro_name="unavailable",
        macro_event_source=MacroEventSource(
            event_count=1,
            duration_us=0,
            iter_events=lambda: iter(()),
            close=fail_close,
        ),
    )

    assert result == {"status": "error", "message": "No output uinput devices available"}


@pytest.mark.asyncio
async def test_play_macro_does_not_double_sleep_when_wait_exceeds_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DeviceManager()
    clock = {"now": 100.0}
    sleep_calls: list[float] = []

    class _FakeLoop:
        def time(self) -> float:
            return clock["now"]

    async def fake_sleep(duration: float) -> None:
        sleep_calls.append(duration)
        clock["now"] += duration

    monkeypatch.setattr(device_manager.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(device_manager.asyncio, "get_running_loop", lambda: _FakeLoop())

    await scheduler.play_macro_task(
        manager,
        instance_id=1,
        macro_events=[],
        macro_event_source=MacroEventSource(
            event_count=1,
            duration_us=100_000,
            iter_events=lambda: iter([{"t_us": 0, "macro_action": "wait", "duration_us": 200_000}]),
        ),
        macro_name="wait_longer_than_duration",
        replay_mouse_movement=True,
        replay_mouse_clicks=True,
        speed=1.0,
        loop_mode="none",
        loop_count=1,
        move_to_start=False,
        start_x=0,
        start_y=0,
        block_mouse_movement=False,
        deps=device_manager._macro_runtime_deps(),
    )

    assert sleep_calls == [pytest.approx(0.2), 0]


@pytest.mark.asyncio
async def test_hold_macro_block_mouse_movement_refreshes_suppression_until_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DeviceManager()
    manager.output_state.mouse_uinput = MagicMock()

    begin_mouse_rel_suppression = MagicMock()
    end_mouse_rel_suppression = MagicMock()
    monkeypatch.setattr(mouse, "begin_mouse_rel_suppression", begin_mouse_rel_suppression)
    monkeypatch.setattr(mouse, "end_mouse_rel_suppression", end_mouse_rel_suppression)

    await manager.play_macro(
        macro_events=[{"t_us": 0, "macro_action": "wait", "duration_us": 10_000}],
        macro_name="hold_blocked",
        loop_mode="hold",
        block_mouse_movement=True,
        source_device="dev1",
        source_button="btn_hold",
        trigger_value=1,
    )
    await asyncio.sleep(0.05)

    result = await manager.play_macro(
        macro_events=[],
        macro_name="hold_blocked",
        loop_mode="hold",
        block_mouse_movement=True,
        source_device="dev1",
        source_button="btn_hold",
        trigger_value=0,
    )

    assert result["status"] == "ok"
    assert result["cancelled"] is False
    await asyncio.sleep(0.02)
    assert begin_mouse_rel_suppression.call_count > 1
    assert end_mouse_rel_suppression.called


@pytest.mark.asyncio
async def test_cancel_macro_playback_releases_held_keys() -> None:
    manager = DeviceManager()
    manager.output_state.keyboard_uinput = MagicMock()

    await manager.play_macro(
        macro_events=[
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_A,
                "value": 1,
                "device_type": "keyboard",
            },
            {
                "t_us": 2_000_000,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_A,
                "value": 0,
                "device_type": "keyboard",
            },
        ],
        macro_name="hold",
    )
    await asyncio.sleep(0.01)

    result = await manager.cancel_macro_playback()

    assert result["status"] == "ok"
    assert result["cancelled"] is True
    assert ("keyboard", evdev.ecodes.KEY_A) not in manager.macro_state.held_refcount
    assert any(
        c.args == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0)
        for c in manager.output_state.keyboard_uinput.write.call_args_list
    )


@pytest.mark.asyncio
async def test_cancel_macro_playback_releases_held_gamepad_abs() -> None:
    manager = DeviceManager()
    manager.output_state.gamepad_uinput = MagicMock()

    await manager.play_macro(
        macro_events=[
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_ABS,
                "code": evdev.ecodes.ABS_RZ,
                "value": 255,
                "device_type": "gamepad",
            },
            {
                "t_us": 2_000_000,
                "type": evdev.ecodes.EV_ABS,
                "code": evdev.ecodes.ABS_RZ,
                "value": 0,
                "device_type": "gamepad",
            },
        ],
        macro_name="trigger_hold",
    )
    await asyncio.sleep(0.01)

    result = await manager.cancel_macro_playback()

    assert result["status"] == "ok"
    assert result["cancelled"] is True
    assert ("gamepad", evdev.ecodes.ABS_RZ) not in manager.macro_state.held_abs_refcount
    assert any(
        c.args == (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RZ, 0)
        for c in manager.output_state.gamepad_uinput.write.call_args_list
    )


@pytest.mark.asyncio
async def test_macro_abs_cleanup_ignores_explicit_neutral_value() -> None:
    manager = DeviceManager()
    manager.output_state.gamepad_uinput = MagicMock()

    await manager.play_macro(
        macro_events=[
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_ABS,
                "code": evdev.ecodes.ABS_HAT0X,
                "value": 1,
                "device_type": "gamepad",
            },
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_ABS,
                "code": evdev.ecodes.ABS_HAT0X,
                "value": 0,
                "device_type": "gamepad",
            },
        ],
        macro_name="hat_tap",
    )
    await asyncio.sleep(0.01)

    assert manager.macro_state.held_abs_refcount == {}
    assert [
        c.args
        for c in manager.output_state.gamepad_uinput.write.call_args_list
        if c.args == (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_HAT0X, 0)
    ] == [(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_HAT0X, 0)]


@pytest.mark.asyncio
async def test_play_macro_routes_gamepad_event_output_id() -> None:
    manager = DeviceManager()
    default_gamepad = MagicMock()
    second_gamepad = MagicMock()
    manager.output_state.virtual_gamepad_uinputs = {
        "virtual-gamepad-1": default_gamepad,
        "virtual-gamepad-2": second_gamepad,
    }

    await scheduler.play_macro_task(
        manager,
        instance_id=1,
        macro_events=[
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.BTN_SOUTH,
                "value": 1,
                "device_type": "gamepad",
                "output_id": "virtual-gamepad-2",
            },
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.BTN_SOUTH,
                "value": 0,
                "device_type": "gamepad",
                "output_id": "virtual-gamepad-2",
            },
        ],
        macro_name="routed_gamepad",
        replay_mouse_movement=True,
        replay_mouse_clicks=True,
        speed=1.0,
        loop_mode="none",
        loop_count=1,
        move_to_start=False,
        start_x=0,
        start_y=0,
        block_mouse_movement=False,
        deps=device_manager._macro_runtime_deps(),
    )

    default_gamepad.write.assert_not_called()
    assert [call.args for call in second_gamepad.write.call_args_list] == [
        (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 1),
        (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 0),
    ]


@pytest.mark.asyncio
async def test_macro_switch_interrupt_releases_held_state_for_previous_instance() -> None:
    manager = DeviceManager()
    manager.output_state.keyboard_uinput = MagicMock()

    await manager.play_macro(
        macro_events=[
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_J,
                "value": 1,
                "device_type": "keyboard",
            },
            {
                "t_us": 2_000_000,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_J,
                "value": 0,
                "device_type": "keyboard",
            },
        ],
        macro_name="toggle_a",
        loop_mode="toggle",
        loop_stop_behavior="cancel_run",
        source_device="dev1",
        source_button="btn_toggle",
        trigger_value=1,
    )
    await asyncio.sleep(0.02)

    result = await manager.play_macro(
        macro_events=[],
        macro_name="toggle_b",
        loop_mode="toggle",
        source_device="dev1",
        source_button="btn_toggle",
        trigger_value=1,
    )

    assert result["status"] == "ok"
    assert result["cancelled"] is True
    assert ("keyboard", evdev.ecodes.KEY_J) not in manager.macro_state.held_refcount
    assert any(
        c.args == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_J, 0)
        for c in manager.output_state.keyboard_uinput.write.call_args_list
    )


@pytest.mark.asyncio
async def test_macro_cleanup_releases_unmatched_held_keys() -> None:
    manager = DeviceManager()
    manager.output_state.keyboard_uinput = MagicMock()

    await manager.play_macro(
        macro_events=[
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_B,
                "value": 1,
                "device_type": "keyboard",
            }
        ],
        macro_name="unmatched",
    )

    if manager.macro_state.tasks:
        await asyncio.gather(*manager.macro_state.tasks.values())

    assert manager.macro_state.held_refcount == {}
    assert any(
        c.args == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0)
        for c in manager.output_state.keyboard_uinput.write.call_args_list
    )


def test_macro_cleanup_sync_uses_raw_uinput_with_non_identity_writer() -> None:
    manager = DeviceManager()
    raw_keyboard = MagicMock()
    writer = MagicMock()
    manager.output_state.keyboard_uinput = raw_keyboard
    manager.macro_state.instance_held[1] = {("keyboard", evdev.ecodes.KEY_B)}
    manager.macro_state.held_refcount[("keyboard", evdev.ecodes.KEY_B)] = 1
    sync_calls: list[tuple[object, object]] = []

    base_deps = device_manager._macro_runtime_deps()
    deps = MacroRuntimeDeps(
        asyncio_mod=base_deps.asyncio_mod,
        evdev_mod=base_deps.evdev_mod,
        uinput_writer=lambda raw: writer if raw is raw_keyboard else None,
        log=base_deps.log,
        int_value_fn=base_deps.int_value_fn,
        str_value_fn=base_deps.str_value_fn,
    )
    outputs.release_macro_held_for_instance(
        manager,
        1,
        deps=deps,
        sync_fn=lambda raw_uinput, output_writer: sync_calls.append((raw_uinput, output_writer)),
    )

    writer.write.assert_called_once_with(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0)
    assert sync_calls == [(raw_keyboard, writer)]
    assert manager.macro_state.held_refcount == {}


def test_macro_cleanup_logs_expected_output_release_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = DeviceManager()
    keyboard = MagicMock()
    gamepad = MagicMock()
    keyboard.write.side_effect = OSError("keyboard gone")
    gamepad.write.side_effect = OSError("gamepad gone")
    manager.output_state.keyboard_uinput = keyboard
    manager.output_state.gamepad_uinput = gamepad
    manager.macro_state.instance_held[1] = {("keyboard", evdev.ecodes.KEY_B)}
    manager.macro_state.instance_held_abs[1] = {("gamepad", evdev.ecodes.ABS_Z)}
    manager.macro_state.held_refcount[("keyboard", evdev.ecodes.KEY_B)] = 1
    manager.macro_state.held_abs_refcount[("gamepad", evdev.ecodes.ABS_Z)] = 1

    with caplog.at_level(logging.DEBUG, logger="keymasqd.devices"):
        outputs.release_macro_held_for_instance(
            manager, 1, deps=device_manager._macro_runtime_deps()
        )

    assert manager.macro_state.held_refcount == {}
    assert manager.macro_state.held_abs_refcount == {}
    assert "Failed to release macro-held output key" in caplog.text
    assert "Failed to release macro-held ABS output" in caplog.text


def test_macro_cleanup_logs_unexpected_output_release_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = DeviceManager()
    keyboard = MagicMock()
    gamepad = MagicMock()
    keyboard.write.side_effect = RuntimeError("keyboard writer invalid")
    gamepad.write.side_effect = RuntimeError("gamepad writer invalid")
    manager.output_state.keyboard_uinput = keyboard
    manager.output_state.gamepad_uinput = gamepad
    manager.macro_state.instance_held[1] = {("keyboard", evdev.ecodes.KEY_B)}
    manager.macro_state.instance_held_abs[1] = {("gamepad", evdev.ecodes.ABS_Z)}
    manager.macro_state.held_refcount[("keyboard", evdev.ecodes.KEY_B)] = 1
    manager.macro_state.held_abs_refcount[("gamepad", evdev.ecodes.ABS_Z)] = 1

    with caplog.at_level(logging.ERROR, logger="keymasqd.devices"):
        outputs.release_macro_held_for_instance(
            manager, 1, deps=device_manager._macro_runtime_deps()
        )

    assert manager.macro_state.held_refcount == {}
    assert manager.macro_state.held_abs_refcount == {}
    assert "Unexpected failure releasing macro-held output key" in caplog.text
    assert "Unexpected failure releasing macro-held ABS output" in caplog.text
    assert "RuntimeError: keyboard writer invalid" in caplog.text
    assert "RuntimeError: gamepad writer invalid" in caplog.text


def test_macro_cleanup_logs_sync_failures(caplog: pytest.LogCaptureFixture) -> None:
    def make_manager() -> DeviceManager:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = MagicMock()
        manager.macro_state.instance_held[1] = {("keyboard", evdev.ecodes.KEY_B)}
        manager.macro_state.held_refcount[("keyboard", evdev.ecodes.KEY_B)] = 1
        return manager

    with caplog.at_level(logging.DEBUG, logger="keymasqd.devices"):
        outputs.release_macro_held_for_instance(
            make_manager(),
            1,
            deps=device_manager._macro_runtime_deps(),
            sync_fn=MagicMock(side_effect=OSError("sync failed")),
        )

    assert "Failed to synchronize macro cleanup outputs" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="keymasqd.devices"):
        outputs.release_macro_held_for_instance(
            make_manager(),
            1,
            deps=device_manager._macro_runtime_deps(),
            sync_fn=MagicMock(side_effect=RuntimeError("sync state invalid")),
        )

    assert "Unexpected failure synchronizing macro cleanup outputs" in caplog.text
    assert "RuntimeError: sync state invalid" in caplog.text


@pytest.mark.asyncio
async def test_release_all_devices_cleans_up_macro_and_grabbed_state() -> None:
    manager = DeviceManager()
    keyboard_uinput = MagicMock()
    manager.output_state.keyboard_uinput = keyboard_uinput

    grabbed = MagicMock()
    grabbed.release = AsyncMock()
    grabbed.stop_event_loop = AsyncMock()
    manager.grabbed_devices = {"1234:5678": [grabbed]}
    manager.active_mappings = {
        "1234:5678": {"btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_a")}
    }
    manager.grab_state.desired_paths = {"1234:5678": {"/dev/input/event0"}}

    await manager.play_macro(
        macro_events=[
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_K,
                "value": 1,
                "device_type": "keyboard",
            },
            {
                "t_us": 2_000_000,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_K,
                "value": 0,
                "device_type": "keyboard",
            },
        ],
        macro_name="disconnect_cleanup",
    )
    await asyncio.sleep(0.02)

    await manager.release_all_devices()

    assert manager.grabbed_devices == {}
    assert manager.active_mappings == {}
    assert manager.grab_state.desired_paths == {}
    assert manager.macro_state.held_refcount == {}
    grabbed.release.assert_awaited_once()
    assert any(
        c.args == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_K, 0)
        for c in keyboard_uinput.write.call_args_list
    )


@pytest.mark.asyncio
async def test_play_macro_count_loop_repeats_events() -> None:
    manager = DeviceManager()
    manager.output_state.keyboard_uinput = MagicMock()

    await manager.play_macro(
        macro_events=[
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_C,
                "value": 1,
                "device_type": "keyboard",
            },
            {
                "t_us": 1_000,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_C,
                "value": 0,
                "device_type": "keyboard",
            },
        ],
        macro_name="count",
        loop_mode="count",
        loop_count=2,
    )

    if manager.macro_state.tasks:
        await asyncio.gather(*manager.macro_state.tasks.values())

    press_calls = [
        c
        for c in manager.output_state.keyboard_uinput.write.call_args_list
        if c.args[2] == 1 and c.args[1] == evdev.ecodes.KEY_C
    ]
    release_calls = [
        c
        for c in manager.output_state.keyboard_uinput.write.call_args_list
        if c.args[2] == 0 and c.args[1] == evdev.ecodes.KEY_C
    ]
    assert len(press_calls) == 2
    assert len(release_calls) == 2


@pytest.mark.asyncio
async def test_play_macro_handles_macro_moves_and_unusual_device_type_routing() -> None:
    manager = DeviceManager()
    manager.output_state.keyboard_uinput = MagicMock()
    manager.output_state.mouse_uinput = MagicMock()
    manager.set_cursor_position = AsyncMock(return_value={"status": "ok"})  # type: ignore[method-assign]

    await scheduler.play_macro_task(
        manager,
        instance_id=1,
        macro_events=[
            {
                "device_type": "macro",
                "type": 0,
                "code": 0,
                "value": 0,
                "t_us": 0,
                "macro_action": "mouse_move_abs",
                "x": 320,
                "y": 240,
            },
            {
                "device_type": "macro",
                "type": 0,
                "code": 0,
                "value": 0,
                "t_us": 0,
                "macro_action": "mouse_move_rel",
                "x": 7,
                "y": -3,
            },
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_SYN,
                "code": evdev.ecodes.SYN_REPORT,
                "value": 0,
                "device_type": "keyboard",
            },
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_REL,
                "code": evdev.ecodes.REL_X,
                "value": 5,
                "device_type": "mouse",
            },
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_REL,
                "code": evdev.ecodes.REL_WHEEL,
                "value": -1,
                "device_type": "mouse",
            },
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_REL,
                "code": getattr(evdev.ecodes, "REL_WHEEL_HI_RES", evdev.ecodes.REL_WHEEL),
                "value": -120,
                "device_type": "mouse",
            },
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.BTN_LEFT,
                "value": 1,
                "device_type": "mouse",
            },
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_Q,
                "value": 1,
                "device_type": "other",
            },
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_ABS,
                "code": evdev.ecodes.ABS_X,
                "value": 123,
                "device_type": "other",
            },
            {
                "t_us": 0,
                "type": 9999,
                "code": 1,
                "value": 1,
                "device_type": "other",
            },
        ],
        macro_name="macro-moves",
        replay_mouse_movement=True,
        replay_mouse_clicks=False,
        speed=1.0,
        loop_mode="none",
        loop_count=1,
        move_to_start=False,
        start_x=0,
        start_y=0,
        block_mouse_movement=False,
        deps=device_manager._macro_runtime_deps(),
    )

    manager.set_cursor_position.assert_awaited_once_with(320, 240)
    assert [call.args for call in manager.output_state.keyboard_uinput.write.call_args_list] == [
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_Q, 1),
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_Q, 0),
    ]
    assert [call.args for call in manager.output_state.mouse_uinput.write.call_args_list] == [
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 7),
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, -3),
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 5),
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL, -1),
        (
            evdev.ecodes.EV_REL,
            getattr(evdev.ecodes, "REL_WHEEL_HI_RES", evdev.ecodes.REL_WHEEL),
            -120,
        ),
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 123),
    ]


@pytest.mark.asyncio
async def test_play_macro_replays_semantic_moves_when_recorded_movement_disabled() -> None:
    manager = DeviceManager()
    manager.output_state.mouse_uinput = MagicMock()
    manager.set_cursor_position = AsyncMock(return_value={"status": "ok"})  # type: ignore[method-assign]
    manager.move_cursor_natural = AsyncMock(return_value={"status": "ok"})  # type: ignore[method-assign]

    await scheduler.play_macro_task(
        manager,
        instance_id=1,
        macro_events=[
            {
                "device_type": "macro",
                "type": 0,
                "code": 0,
                "value": 0,
                "t_us": 0,
                "macro_action": "mouse_move_abs",
                "x": 320,
                "y": 240,
            },
            {
                "device_type": "macro",
                "type": 0,
                "code": 0,
                "value": 0,
                "t_us": 0,
                "macro_action": "mouse_move_natural_abs",
                "x": 640,
                "y": 480,
                "speed": 12000.0,
                "jitter": 0.3,
                "curve": "natural",
                "tolerance": 2,
                "max_duration_ms": 3000,
            },
            {
                "device_type": "macro",
                "type": 0,
                "code": 0,
                "value": 0,
                "t_us": 0,
                "macro_action": "mouse_move_rel",
                "x": 7,
                "y": -3,
            },
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_REL,
                "code": evdev.ecodes.REL_X,
                "value": 5,
                "device_type": "mouse",
            },
        ],
        macro_name="semantic_moves",
        replay_mouse_movement=False,
        replay_mouse_clicks=True,
        speed=1.0,
        loop_mode="none",
        loop_count=1,
        move_to_start=False,
        start_x=0,
        start_y=0,
        block_mouse_movement=False,
        deps=device_manager._macro_runtime_deps(),
    )

    manager.set_cursor_position.assert_awaited_once_with(320, 240)
    manager.move_cursor_natural.assert_awaited_once_with(  # type: ignore[attr-defined]
        640,
        480,
        12000.0,
        0.3,
        "natural",
        2,
        3000,
    )
    assert [call.args for call in manager.output_state.mouse_uinput.write.call_args_list] == [
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 7),
        (evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, -3),
    ]


@pytest.mark.asyncio
async def test_play_macro_natural_move_can_stop_current_run_on_failure() -> None:
    manager = DeviceManager()
    manager.output_state.keyboard_uinput = MagicMock()
    manager.move_cursor_natural = AsyncMock(  # type: ignore[method-assign]
        return_value={"status": "error", "reached": False}
    )

    await scheduler.play_macro_task(
        manager,
        instance_id=1,
        macro_events=[
            {
                "device_type": "macro",
                "type": 0,
                "code": 0,
                "value": 0,
                "t_us": 0,
                "macro_action": "mouse_move_natural_abs",
                "x": 320,
                "y": 240,
                "speed": 1500.0,
                "jitter": 0.5,
                "curve": "linear",
                "tolerance": 3,
                "max_duration_ms": 1200,
                "stop_on_failure": True,
            },
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_A,
                "value": 1,
                "device_type": "keyboard",
            },
        ],
        macro_name="natural_stop",
        replay_mouse_movement=True,
        replay_mouse_clicks=True,
        speed=1.0,
        loop_mode="none",
        loop_count=1,
        move_to_start=False,
        start_x=0,
        start_y=0,
        block_mouse_movement=False,
        deps=device_manager._macro_runtime_deps(),
    )

    manager.move_cursor_natural.assert_awaited_once_with(  # type: ignore[attr-defined]
        320,
        240,
        1500.0,
        0.5,
        "linear",
        3,
        1200,
    )
    manager.output_state.keyboard_uinput.write.assert_not_called()


@pytest.mark.asyncio
async def test_play_macro_wait_control_actions_shift_later_deadlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DeviceManager()
    manager.output_state.keyboard_uinput = MagicMock()
    clock = {"now": 100.0}
    sleep_calls: list[float] = []

    class _FakeLoop:
        def time(self) -> float:
            return clock["now"]

    async def fake_sleep(duration: float) -> None:
        sleep_calls.append(duration)
        clock["now"] += duration

    async def fake_run_macro_control_action(*args, **kwargs) -> float:
        return 0.2

    monkeypatch.setattr(device_manager.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(device_manager.asyncio, "get_running_loop", lambda: _FakeLoop())
    await scheduler.play_macro_task(
        manager,
        instance_id=1,
        macro_events=[
            {
                "t_us": 0,
                "macro_action": "wait",
                "duration_us": 200_000,
            },
            {
                "t_us": 100_000,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_A,
                "value": 0,
                "device_type": "keyboard",
            },
        ],
        macro_name="shifted_deadline",
        replay_mouse_movement=True,
        replay_mouse_clicks=True,
        speed=1.0,
        loop_mode="none",
        loop_count=1,
        move_to_start=False,
        start_x=0,
        start_y=0,
        block_mouse_movement=False,
        deps=device_manager._macro_runtime_deps(),
        control_action_fn=fake_run_macro_control_action,
    )

    assert sleep_calls == [pytest.approx(0.3), 0]
