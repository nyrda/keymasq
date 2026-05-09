# ruff: noqa: F403, F405, I001
from tests.keymasqd.device_manager_support import *

class TestSuperkeys:
    @pytest.mark.asyncio
    async def test_mapping_reset_clears_combo_passthrough_hold_but_preserves_passthrough_release(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        decisions = [
            ComboDecision(passthrough_current_event=True, reset_candidates=True),
            None,
        ]
        mapping_state: dict[str, dm.MappingAction] = {}

        async def event_callback(*_args):
            return decisions.pop(0)

        passthrough_uinput = _FakeUInput()
        mapped_uinput = _FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"key_1": "key_1"},
            mapping_getter=lambda: mapping_state,
            event_callback=event_callback,
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=mapped_uinput,  # type: ignore[arg-type]
        )
        device._running = True
        device.uinput = passthrough_uinput  # type: ignore[assignment]

        press_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_1,
            value=1,
        )
        release_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_1,
            value=0,
        )

        await _runtime_process_grabbed_event(device, press_event)

        assert device.state.combo_passthrough_held == {"key_1"}
        assert "key_1" not in device.state.held_source_actions

        await device.reset_mapping_runtime_state()
        mapping_state["key_1"] = dm.MappingAction(
            action_type=ActionType.KEYBOARD,
            target="key_a",
        )

        assert device.state.combo_passthrough_held == set()
        assert device.state.held_source_actions["key_1"] is None

        await _runtime_process_grabbed_event(device, release_event)

        assert passthrough_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 0),
        ]
        assert mapped_uinput.writes == []
        assert "key_1" not in device.state.held_source_actions
    @pytest.mark.asyncio
    async def test_mapping_reset_clears_combo_recalled_suppression_state(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = _make_grabbed_device(monkeypatch)
        device.state.combo_passthrough_held.add("key_x")
        device.state.combo_recalled_bindings.add("key_x")

        await device.reset_mapping_runtime_state()

        assert device.state.combo_passthrough_held == set()
        assert device.state.combo_recalled_bindings == set()
    @pytest.mark.asyncio
    async def test_combo_recalled_repeat_is_suppressed_until_restore_or_new_press(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        passthrough = _FakeUInput()
        device = _make_grabbed_device(monkeypatch)
        device.uinput = passthrough  # type: ignore[assignment]
        device._running = True
        device.state.combo_passthrough_held.add("key_x")
        device.mark_combo_recalled_binding("key_x")

        repeat_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_X,
            value=2,
        )

        await _runtime_process_grabbed_event(device, repeat_event)

        assert passthrough.writes == []
        assert device.state.combo_passthrough_held == {"key_x"}
        assert device.state.combo_recalled_bindings == {"key_x"}

        device.clear_combo_recalled_binding("key_x")
        await _runtime_process_grabbed_event(device, repeat_event)

        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 2),
        ]
    @pytest.mark.asyncio
    async def test_combo_recalled_modifier_uses_normalized_name_for_suppression(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        passthrough = _FakeUInput()
        device = _make_grabbed_device(monkeypatch)
        device.uinput = passthrough  # type: ignore[assignment]
        device._running = True
        device.state.combo_passthrough_held.add("key_leftmeta")
        device.mark_combo_recalled_binding("key_leftmeta")

        repeat_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_LEFTMETA,
            value=2,
        )
        release_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_LEFTMETA,
            value=0,
        )

        await _runtime_process_grabbed_event(device, repeat_event)
        await _runtime_process_grabbed_event(device, release_event)

        assert passthrough.writes == []
        assert device.state.combo_recalled_bindings == set()
        assert device.state.combo_passthrough_held == set()
    @pytest.mark.asyncio
    async def test_combo_recalled_release_clears_suppression_without_passthrough(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        callback = AsyncMock(return_value=None)
        passthrough = _FakeUInput()
        device = _make_grabbed_device(monkeypatch)
        device.uinput = passthrough  # type: ignore[assignment]
        device._running = True
        device.event_callback = callback
        device.state.combo_passthrough_held.add("key_x")
        device.mark_combo_recalled_binding("key_x")
        device.state.held_source_actions["key_x"] = None

        release_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_X,
            value=0,
        )

        await _runtime_process_grabbed_event(device, release_event)

        assert callback.await_count == 1
        assert passthrough.writes == []
        assert device.state.combo_passthrough_held == set()
        assert device.state.combo_recalled_bindings == set()
        assert "key_x" not in device.state.held_source_actions
    @pytest.mark.asyncio
    async def test_combo_recalled_press_becomes_fresh_press_again(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        passthrough = _FakeUInput()
        device = _make_grabbed_device(monkeypatch)
        device.uinput = passthrough  # type: ignore[assignment]
        device._running = True
        device.state.combo_passthrough_held.add("key_x")
        device.mark_combo_recalled_binding("key_x")

        callback_calls = {"count": 0}

        async def event_callback(*_args):
            callback_calls["count"] += 1
            assert device.state.combo_recalled_bindings == set()
            assert device.state.combo_passthrough_held == set()
            return ComboDecision(passthrough_current_event=True)

        device.event_callback = event_callback
        press_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_X,
            value=1,
        )

        await _runtime_process_grabbed_event(device, press_event)

        assert callback_calls["count"] == 1
        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 1),
        ]
        assert device.state.combo_recalled_bindings == set()
        assert device.state.combo_passthrough_held == {"key_x"}
    @pytest.mark.asyncio
    async def test_vvv_logs_raw_hardware_events_but_skips_mouse_motion(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        passthrough = _FakeUInput()
        device = _make_grabbed_device(monkeypatch)
        device.uinput = passthrough  # type: ignore[assignment]
        device._running = True
        device.verbosity = 3

        key_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_X,
            value=2,
        )
        rel_event = SimpleNamespace(
            type=evdev.ecodes.EV_REL,
            code=evdev.ecodes.REL_X,
            value=12,
        )

        with caplog.at_level(logging.DEBUG, logger="keymasqd.devices"):
            await _runtime_process_grabbed_event(device, key_event)
            await _runtime_process_grabbed_event(device, rel_event)

        assert "[hw 1234:5678 kbd] type=1 code=45 name=key_x value=2" in caplog.text
        assert "REL_X" not in caplog.text
        assert "type=2 code=0" not in caplog.text
    @pytest.mark.asyncio
    async def test_superkey_release_after_reset_does_not_recreate_stale_machine(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "mouse")

        mapping_state = {
            "btn_side": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=SuperkeyConfig(
                    name="test",
                    tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
                ),
            )
        }

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"btn_side": "btn_side"},
            mapping_getter=lambda: mapping_state,
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.MOUSE,
            keyboard_uinput=_FakeUInput(),  # type: ignore[arg-type]
        )
        device._running = True

        press_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.BTN_SIDE,
            value=1,
        )
        release_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.BTN_SIDE,
            value=0,
        )

        await _runtime_process_grabbed_event(device, press_event)
        assert "btn_side" in device.state.superkey_machines

        await device.reset_superkeys()
        assert device.state.superkey_machines == {}

        await _runtime_process_grabbed_event(device, release_event)

        assert device.state.superkey_machines == {}
    @pytest.mark.asyncio
    async def test_shared_superkey_config_on_two_inputs_holds_output_until_both_release(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "mouse")

        shared_config = SuperkeyConfig(
            name="shared",
            hold_threshold_ms=0,
            hold_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
        )
        mapping_state = {
            "btn_side": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=shared_config,
            ),
            "btn_extra": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=shared_config,
            ),
        }

        keyboard_uinput = _FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"btn_side": "btn_side", "btn_extra": "btn_extra"},
            mapping_getter=lambda: mapping_state,
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.MOUSE,
            keyboard_uinput=keyboard_uinput,  # type: ignore[arg-type]
        )
        device._running = True

        side_press = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.BTN_SIDE,
            value=1,
        )
        extra_press = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.BTN_EXTRA,
            value=1,
        )
        side_release = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.BTN_SIDE,
            value=0,
        )
        extra_release = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.BTN_EXTRA,
            value=0,
        )

        await _runtime_process_grabbed_event(device, side_press)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(device, extra_press)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert set(device.state.superkey_machines) == {"btn_side", "btn_extra"}
        assert device.state.superkey_machines["btn_side"].state.value == "holding"
        assert device.state.superkey_machines["btn_extra"].state.value == "holding"
        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
        ]

        await _runtime_process_grabbed_event(device, side_release)
        await asyncio.sleep(0)

        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
        ]
        assert device.state.held_output_keys["keyboard"] == {evdev.ecodes.KEY_A}

        await _runtime_process_grabbed_event(device, extra_release)
        await asyncio.sleep(0)

        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]
        assert device.state.held_output_keys["keyboard"] == set()
    @pytest.mark.asyncio
    async def test_overload_superkey_fans_out_press_repeat_and_release(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        mapping_state = {
            "key_f13": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=SuperkeyConfig(
                    name="overload",
                    mode=SuperkeyMode.OVERLOAD,
                    overload_actions=[
                        dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
                        dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_b"),
                    ],
                ),
            )
        }

        keyboard_uinput = _FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"key_f13": "key_f13"},
            mapping_getter=lambda: mapping_state,
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=keyboard_uinput,  # type: ignore[arg-type]
        )
        device._running = True

        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F13, value=1),
        )
        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F13, value=2),
        )
        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F13, value=0),
        )

        assert device.state.superkey_machines == {}
        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 2),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 2),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
        ]
    @pytest.mark.asyncio
    async def test_split_overload_superkey_pulses_down_and_up_actions(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        mapping_state = {
            "key_f13": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=SuperkeyConfig(
                    name="split-overload",
                    mode=SuperkeyMode.OVERLOAD,
                    overload_actions=[
                        dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_leftctrl"),
                    ],
                    overload_down_actions=[
                        dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
                    ],
                    overload_up_actions=[
                        dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_b"),
                    ],
                ),
            )
        }

        keyboard_uinput = _FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"key_f13": "key_f13"},
            mapping_getter=lambda: mapping_state,
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=keyboard_uinput,  # type: ignore[arg-type]
        )
        device._running = True

        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F13, value=1),
        )
        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F13, value=2),
        )
        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F13, value=0),
        )

        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 2),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 0),
        ]
    @pytest.mark.asyncio
    async def test_overload_superkey_refcounts_shared_outputs_across_two_inputs(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "mouse")

        shared_config = SuperkeyConfig(
            name="overload_shared",
            mode=SuperkeyMode.OVERLOAD,
            overload_actions=[
                dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
            ],
        )
        mapping_state = {
            "btn_side": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=shared_config,
            ),
            "btn_extra": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=shared_config,
            ),
        }

        keyboard_uinput = _FakeUInput()
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"btn_side": "btn_side", "btn_extra": "btn_extra"},
            mapping_getter=lambda: mapping_state,
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.MOUSE,
            keyboard_uinput=keyboard_uinput,  # type: ignore[arg-type]
        )
        device._running = True

        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.BTN_SIDE, value=1),
        )
        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.BTN_EXTRA, value=1),
        )
        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.BTN_SIDE, value=0),
        )

        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
        ]
        assert device.state.held_output_keys["keyboard"] == {evdev.ecodes.KEY_A}

        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.BTN_EXTRA, value=0),
        )

        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]
    @pytest.mark.asyncio
    async def test_reset_mapping_runtime_state_seeds_startup_held_action_and_releases_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        mapping_state = {
            "key_f13": dm.MappingAction(
                action_type=ActionType.KEYBOARD,
                target="key_a",
            )
        }

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"key_f13": "key_f13"},
            mapping_getter=lambda: mapping_state,
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=_FakeUInput(),  # type: ignore[arg-type]
        )
        device._running = True
        device.device = SimpleNamespace(active_keys=lambda: [evdev.ecodes.KEY_F13])

        await device.reset_mapping_runtime_state()

        assert device.state.held_source_actions["key_f13"] == mapping_state["key_f13"]
        assert device.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]
    @pytest.mark.asyncio
    async def test_superkey_broadcast_does_not_block_hot_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "mouse")

        blocker = asyncio.Event()

        async def stalled_callback(_command, _data):
            await blocker.wait()

        mapping_state = {
            "btn_side": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=SuperkeyConfig(
                    name="test",
                    tap_actions=[SuperkeyActionData(action_type="exec", exec_ref=7)],
                ),
            )
        }

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"btn_side": "btn_side"},
            mapping_getter=lambda: mapping_state,
            event_callback=AsyncMock(return_value=None),
            device_type=DeviceType.MOUSE,
            keyboard_uinput=_FakeUInput(),  # type: ignore[arg-type]
            broadcast_callback=stalled_callback,
        )
        device._running = True

        press_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.BTN_SIDE,
            value=1,
        )
        release_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.BTN_SIDE,
            value=0,
        )

        await asyncio.wait_for(_runtime_process_grabbed_event(device, press_event), timeout=0.05)
        await asyncio.wait_for(
            _runtime_process_grabbed_event(device, release_event), timeout=0.05
        )

        blocker.set()
        await asyncio.sleep(0)
