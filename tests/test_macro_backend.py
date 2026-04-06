import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import evdev
import pytest

import keyforge.common.paths as paths
import keyforge.keyforged.device_manager as dm
import keyforge.keyforged.recording as recording_module
from keyforge.common.models import (
    ActionType,
    DeviceProfileLayer,
    DeviceType,
    MappingAction,
    ProfileConfig,
)
from keyforge.keyforged.device_manager import DeviceManager
from keyforge.keyforged.recording import RecordingManager
from keyforge.keyforged.runtime import grabbed_device as gdm
from keyforge.keyforged.runtime import macros as mdm
from keyforge.keyforged.runtime.grabbed_device import GrabbedDevice
from keyforge.session.profiles import ProfileManager


class _FakeRecorder:
    def __init__(self) -> None:
        self.is_recording = True
        self.calls: list[tuple[str, evdev.InputEvent]] = []

    def record_event(self, device_type: str, event: evdev.InputEvent) -> None:
        self.calls.append((device_type, event))


async def _play_macro_task(manager: DeviceManager, **kwargs: object) -> None:
    await mdm.play_macro_task(
        manager,
        instance_id=int(kwargs["instance_id"]),
        macro_events=cast(list[dict[str, object]], kwargs["macro_events"]),
        macro_name=str(kwargs["macro_name"]),
        replay_mouse_movement=bool(kwargs["replay_mouse_movement"]),
        replay_mouse_clicks=bool(kwargs["replay_mouse_clicks"]),
        speed=float(kwargs["speed"]),
        loop_mode=str(kwargs["loop_mode"]),
        loop_count=int(kwargs["loop_count"]),
        move_to_start=bool(kwargs["move_to_start"]),
        start_x=int(kwargs["start_x"]),
        start_y=int(kwargs["start_y"]),
        block_mouse_movement=bool(kwargs["block_mouse_movement"]),
        asyncio_mod=dm._macro_asyncio_runtime(),
        evdev_mod=dm.evdev,
        log=dm.log,
        int_value_fn=dm._int_value,
        str_value_fn=dm._str_value,
        uinput_writer=dm._macro_uinput_writer(),
        contextlib_mod=dm.contextlib,
        random_mod=dm.random,
        uuid_mod=dm.uuid,
        command_type=dm._macro_command_type(),
    )


async def _process_grabbed_event(device: GrabbedDevice, event: evdev.InputEvent) -> None:
    await gdm.process_event(
        device,
        event,
        evdev_mod=evdev,
        time_mod=gdm.time,
        log=gdm.log,
        combo_decision_cls=gdm.ComboDecision,
        classify_event_device_type_fn=gdm.classify_event_device_type,
        action_type_enum=gdm.ActionType,
    )


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
    assert recorder._events[0]["t_us"] == 0
    assert recorder._events[1]["t_us"] == 400


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
    events = cast(list[dict[str, object]], result["events"])

    assert result["event_count"] == 2
    assert all(event["type"] == evdev.ecodes.EV_KEY for event in events)


@pytest.mark.asyncio
async def test_recording_ignores_start_stop_mapping_buttons() -> None:
    recorder = _FakeRecorder()
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
    await _process_grabbed_event(gd_start, start_event)
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
    await _process_grabbed_event(gd_normal, normal_event)

    assert len(recorder.calls) == 1
    assert recorder.calls[0][0] == "keyboard"
    assert recorder.calls[0][1].code == evdev.ecodes.KEY_F14


@pytest.mark.asyncio
async def test_play_macro_allows_concurrent_playback() -> None:
    manager = DeviceManager()
    manager.output_state.keyboard_uinput = MagicMock()

    started: list[str] = []
    finished: list[str] = []

    async def fake_play_macro_task(_manager: DeviceManager, **kwargs) -> None:
        name = kwargs.get("macro_name", "")
        started.append(name)
        await asyncio.sleep(0.05)
        finished.append(name)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mdm, "play_macro_task", fake_play_macro_task)
    try:
        await manager.play_macro(macro_events=[], macro_name="first")
        await asyncio.sleep(0)
        await manager.play_macro(macro_events=[], macro_name="second")
        await asyncio.sleep(0.1)

        assert "first" in started
        assert "second" in started
        assert "first" in finished
        assert "second" in finished
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_play_macro_can_move_mouse_to_saved_start() -> None:
    manager = DeviceManager()
    manager.output_state.mouse_uinput = MagicMock()

    await _play_macro_task(
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
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mdm, "begin_mouse_rel_suppression", begin_mouse_rel_suppression)
    monkeypatch.setattr(mdm, "end_mouse_rel_suppression", end_mouse_rel_suppression)

    await _play_macro_task(
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
    )

    assert begin_mouse_rel_suppression.called
    assert end_mouse_rel_suppression.called
    monkeypatch.undo()


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

    await asyncio.sleep(0.01)

    assert manager.macro_state.held_refcount == {}
    assert any(
        c.args == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0)
        for c in manager.output_state.keyboard_uinput.write.call_args_list
    )


