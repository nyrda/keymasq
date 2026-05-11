# ruff: noqa: F403, F405, I001
from tests.keymasqd.macro_backend_support import *


async def _wait_for_no_running_macros(manager: DeviceManager) -> None:
    while mdm.running_macro_instance_ids(manager):
        await asyncio.sleep(0.005)


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

    assert calls == [(recording_module._open_recording_input_device, ("/dev/input/event0",))]


@pytest.mark.asyncio
async def test_play_macro_hold_loop_finishes_current_run_on_release_by_default() -> None:
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
    assert result["cancelled"] is False
    await asyncio.wait_for(_wait_for_no_running_macros(manager), timeout=0.5)
    assert any(
        c.args == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_D, 0)
        for c in manager.output_state.keyboard_uinput.write.call_args_list
    )


@pytest.mark.asyncio
async def test_hold_release_cancel_run_stops_immediately() -> None:
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
        loop_stop_behavior="cancel_run",
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
async def test_hold_release_finish_run_allows_pulse_trigger_to_complete_once() -> None:
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
                "t_us": 20_000,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_C,
                "value": 0,
                "device_type": "keyboard",
            },
        ],
        macro_name="pulse_hold",
        loop_mode="hold",
        source_device="dev1",
        source_button="wheel_up",
        trigger_value=1,
    )
    result = await manager.play_macro(
        macro_events=[],
        macro_name="pulse_hold",
        loop_mode="hold",
        source_device="dev1",
        source_button="wheel_up",
        trigger_value=0,
    )

    assert result["status"] == "ok"
    assert result["cancelled"] is False
    await asyncio.wait_for(_wait_for_no_running_macros(manager), timeout=0.5)
    writes = [c.args for c in manager.output_state.keyboard_uinput.write.call_args_list]
    assert writes.count((evdev.ecodes.EV_KEY, evdev.ecodes.KEY_C, 1)) == 1
    assert writes.count((evdev.ecodes.EV_KEY, evdev.ecodes.KEY_C, 0)) == 1


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
async def test_toggle_second_press_finishes_current_run_by_default() -> None:
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
    assert result["cancelled"] is False
    await asyncio.wait_for(_wait_for_no_running_macros(manager), timeout=0.5)
    assert any(
        c.args == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_G, 0)
        for c in manager.output_state.keyboard_uinput.write.call_args_list
    )


@pytest.mark.asyncio
async def test_toggle_second_press_cancel_run_stops_immediately() -> None:
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
            },
            {
                "t_us": 100_000,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_B,
                "value": 0,
                "device_type": "keyboard",
            },
        ],
        macro_name="toggle_cancel",
        loop_mode="toggle",
        loop_stop_behavior="cancel_run",
        source_device="dev1",
        source_button="btn_toggle_cancel",
        trigger_value=1,
    )

    await asyncio.sleep(0.02)
    result = await manager.play_macro(
        macro_events=[],
        macro_name="toggle_cancel",
        loop_mode="toggle",
        source_device="dev1",
        source_button="btn_toggle_cancel",
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
                        macro_loop_mode="hold",
                        macro_loop_stop_behavior="cancel_run",
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
    assert macro_action.macro_loop_mode == "hold"
    assert macro_action.macro_loop_stop_behavior == "cancel_run"

    assert layer.mappings["btn_start"].action_type == ActionType.START_MACRO_RECORDING
    assert layer.mappings["btn_stop"].action_type == ActionType.STOP_MACRO_RECORDING
