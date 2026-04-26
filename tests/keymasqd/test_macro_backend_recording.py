# ruff: noqa: F403, F405, I001
from tests.keymasqd.macro_backend_support import *

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


class _QuietInputDevice:
    def close(self) -> None:
        pass

    async def async_read_loop(self):
        while True:
            await asyncio.sleep(60)
            yield evdev.InputEvent(1, 1, evdev.ecodes.EV_SYN, 0, 0)


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
        recording_module,
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

    await _process_grabbed_event(
        _grabbed_recording_device(recorder),
        evdev.InputEvent(1, 200, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
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

    monkeypatch.setattr(recording_module.evdev, "InputDevice", fake_input_device)
    monkeypatch.setattr(
        recording_module,
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

    await _process_grabbed_event(
        _grabbed_recording_device(recorder),
        evdev.InputEvent(1, 200, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
    )
    result = await recorder.stop()

    assert opened_paths == ["/dev/input/event10"]
    assert result["event_count"] == 0


@pytest.mark.asyncio
async def test_recording_manager_keeps_unrelated_grabbed_raw_when_passthrough_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = RecordingManager()
    monkeypatch.setattr(recording_module.evdev, "InputDevice", lambda _path: _QuietInputDevice())
    monkeypatch.setattr(
        recording_module,
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

    await _process_grabbed_event(
        _grabbed_recording_device(recorder),
        evdev.InputEvent(1, 200, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
    )
    result = await recorder.stop()

    assert result["event_count"] == 1


@pytest.mark.asyncio
async def test_recording_manager_snapshot_does_not_skip_action_execution() -> None:
    class _FlakyRecordingGrabbedDevice(GrabbedDevice):
        def __init__(self, *args: object, recorder: _FakeRecorder, **kwargs: object) -> None:
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

    recorder = _FakeRecorder()
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

    await _process_grabbed_event(
        grabbed,
        evdev.InputEvent(1, 200, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
    )

    assert len(recorder.calls) == 1
    assert keyboard_uinput.write.call_args_list[0].args == (
        evdev.ecodes.EV_KEY,
        evdev.ecodes.KEY_A,
        1,
    )


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

    monkeypatch.setattr(dm.runtime_outputs, "create_global_uinputs", fake_create_global_uinputs)
    monkeypatch.setattr(dm.runtime_outputs, "destroy_global_uinputs", fake_destroy_global_uinputs)

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
async def test_hold_macro_block_mouse_movement_refreshes_suppression_until_release() -> None:
    manager = DeviceManager()
    manager.output_state.mouse_uinput = MagicMock()

    begin_mouse_rel_suppression = MagicMock()
    end_mouse_rel_suppression = MagicMock()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mdm, "begin_mouse_rel_suppression", begin_mouse_rel_suppression)
    monkeypatch.setattr(mdm, "end_mouse_rel_suppression", end_mouse_rel_suppression)

    await manager.play_macro(
        macro_events=[],
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

    await _play_macro_task(
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
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 123)
    ]


@pytest.mark.asyncio
async def test_play_macro_control_actions_shift_later_deadlines(
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

    monkeypatch.setattr(dm.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(dm.asyncio, "get_running_loop", lambda: _FakeLoop())
    monkeypatch.setattr(mdm, "run_macro_control_action", fake_run_macro_control_action)

    await _play_macro_task(
        manager,
        instance_id=1,
        macro_events=[
            {
                "t_us": 0,
                "macro_action": "wait_fixed",
                "duration_ms": 1,
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
    )

    assert sleep_calls == [pytest.approx(0.3), 0]