@pytest.mark.asyncio
async def test_release_all_devices_cleans_up_macro_and_grabbed_state() -> None:
    manager = DeviceManager()
    keyboard_uinput = MagicMock()
    manager.output_state.keyboard_uinput = keyboard_uinput

    grabbed = MagicMock()
    grabbed.release = AsyncMock()
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

    await asyncio.sleep(0.01)

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
async def test_play_macro_handles_synthetic_abs_and_unusual_device_type_routing() -> None:
    manager = DeviceManager()
    manager.output_state.keyboard_uinput = MagicMock()
    manager.output_state.mouse_uinput = MagicMock()
    emit_absolute_mouse_move = MagicMock()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mdm, "emit_absolute_mouse_move", emit_absolute_mouse_move)

    await _play_macro_task(
        manager,
        instance_id=1,
        macro_events=[
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_REL,
                "code": evdev.ecodes.REL_X,
                "value": 320,
                "device_type": "mouse",
                "synthetic_move": True,
                "move_mode": "abs",
                "move_id": "abs-1",
                "move_step": 1,
            },
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_REL,
                "code": evdev.ecodes.REL_Y,
                "value": 240,
                "device_type": "mouse",
                "synthetic_move": True,
                "move_mode": "abs",
                "move_id": "abs-1",
                "move_step": 1,
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
        macro_name="synthetic",
        replay_mouse_movement=False,
        replay_mouse_clicks=False,
        speed=1.0,
        loop_mode="none",
        loop_count=1,
        move_to_start=False,
        start_x=0,
        start_y=0,
        block_mouse_movement=False,
    )

    emit_absolute_mouse_move.assert_called_once_with(
        manager,
        320,
        240,
        evdev_mod=dm.evdev,
        uinput_writer=dm._macro_uinput_writer(),
    )
    assert [call.args for call in manager.output_state.keyboard_uinput.write.call_args_list] == [
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_Q, 1),
        (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_Q, 0),
    ]
    assert [call.args for call in manager.output_state.mouse_uinput.write.call_args_list] == [
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 123)
    ]
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_recording_manager_start_opens_extra_devices_via_to_thread(monkeypatch) -> None:
    recorder = RecordingManager(broadcast_callback=AsyncMock())
    recorder._read_extra_device = AsyncMock(return_value=None)  # type: ignore[method-assign]
    calls: list[tuple[object, tuple[object, ...]]] = []

    async def fake_to_thread(func, /, *args, **kwargs):
        assert kwargs == {}
        calls.append((func, args))
        return SimpleNamespace(close=MagicMock())

    monkeypatch.setattr(recording_module.asyncio, "to_thread", fake_to_thread)

    await recorder.start([{"path": "/dev/input/event0", "device_type": "keyboard"}])
    await asyncio.sleep(0)
    await recorder.stop()

    assert calls == [(evdev.InputDevice, ("/dev/input/event0",))]


@pytest.mark.asyncio
async def test_play_macro_hold_loop_stops_on_release_trigger() -> None:
    manager = DeviceManager()
    manager.output_state.keyboard_uinput = MagicMock()

    await manager.play_macro(
        macro_events=[
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_D,
                "value": 1,
                "device_type": "keyboard",
            },
            {
                "t_us": 100_000,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_D,
                "value": 0,
                "device_type": "keyboard",
            },
        ],
        macro_name="hold",
        loop_mode="hold",
        source_device="dev1",
        source_button="btn1",
        trigger_value=1,
    )

    await asyncio.sleep(0.02)
    result = await manager.play_macro(
        macro_events=[],
        macro_name="hold",
        loop_mode="hold",
        source_device="dev1",
        source_button="btn1",
        trigger_value=0,
    )

    assert result["status"] == "ok"
    assert result["cancelled"] is True


@pytest.mark.asyncio
async def test_hold_release_cancels_even_if_release_call_loop_mode_is_none() -> None:
    manager = DeviceManager()
    manager.output_state.keyboard_uinput = MagicMock()

    await manager.play_macro(
        macro_events=[
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_F,
                "value": 1,
                "device_type": "keyboard",
            },
            {
                "t_us": 100_000,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_F,
                "value": 0,
                "device_type": "keyboard",
            },
        ],
        macro_name="hold_any_release",
        loop_mode="hold",
        source_device="dev1",
        source_button="btn_hold",
        trigger_value=1,
    )

    await asyncio.sleep(0.02)
    result = await manager.play_macro(
        macro_events=[],
        macro_name="hold_any_release",
        loop_mode="none",
        source_device="dev1",
        source_button="btn_hold",
        trigger_value=0,
    )

    assert result["status"] == "ok"
    assert result["cancelled"] is True


@pytest.mark.asyncio
async def test_cancel_macro_playback_interrupts_tight_toggle_loop() -> None:
    manager = DeviceManager()
    manager.output_state.keyboard_uinput = MagicMock()

    await manager.play_macro(
        macro_events=[
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_E,
                "value": 1,
                "device_type": "keyboard",
            },
            {
                "t_us": 1,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_E,
                "value": 0,
                "device_type": "keyboard",
            },
        ],
        macro_name="toggle_tight",
        loop_mode="toggle",
        source_device="dev1",
        source_button="btn1",
        trigger_value=1,
    )

    await asyncio.sleep(0.02)
    result = await asyncio.wait_for(manager.cancel_macro_playback(), timeout=0.5)

    assert result["status"] == "ok"
    assert result["cancelled"] is True


@pytest.mark.asyncio
async def test_toggle_second_press_cancels_by_source_even_if_name_differs() -> None:
    manager = DeviceManager()
    manager.output_state.keyboard_uinput = MagicMock()

    await manager.play_macro(
        macro_events=[
            {
                "t_us": 0,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_G,
                "value": 1,
                "device_type": "keyboard",
            },
            {
                "t_us": 50_000,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_G,
                "value": 0,
                "device_type": "keyboard",
            },
        ],
        macro_name="toggle_a",
        loop_mode="toggle",
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


@pytest.mark.asyncio
async def test_cancel_macro_playback_cancels_all_running_instances() -> None:
    manager = DeviceManager()
    manager.output_state.keyboard_uinput = MagicMock()

    async def start_instance(name: str, key_code: int) -> None:
        await manager.play_macro(
            macro_events=[
                {
                    "t_us": 0,
                    "type": evdev.ecodes.EV_KEY,
                    "code": key_code,
                    "value": 1,
                    "device_type": "keyboard",
                },
                {
                    "t_us": 200_000,
                    "type": evdev.ecodes.EV_KEY,
                    "code": key_code,
                    "value": 0,
                    "device_type": "keyboard",
                },
            ],
            macro_name=name,
            loop_mode="toggle",
            source_device=f"dev_{name}",
            source_button=f"btn_{name}",
            trigger_value=1,
        )

    await start_instance("a", evdev.ecodes.KEY_H)
    await start_instance("b", evdev.ecodes.KEY_I)

    await asyncio.sleep(0.02)
    assert len(mdm.running_macro_instance_ids(manager)) >= 2

    result = await manager.cancel_macro_playback()
    assert result["status"] == "ok"
    assert result["cancelled"] is True


@pytest.mark.asyncio
async def test_cancel_macro_playback_releases_tracked_outputs() -> None:
    manager = DeviceManager()
    device = MagicMock()
    manager.grabbed_devices = {"hw": [device]}

    result = await manager.cancel_macro_playback()

    assert result["status"] == "ok"
    device.release_tracked_outputs.assert_called_once()
    assert mdm.running_macro_instance_ids(manager) == []


def test_profile_macro_roundtrip_and_special_actions(temp_config_dir, monkeypatch) -> None:
    monkeypatch.setattr(paths, "SUPERKEYS_DIR", temp_config_dir / "superkeys")

    profile = ProfileConfig(
        name="Macro Profile",
        enabled=True,
        device_layers={
            "1234:5678": DeviceProfileLayer(
                hardware_id="1234:5678",
                mappings={
                    "btn_macro": MappingAction(
                        action_type=ActionType.MACRO,
                        macro_name="combo_1",
                        macro_replay_mouse_movement=False,
                        macro_replay_mouse_clicks=True,
                        macro_speed=1.25,
                    ),
                    "btn_start": MappingAction(action_type=ActionType.START_MACRO_RECORDING),
                    "btn_stop": MappingAction(action_type=ActionType.STOP_MACRO_RECORDING),
                },
            )
        },
    )

    manager = ProfileManager()
    manager.save_profile(profile)

    loaded = manager.list_profiles()[0].config
    layer = loaded.device_layers["1234:5678"]
    macro_action = layer.mappings["btn_macro"]

    assert macro_action.action_type == ActionType.MACRO
    assert macro_action.macro_name == "combo_1"
    assert macro_action.macro_replay_mouse_movement is False
    assert macro_action.macro_replay_mouse_clicks is True
    assert macro_action.macro_speed == 1.25

    assert layer.mappings["btn_start"].action_type == ActionType.START_MACRO_RECORDING
    assert layer.mappings["btn_stop"].action_type == ActionType.STOP_MACRO_RECORDING
