# ruff: noqa: F403, F405, I001
from tests.keymasqd.device_manager_support import *


class _FakeGrabbedRecorder:
    def __init__(self, *, should_record: bool = True) -> None:
        self.is_recording = True
        self.should_record = should_record
        self.calls: list[tuple[str, evdev.InputEvent]] = []
        self.should_record_calls: list[tuple[str, list[str]]] = []

    def should_record_grabbed_event(
        self,
        device_path: str,
        device_types: list[str],
    ) -> bool:
        self.should_record_calls.append((device_path, list(device_types)))
        return self.should_record

    def record_event(self, device_type: str, event: evdev.InputEvent) -> None:
        self.calls.append((device_type, event))


class _FailingWriteUInput(_FakeUInput):
    def write(self, event_type: int, code: int, value: int) -> None:
        raise OSError("uinput disconnected")


class _CountingUInput(_FakeUInput):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.syn_count = 0

    def syn(self) -> None:
        self.syn_count += 1


class TestGrabbedDeviceHelpers:
    @pytest.mark.asyncio
    async def test_device_release_ends_held_profile_trigger_state(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events: list[tuple[CommandType, dict[str, object]]] = []
        deactivate_event = asyncio.Event()
        expected_deactivate = (
            CommandType.PROFILE_DEACTIVATE_REQUESTED,
            {
                "profile_name": "Nav",
                "activation_id": "activation-1",
                "reason": "trigger_end",
            },
        )

        async def broadcast(event_type: CommandType, data: dict[str, object]) -> None:
            event = (event_type, data)
            events.append(event)
            if event == expected_deactivate:
                deactivate_event.set()

        manager = DeviceManager(broadcast_callback=broadcast)
        device = _make_grabbed_device(
            monkeypatch,
            button_map={"caps": "key_capslock"},
            profile_activation_trigger_end_observer=manager.observe_profile_trigger_end,
        )
        manager.grabbed_devices["1234:5678"] = [device]
        device.state.held_source_keys.add("key_capslock")
        device.state.held_source_actions["key_capslock"] = dm.MappingAction(
            action_type=ActionType.PROFILE_ENABLE,
            profile_name="Nav",
        )

        manager.observe_profile_trigger_start("1234:5678:key_capslock")
        await manager.track_profile_activation(
            "Nav",
            "activation-1",
            "1234:5678:key_capslock",
            {"on_trigger_end": True},
        )

        await device.release()
        await asyncio.wait_for(deactivate_event.wait(), timeout=1.0)

        assert expected_deactivate in events
        assert "key_capslock" not in device.state.held_source_keys
        assert "key_capslock" not in device.state.held_source_actions

    @pytest.mark.asyncio
    async def test_device_release_ends_held_overload_profile_trigger_state(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events: list[tuple[CommandType, dict[str, object]]] = []
        deactivate_event = asyncio.Event()
        expected_deactivate = (
            CommandType.PROFILE_DEACTIVATE_REQUESTED,
            {
                "profile_name": "Nav",
                "activation_id": "activation-1",
                "reason": "trigger_end",
            },
        )

        async def broadcast(event_type: CommandType, data: dict[str, object]) -> None:
            event = (event_type, data)
            events.append(event)
            if event == expected_deactivate:
                deactivate_event.set()

        manager = DeviceManager(broadcast_callback=broadcast)
        device = _make_grabbed_device(
            monkeypatch,
            button_map={"f13": "key_f13"},
            profile_activation_trigger_end_observer=manager.observe_profile_trigger_end,
        )
        manager.grabbed_devices["1234:5678"] = [device]
        child_event_name = "key_f13#overload#0"
        trigger_id = f"1234:5678:{child_event_name}"
        device.state.held_profile_trigger_events.add(child_event_name)

        manager.observe_profile_trigger_start(trigger_id)
        await manager.track_profile_activation(
            "Nav",
            "activation-1",
            trigger_id,
            {"on_trigger_end": True},
        )

        await device.release()
        await asyncio.wait_for(deactivate_event.wait(), timeout=1.0)

        assert expected_deactivate in events
        assert device.state.held_profile_trigger_events == set()

    @pytest.mark.asyncio
    async def test_consumed_release_clears_held_profile_trigger_state(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        action = dm.MappingAction(
            action_type=ActionType.PROFILE_ENABLE,
            profile_name="Nav",
        )

        async def consume_release(*args: object) -> bool:
            return int(cast(int, args[4])) == 0

        starts: list[str | None] = []
        ends: list[str | None] = []
        device = _make_grabbed_device(
            monkeypatch,
            button_map={"caps": "key_capslock"},
            profile_activation_trigger_start_observer=starts.append,
            profile_activation_trigger_end_observer=ends.append,
        )
        device.mapping_getter = lambda: {"caps": action}
        device.event_callback = AsyncMock(side_effect=consume_release)

        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_CAPSLOCK, 1),
        )
        assert "key_capslock" in device.state.held_source_keys
        assert "key_capslock" in device.state.held_source_actions

        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_CAPSLOCK, 0),
        )

        assert starts == ["1234:5678:key_capslock"]
        assert ends == ["1234:5678:key_capslock"]
        assert "key_capslock" not in device.state.held_source_keys
        assert "key_capslock" not in device.state.held_source_actions

    @pytest.mark.asyncio
    async def test_unmapped_grabbed_key_press_consumes_profile_action_count(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events: list[tuple[CommandType, dict[str, object]]] = []
        deactivate_event = asyncio.Event()
        expected_deactivate = (
            CommandType.PROFILE_DEACTIVATE_REQUESTED,
            {
                "profile_name": "Nav",
                "activation_id": "activation-1",
                "reason": "action_count",
            },
        )

        async def broadcast(event_type: CommandType, data: dict[str, object]) -> None:
            event = (event_type, data)
            events.append(event)
            if event == expected_deactivate:
                deactivate_event.set()

        manager = DeviceManager(broadcast_callback=broadcast)
        passthrough = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            profile_activation_recorder=manager.record_profile_action,
        )
        device.uinput = passthrough  # type: ignore[assignment]

        await manager.track_profile_activation(
            "Nav",
            "activation-1",
            "1234:5678:key_capslock",
            {"after_actions": 1},
        )

        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
        )
        await asyncio.wait_for(deactivate_event.wait(), timeout=1.0)

        assert expected_deactivate in events
        assert passthrough.writes == [(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)]

    @pytest.mark.asyncio
    async def test_inspector_suppressed_key_esc_press_disables_suppression(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events: list[dict[str, object]] = []
        diagnostics: list[str] = []
        disable_suppression = AsyncMock(
            return_value={"status": "ok", "suppressed": False, "reason": "key_esc"}
        )
        passthrough = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            inspector_event_callback=events.append,
            inspector_active_getter=lambda _hardware_id: True,
            inspector_suppression_getter=lambda _hardware_id: True,
            inspector_suppression_disabler=disable_suppression,
            diagnostics_recorder=lambda label, _duration_us: diagnostics.append(label),
        )
        device.uinput = passthrough  # type: ignore[assignment]

        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_ESC, 1),
        )

        disable_suppression.assert_awaited_once_with("1234:5678", "key_esc")
        assert events[0]["code_name"] == "key_esc"
        assert events[0]["suppressed"] is True
        assert passthrough.writes == []
        assert diagnostics == ["inspector_escape_key"]
        cast(AsyncMock, device.event_callback).assert_not_awaited()

    @pytest.mark.asyncio
    async def test_inspector_key_esc_press_disables_other_suppressed_device(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        diagnostics: list[str] = []
        disable_suppression = AsyncMock(return_value={"status": "ok"})
        passthrough = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            inspector_active_getter=lambda _hardware_id: False,
            inspector_suppression_getter=lambda _hardware_id: False,
            inspector_suppressed_ids_getter=lambda: {"mouse-hid"},
            inspector_suppression_disabler=disable_suppression,
            diagnostics_recorder=lambda label, _duration_us: diagnostics.append(label),
        )
        device.uinput = passthrough  # type: ignore[assignment]

        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_ESC, 1),
        )

        disable_suppression.assert_awaited_once_with("mouse-hid", "key_esc")
        assert passthrough.writes == []
        assert diagnostics == ["inspector_escape_key"]
        cast(AsyncMock, device.event_callback).assert_not_awaited()

    @pytest.mark.asyncio
    async def test_inspector_suppressed_non_escape_and_escape_release_do_not_disable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events: list[dict[str, object]] = []
        diagnostics: list[str] = []
        disable_suppression = AsyncMock(return_value={"status": "ok"})
        passthrough = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            inspector_event_callback=events.append,
            inspector_active_getter=lambda _hardware_id: True,
            inspector_suppression_getter=lambda _hardware_id: True,
            inspector_suppression_disabler=disable_suppression,
            diagnostics_recorder=lambda label, _duration_us: diagnostics.append(label),
        )
        device.uinput = passthrough  # type: ignore[assignment]

        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
        )
        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_ESC, 0),
        )

        disable_suppression.assert_not_awaited()
        assert [event["code_name"] for event in events] == ["key_a", "key_esc"]
        assert [event["suppressed"] for event in events] == [True, True]
        assert passthrough.writes == []
        assert diagnostics == ["inspector_suppressed", "inspector_suppressed"]
        cast(AsyncMock, device.event_callback).assert_not_awaited()

    def test_find_action_for_event_prefers_evdev_code_over_alias_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = _make_grabbed_device(
            monkeypatch,
            button_map={"south": "btn_south"},
            button_codes={"south": evdev.ecodes.BTN_SOUTH},
        )
        mapping = {"south": MappingAction(action_type=ActionType.KEYBOARD, target="key_a")}
        event = evdev.InputEvent(
            0,
            0,
            evdev.ecodes.EV_KEY,
            evdev.ecodes.BTN_SOUTH,
            1,
        )

        assert _runtime_find_grabbed_action_for_event(device, event, mapping) == mapping["south"]

    @pytest.mark.asyncio
    async def test_passthrough_preserves_source_syn_report_frame(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        passthrough = _CountingUInput()
        diagnostics: list[tuple[str, float]] = []
        device = _make_grabbed_device(
            monkeypatch,
            diagnostics_recorder=lambda label, duration_us: diagnostics.append(
                (label, duration_us)
            ),
        )
        device.uinput = passthrough  # type: ignore[assignment]

        deps = gde.build_event_processing_deps(log=logging.getLogger("test"))

        await gde.process_event(
            device,
            evdev.InputEvent(0, 0, evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 10),
            deps=deps,
        )
        syn_mt_report = getattr(evdev.ecodes, "SYN_MT_REPORT", None)
        if syn_mt_report is None:
            pytest.skip("evdev does not expose SYN_MT_REPORT")
        await gde.process_event(
            device,
            evdev.InputEvent(0, 0, evdev.ecodes.EV_SYN, int(syn_mt_report), 0),
            deps=deps,
        )
        assert gdo.passthrough_frame_open(passthrough)
        assert passthrough.syn_count == 0

        await gde.process_event(
            device,
            evdev.InputEvent(0, 0, evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Y, 20),
            deps=deps,
        )

        assert passthrough.writes == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 10),
            (evdev.ecodes.EV_SYN, int(syn_mt_report), 0),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Y, 20),
        ]
        assert passthrough.syn_count == 0
        assert gdo.passthrough_frame_open(passthrough)

        await gde.process_event(
            device,
            evdev.InputEvent(0, 0, evdev.ecodes.EV_SYN, evdev.ecodes.SYN_REPORT, 0),
            deps=deps,
        )

        assert passthrough.syn_count == 1
        assert not gdo.passthrough_frame_open(passthrough)
        assert [label for label, _duration_us in diagnostics] == [
            "passthrough_other",
            "syn",
            "passthrough_other",
            "passthrough_syn",
        ]

    @pytest.mark.asyncio
    async def test_passthrough_syn_mt_report_opens_frame_for_empty_contact_update(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        syn_mt_report = getattr(evdev.ecodes, "SYN_MT_REPORT", None)
        if syn_mt_report is None:
            pytest.skip("evdev does not expose SYN_MT_REPORT")

        passthrough = _CountingUInput()
        device = _make_grabbed_device(monkeypatch)
        device.uinput = passthrough  # type: ignore[assignment]
        deps = gde.build_event_processing_deps(log=logging.getLogger("test"))

        await gde.process_event(
            device,
            evdev.InputEvent(0, 0, evdev.ecodes.EV_SYN, int(syn_mt_report), 0),
            deps=deps,
        )

        assert passthrough.writes == [(evdev.ecodes.EV_SYN, int(syn_mt_report), 0)]
        assert passthrough.syn_count == 0
        assert gdo.passthrough_frame_open(passthrough)

        await gde.process_event(
            device,
            evdev.InputEvent(0, 0, evdev.ecodes.EV_SYN, evdev.ecodes.SYN_REPORT, 0),
            deps=deps,
        )

        assert passthrough.syn_count == 1
        assert not gdo.passthrough_frame_open(passthrough)

    @pytest.mark.asyncio
    async def test_generated_gamepad_output_defers_syn_inside_passthrough_frame(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        target_uinput = _CountingUInput()

        def resolve_gamepad_output(_output_id: str | None, _context: str) -> SimpleNamespace:
            return SimpleNamespace(uinput=target_uinput, bucket="gamepad:target")

        device = _make_grabbed_device(
            monkeypatch,
            gamepad_output_resolver=resolve_gamepad_output,
        )
        action = MappingAction(
            action_type=ActionType.GAMEPAD_AXIS,
            target="abs_x",
            axis_value=32767,
            output_id="target",
        )
        event = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 1)

        gdo.mark_passthrough_frame_open(target_uinput)
        await gda.execute_action(
            device,
            action,
            event,
            "btn_south",
            deps=gde.build_action_execution_deps(),
        )

        assert target_uinput.writes == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 32767)
        ]
        assert target_uinput.syn_count == 0

        gdo.flush_passthrough_frame(
            target_uinput,
            uinput_writer=gdt.identity_uinput_writer,
        )

        assert target_uinput.syn_count == 1
        assert not gdo.passthrough_frame_open(target_uinput)
    def test_device_has_mapped_buttons_matches_by_code_when_names_differ(self) -> None:
        caps = {
            evdev.ecodes.EV_KEY: [evdev.ecodes.BTN_SOUTH],
        }

        assert _runtime_device_has_mapped_buttons(
            caps,
            {"btn_south"},
            {(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH)},
        )
    def test_device_has_mapped_buttons_ignores_cross_type_code_collision(self) -> None:
        caps = {
            evdev.ecodes.EV_REL: [evdev.ecodes.REL_WHEEL],
        }

        assert not _runtime_device_has_mapped_buttons(
            caps,
            {"key_7"},
            {(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_7)},
        )

    def test_analog_axis_bindings_filter_by_interface_source(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = _make_grabbed_device(
            monkeypatch,
            analog_inputs={
                "left_stick": {
                    "source": "kbd",
                    "axes": [{"role": "x", "evdev": "abs_x", "evdev_code": evdev.ecodes.ABS_X}],
                },
                "right_stick": {
                    "source": "mouse",
                    "axes": [{"role": "x", "evdev": "abs_x", "evdev_code": evdev.ecodes.ABS_X}],
                },
            },
        )

        assert device.analog_axis_bindings == {
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X): ("left_stick", "x")
        }

    def test_analog_input_bindings_filter_by_source_for_grab_eligibility(self) -> None:
        analog_inputs = {
            "left_stick": {
                "source": "kbd",
                "axes": [{"role": "x", "evdev": "abs_x", "evdev_code": evdev.ecodes.ABS_X}],
            },
            "right_stick": {
                "source": "mouse",
                "axes": [{"role": "x", "evdev": "abs_y", "evdev_code": evdev.ecodes.ABS_Y}],
            },
            "pedal": {
                "axes": [{"role": "x", "evdev": "abs_z", "evdev_code": evdev.ecodes.ABS_Z}],
            },
        }

        assert ldm.analog_input_bindings(analog_inputs, source="kbd") == {
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z),
        }
        assert ldm.analog_input_bindings(analog_inputs, source="mouse") == {
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Y),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z),
        }

    def test_refresh_analog_axis_ranges_does_not_infer_stick_center(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = _make_grabbed_device(
            monkeypatch,
            analog_inputs={
                "left_stick": {
                    "type": "stick",
                    "source": "kbd",
                    "axes": [{"role": "x", "evdev": "abs_x", "evdev_code": evdev.ecodes.ABS_X}],
                },
            },
        )
        device.device = SimpleNamespace(
            absinfo=lambda _code: SimpleNamespace(min=0, max=65535, value=12345)
        )

        device._refresh_analog_axis_ranges()

        assert device.analog_axis_ranges[("left_stick", "x")] == (0, 65535)
        assert device.analog_axis_calibrations[("left_stick", "x")] == {
            "minimum": 0,
            "maximum": 65535,
        }

    def test_refresh_analog_axis_ranges_infers_axis_rest(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = _make_grabbed_device(
            monkeypatch,
            analog_inputs={
                "left_trigger": {
                    "type": "axis",
                    "source": "kbd",
                    "axes": [{"role": "x", "evdev": "abs_z", "evdev_code": evdev.ecodes.ABS_Z}],
                },
            },
        )
        device.device = SimpleNamespace(
            absinfo=lambda _code: SimpleNamespace(min=0, max=1023, value=127)
        )

        device._refresh_analog_axis_ranges()

        assert device.analog_axis_calibrations[("left_trigger", "x")] == {
            "minimum": 0,
            "maximum": 1023,
            "rest": 127,
        }

    def test_find_grabbed_action_for_event_ignores_cross_type_code_collision(
        self,
        monkeypatch,
    ) -> None:
        device = _make_grabbed_device(
            monkeypatch,
            button_map={
                "extra_13": "key_7",
                "wheel_up": "rel_wheel",
                "wheel_down": "rel_wheel",
            },
            button_codes={
                "extra_13": evdev.ecodes.KEY_7,
                "wheel_up": evdev.ecodes.REL_WHEEL,
                "wheel_down": evdev.ecodes.REL_WHEEL,
            },
            button_values={"wheel_up": 1, "wheel_down": -1},
        )
        mapping = {"extra_13": MappingAction(action_type=ActionType.KEYBOARD, target="key_a")}
        event = evdev.InputEvent(
            0,
            0,
            evdev.ecodes.EV_REL,
            evdev.ecodes.REL_WHEEL,
            -1,
        )

        assert _runtime_find_grabbed_action_for_event(device, event, mapping) is None
    def test_find_grabbed_action_for_event_distinguishes_wheel_direction(self, monkeypatch) -> None:
        device = _make_grabbed_device(
            monkeypatch,
            button_map={
                "wheel_up": "rel_wheel",
                "wheel_down": "rel_wheel",
            },
            button_codes={
                "wheel_up": evdev.ecodes.REL_WHEEL,
                "wheel_down": evdev.ecodes.REL_WHEEL,
            },
            button_values={"wheel_up": 1, "wheel_down": -1},
        )
        mapping = {
            "wheel_down": MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
        }
        down_event = evdev.InputEvent(
            0,
            0,
            evdev.ecodes.EV_REL,
            evdev.ecodes.REL_WHEEL,
            -1,
        )
        up_event = evdev.InputEvent(
            0,
            0,
            evdev.ecodes.EV_REL,
            evdev.ecodes.REL_WHEEL,
            1,
        )

        assert _runtime_find_grabbed_action_for_event(
            device,
            down_event,
            mapping,
        ) == mapping["wheel_down"]
        assert _runtime_find_grabbed_action_for_event(device, up_event, mapping) is None

    @pytest.mark.asyncio
    async def test_process_grabbed_wheel_event_executes_mapped_action_as_pulse(
        self,
        monkeypatch,
    ) -> None:
        keyboard = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            button_map={
                "wheel_up": "rel_wheel",
                "wheel_down": "rel_wheel",
            },
            button_codes={
                "wheel_up": evdev.ecodes.REL_WHEEL,
                "wheel_down": evdev.ecodes.REL_WHEEL,
            },
            button_values={"wheel_up": 1, "wheel_down": -1},
            keyboard_uinput=keyboard,  # type: ignore[arg-type]
        )
        mapping = {
            "wheel_down": MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
            "wheel_up": MappingAction(action_type=ActionType.KEYBOARD, target="key_b"),
        }
        device.mapping_getter = lambda: mapping  # type: ignore[method-assign]

        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(
                0,
                0,
                evdev.ecodes.EV_REL,
                evdev.ecodes.REL_WHEEL,
                -1,
            ),
        )
        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(
                0,
                0,
                evdev.ecodes.EV_REL,
                evdev.ecodes.REL_WHEEL,
                1,
            ),
        )

        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
        ]

    @pytest.mark.asyncio
    async def test_process_grabbed_wheel_passthrough_and_suppress(
        self,
        monkeypatch,
    ) -> None:
        passthrough = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            button_map={
                "wheel_up": "rel_wheel",
                "wheel_down": "rel_wheel",
            },
            button_codes={
                "wheel_up": evdev.ecodes.REL_WHEEL,
                "wheel_down": evdev.ecodes.REL_WHEEL,
            },
            button_values={"wheel_up": 1, "wheel_down": -1},
        )
        device.uinput = passthrough  # type: ignore[assignment]
        device.mapping_getter = lambda: {  # type: ignore[method-assign]
            "wheel_up": MappingAction(action_type=ActionType.PASSTHROUGH),
            "wheel_down": MappingAction(action_type=ActionType.SUPPRESS),
        }

        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(
                0,
                0,
                evdev.ecodes.EV_REL,
                evdev.ecodes.REL_WHEEL,
                1,
            ),
        )
        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(
                0,
                0,
                evdev.ecodes.EV_REL,
                evdev.ecodes.REL_WHEEL,
                -1,
            ),
        )

        assert passthrough.writes == [(evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL, 1)]

    @pytest.mark.asyncio
    async def test_process_grabbed_wheel_mapping_suppresses_only_matching_high_res_direction(
        self,
        monkeypatch,
    ) -> None:
        passthrough = _FakeUInput()
        keyboard = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            button_map={
                "wheel_up": "rel_wheel",
                "wheel_down": "rel_wheel",
            },
            button_codes={
                "wheel_up": evdev.ecodes.REL_WHEEL,
                "wheel_down": evdev.ecodes.REL_WHEEL,
            },
            button_values={"wheel_up": 1, "wheel_down": -1},
            keyboard_uinput=keyboard,  # type: ignore[arg-type]
        )
        device.uinput = passthrough  # type: ignore[assignment]
        device.mapping_getter = lambda: {  # type: ignore[method-assign]
            "wheel_down": MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
        }

        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(
                0,
                0,
                evdev.ecodes.EV_REL,
                evdev.ecodes.REL_WHEEL_HI_RES,
                -120,
            ),
        )

        assert keyboard.writes == []
        assert passthrough.writes == []

        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(
                0,
                0,
                evdev.ecodes.EV_REL,
                evdev.ecodes.REL_WHEEL_HI_RES,
                120,
            ),
        )

        assert keyboard.writes == []
        assert passthrough.writes == [
            (evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL_HI_RES, 120),
        ]

    @pytest.mark.asyncio
    async def test_process_grabbed_wheel_mapping_records_original_event(
        self,
        monkeypatch,
    ) -> None:
        recorder = _FakeGrabbedRecorder()
        keyboard = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            button_map={
                "wheel_up": "rel_wheel",
                "wheel_down": "rel_wheel",
            },
            button_codes={
                "wheel_up": evdev.ecodes.REL_WHEEL,
                "wheel_down": evdev.ecodes.REL_WHEEL,
            },
            button_values={"wheel_up": 1, "wheel_down": -1},
            device_type=DeviceType.MOUSE,
            keyboard_uinput=keyboard,  # type: ignore[arg-type]
            recording_manager=recorder,
        )
        device.mapping_getter = lambda: {  # type: ignore[method-assign]
            "wheel_up": MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
            "wheel_down": MappingAction(action_type=ActionType.SUPPRESS),
        }

        up_event = evdev.InputEvent(
            0,
            0,
            evdev.ecodes.EV_REL,
            evdev.ecodes.REL_WHEEL,
            1,
        )
        down_event = evdev.InputEvent(
            0,
            1,
            evdev.ecodes.EV_REL,
            evdev.ecodes.REL_WHEEL,
            -1,
        )
        await _runtime_process_grabbed_event(device, up_event)
        await _runtime_process_grabbed_event(device, down_event)

        recorded_events = [
            (device_type, event.code, event.value)
            for device_type, event in recorder.calls
        ]
        assert recorded_events == [
            ("mouse", evdev.ecodes.REL_WHEEL, 1),
            ("mouse", evdev.ecodes.REL_WHEEL, -1),
        ]
        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]

    @pytest.mark.asyncio
    async def test_process_grabbed_mapped_wheel_records_suppressed_high_res_event(
        self,
        monkeypatch,
    ) -> None:
        recorder = _FakeGrabbedRecorder()
        passthrough = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            button_map={"wheel_down": "rel_wheel"},
            button_codes={"wheel_down": evdev.ecodes.REL_WHEEL},
            button_values={"wheel_down": -1},
            device_type=DeviceType.MOUSE,
            recording_manager=recorder,
        )
        device.uinput = passthrough  # type: ignore[assignment]
        device.mapping_getter = lambda: {  # type: ignore[method-assign]
            "wheel_down": MappingAction(action_type=ActionType.SUPPRESS),
        }

        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(
                0,
                0,
                evdev.ecodes.EV_REL,
                evdev.ecodes.REL_WHEEL_HI_RES,
                -120,
            ),
        )

        assert [
            (device_type, event.code, event.value)
            for device_type, event in recorder.calls
        ] == [
            ("mouse", evdev.ecodes.REL_WHEEL_HI_RES, -120),
        ]
        assert passthrough.writes == []

    @pytest.mark.asyncio
    async def test_process_grabbed_wheel_passthrough_records_once(
        self,
        monkeypatch,
    ) -> None:
        recorder = _FakeGrabbedRecorder()
        passthrough = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            button_map={"wheel_up": "rel_wheel"},
            button_codes={"wheel_up": evdev.ecodes.REL_WHEEL},
            button_values={"wheel_up": 1},
            device_type=DeviceType.MOUSE,
            recording_manager=recorder,
        )
        device.uinput = passthrough  # type: ignore[assignment]
        device.mapping_getter = lambda: {  # type: ignore[method-assign]
            "wheel_up": MappingAction(action_type=ActionType.PASSTHROUGH),
        }

        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(
                0,
                0,
                evdev.ecodes.EV_REL,
                evdev.ecodes.REL_WHEEL,
                1,
            ),
        )

        recorded_events = [
            (device_type, event.code, event.value)
            for device_type, event in recorder.calls
        ]
        assert recorded_events == [
            ("mouse", evdev.ecodes.REL_WHEEL, 1),
        ]
        assert list(device.repeat_state.history) == []
        assert passthrough.writes == [(evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL, 1)]

    @pytest.mark.asyncio
    async def test_explicit_passthrough_mapping_does_not_update_repeat_history(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        passthrough = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            button_map={"key_a": "key_a"},
        )
        device.uinput = passthrough  # type: ignore[assignment]
        device.mapping_getter = lambda: {  # type: ignore[method-assign]
            "key_a": MappingAction(action_type=ActionType.PASSTHROUGH),
        }

        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(
                0,
                0,
                evdev.ecodes.EV_KEY,
                evdev.ecodes.KEY_A,
                1,
            ),
        )

        assert passthrough.writes == [(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)]
        assert list(device.repeat_state.history) == []

    @pytest.mark.asyncio
    async def test_process_grabbed_high_res_wheel_passthrough_when_unmapped_or_explicit(
        self,
        monkeypatch,
    ) -> None:
        passthrough = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            button_map={"wheel_down": "rel_wheel"},
            button_codes={"wheel_down": evdev.ecodes.REL_WHEEL},
            button_values={"wheel_down": -1},
        )
        device.uinput = passthrough  # type: ignore[assignment]

        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(
                0,
                0,
                evdev.ecodes.EV_REL,
                evdev.ecodes.REL_WHEEL_HI_RES,
                -120,
            ),
        )
        assert len(device.repeat_state.history) == 1
        device.repeat_state.history.clear()

        device.mapping_getter = lambda: {  # type: ignore[method-assign]
            "wheel_down": MappingAction(action_type=ActionType.PASSTHROUGH),
        }
        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(
                0,
                0,
                evdev.ecodes.EV_REL,
                evdev.ecodes.REL_WHEEL_HI_RES,
                -120,
            ),
        )

        assert passthrough.writes == [
            (evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL_HI_RES, -120),
            (evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL_HI_RES, -120),
        ]
        assert list(device.repeat_state.history) == []

    def test_bucket_tracking_and_release_all_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        device = _make_grabbed_device(monkeypatch)
        passthrough = _FakeUInput()
        keyboard = _FakeUInput()
        mouse = _FakeUInput()
        gamepad = _FakeUInput()
        second_gamepad = _FakeUInput()
        canceled = Mock()
        task = SimpleNamespace(done=lambda: False, cancel=canceled)

        device.uinput = passthrough  # type: ignore[assignment]
        device.keyboard_uinput = keyboard  # type: ignore[assignment]
        device.mouse_uinput = mouse  # type: ignore[assignment]
        device.gamepad_uinput = gamepad  # type: ignore[assignment]
        def resolve_gamepad_output(output_id, context):
            output = second_gamepad if output_id == "virtual-gamepad-2" else gamepad
            return SimpleNamespace(
                output_id=output_id,
                uinput=output,
                bucket=f"gamepad:{output_id}",
                is_virtual=True,
            )

        device._gamepad_output_resolver = resolve_gamepad_output  # type: ignore[method-assign, reportPrivateUsage]
        gdo.track_key_state(device, device.uinput, evdev.ecodes.KEY_A, 1)
        gdo.track_key_state(device, device.keyboard_uinput, evdev.ecodes.KEY_B, 1)
        gdo.track_key_state(device, device.mouse_uinput, evdev.ecodes.BTN_LEFT, 1)
        gdo.track_key_state(
            device,
            gamepad,
            evdev.ecodes.BTN_EAST,
            1,
            bucket="gamepad:virtual-gamepad-1",
        )
        gdo.track_abs_state(
            device,
            evdev.ecodes.ABS_Z,
            255,
            bucket="gamepad:virtual-gamepad-2",
        )
        gdo.track_superkey_abs_output(
            device,
            "gamepad",
            evdev.ecodes.ABS_RZ,
            255,
        )
        gdo.track_superkey_output(device, "gamepad", evdev.ecodes.BTN_SOUTH, 1)
        device.state.rapidfire_tasks["btn_side"] = task  # type: ignore[assignment]
        device.state.rapidfire_outputs["btn_side"] = gdt.RapidfireOutputState(kind="key")
        device.state.rapidfire_active["btn_side"] = True
        device.state.tap_active["btn_side"] = True
        device.state.combo_passthrough_held.add("btn_side")
        device.state.combo_recalled_bindings.add("btn_side")
        device.state.held_source_actions["btn_side"] = None

        assert gdo.bucket_for_uinput(device, device.keyboard_uinput) == "keyboard"

        gdo.release_all_keys(
            device,
            evdev_mod=evdev,
            uinput_writer=lambda device: cast(gdt.WritableUInput | None, device),
        )

        assert passthrough.writes == [(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0)]
        assert keyboard.writes == [(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0)]
        assert mouse.writes == [(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 0)]
        assert (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_EAST, 0) in gamepad.writes
        assert (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RZ, 0) in gamepad.writes
        assert second_gamepad.writes == [(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0)]
        assert device.state.superkey_abs_refcounts["gamepad"] == {}
        assert device.state.held_output_abs["gamepad:virtual-gamepad-2"] == set()
        canceled.assert_called_once()
        assert device.state.rapidfire_tasks == {}
        assert device.state.tap_active == {}
        assert device.state.combo_recalled_bindings == set()
        assert device.state.held_source_actions == {}
    def test_release_helpers_log_failed_uinput_releases(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        device = _make_grabbed_device(monkeypatch)
        keyboard = _FailingWriteUInput()
        gamepad = _FailingWriteUInput()
        device.keyboard_uinput = keyboard  # type: ignore[assignment]
        device.gamepad_uinput = gamepad  # type: ignore[assignment]

        with caplog.at_level(logging.DEBUG, logger="keymasqd.devices"):
            gdo.ensure_key_released(device, evdev.ecodes.KEY_A, device.keyboard_uinput)
            gdo.ensure_abs_axis_released(
                device,
                evdev.ecodes.ABS_Z,
                evdev_mod=evdev,
                uinput_writer=lambda device: cast(gdt.WritableUInput | None, device),
            )

        assert "Failed to release output key" in caplog.text
        assert "Failed to release gamepad ABS axis" in caplog.text
    def test_release_all_keys_keeps_tracking_after_failed_release(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        device = _make_grabbed_device(monkeypatch)
        keyboard = _FailingWriteUInput()
        device.keyboard_uinput = keyboard  # type: ignore[assignment]
        device.state.held_output_keys["keyboard"].add(evdev.ecodes.KEY_A)
        device.state.superkey_output_refcounts["keyboard"][evdev.ecodes.KEY_A] = 1

        with caplog.at_level(logging.DEBUG, logger="keymasqd.devices"):
            gdo.release_all_keys(
                device,
                evdev_mod=evdev,
                uinput_writer=lambda device: cast(gdt.WritableUInput | None, device),
            )

        assert device.state.held_output_keys["keyboard"] == {evdev.ecodes.KEY_A}
        assert device.state.superkey_output_refcounts["keyboard"] == {
            evdev.ecodes.KEY_A: 1
        }
        assert "Failed to release held output keys" in caplog.text
    @pytest.mark.asyncio
    async def test_wait_for_active_key_activity_handles_timeouts_and_drain_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        device = _make_grabbed_device(monkeypatch)

        class _FakeLoop:
            def add_reader(self, _fd: int, _callback) -> None:
                return

            def remove_reader(self, _fd: int) -> None:
                return

        class _FakeInputDevice:
            def __init__(self) -> None:
                self.calls = 0

            def fileno(self) -> int:
                return 4

            def read_one(self):
                self.calls += 1
                if self.calls == 1:
                    raise OSError(errno.EIO, "drain failed")
                return None

        fake_input = _FakeInputDevice()
        device.device = fake_input  # type: ignore[assignment]

        monkeypatch.setattr(gdm.asyncio, "get_running_loop", lambda: _FakeLoop())

        outcomes = iter([TimeoutError(), None])

        async def fake_wait_for(awaitable, _timeout: float):
            close = getattr(awaitable, "close", None)
            if close is not None:
                close()
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(gdm.asyncio, "wait_for", fake_wait_for)

        assert await _runtime_wait_for_grabbed_active_key_activity(device, 0.1) is False

        with caplog.at_level(logging.WARNING, logger="keymasqd.devices"):
            assert await _runtime_wait_for_grabbed_active_key_activity(device, 0.1) is True

        assert "failed to drain pending events before grab" in caplog.text
    @pytest.mark.asyncio
    async def test_broadcast_grab_status_and_startup_held_actions(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        callback = AsyncMock(side_effect=RuntimeError("boom"))
        keyboard = _FakeUInput()
        mouse = _FakeUInput()
        gamepad = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            button_map={"left": "key_a", "right": "key_b"},
            broadcast_callback=callback,
            keyboard_uinput=keyboard,  # type: ignore[arg-type]
            mouse_uinput=mouse,  # type: ignore[arg-type]
            gamepad_uinput=gamepad,  # type: ignore[arg-type]
        )

        class _FakeInputDevice:
            def active_keys(self) -> list[int]:
                return [evdev.ecodes.KEY_A, evdev.ecodes.KEY_B]

        device.device = _FakeInputDevice()  # type: ignore[assignment]
        mapping_state = {
            "left": dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_z"),
            "right": dm.MappingAction(action_type=ActionType.MOUSE, target="btn_left"),
        }
        device.mapping_getter = lambda: mapping_state

        with caplog.at_level(logging.WARNING, logger="keymasqd.devices"):
            await gdg.broadcast_grab_status(
                device,
                "waiting",
                ["key_a"],
                waited_s=1.5,
                log=gdm.log,
            )

        gdg.seed_startup_held_actions(device)

        callback.assert_awaited_once()
        assert "Failed to broadcast grab status" in caplog.text
        assert device.state.held_source_actions["key_a"] == mapping_state["left"]
        assert device.state.held_source_actions["key_b"] == mapping_state["right"]
        assert keyboard.writes == [(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_Z, 0)]
        assert mouse.writes == [(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 0)]
    def test_seed_startup_held_actions_matches_gamepad_alias_by_code(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = _make_grabbed_device(
            monkeypatch,
            button_map={"south": "btn_south"},
            button_codes={"south": evdev.ecodes.BTN_SOUTH},
        )

        class _FakeInputDevice:
            def active_keys(self) -> list[int]:
                return [evdev.ecodes.BTN_SOUTH]

        device.device = _FakeInputDevice()  # type: ignore[assignment]
        mapping_state = {
            "south": dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_z"),
        }
        device.mapping_getter = lambda: mapping_state

        gdg.seed_startup_held_actions(device)

        assert device.state.held_source_actions["btn_a"] == mapping_state["south"]
    def test_reconcile_startup_held_action_releases_gamepad_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = _make_grabbed_device(monkeypatch)
        gamepad = _FakeUInput()
        device.gamepad_uinput = gamepad  # type: ignore[assignment]

        gdg.reconcile_startup_held_action(
            device,
            dm.MappingAction(
                action_type=ActionType.GAMEPAD_AXIS,
                target="abs_z",
                axis_value=255,
            )
            ,
        )
        gdg.reconcile_startup_held_action(
            device,
            dm.MappingAction(action_type=ActionType.GAMEPAD, target="btn_south")
            ,
        )

        assert gamepad.writes == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 0),
        ]
    @pytest.mark.asyncio
    async def test_tap_helpers_and_emit_combo_release_cover_cleanup_paths(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = _make_grabbed_device(monkeypatch)
        passthrough = _FakeUInput()
        gamepad = _FakeUInput()
        device.uinput = passthrough  # type: ignore[assignment]
        device.gamepad_uinput = gamepad  # type: ignore[assignment]

        monkeypatch.setattr(gdm.asyncio, "sleep", AsyncMock())

        await _runtime_tap_grabbed_key(
            device,
            evdev.ecodes.KEY_A,
            25,
            "tap",
            device.keyboard_uinput,  # type: ignore[arg-type]
        )
        device.state.tap_active["trigger"] = True
        await _runtime_tap_grabbed_axis(device, evdev.ecodes.ABS_Z, 25, "trigger")
        move_action = dm.MappingAction(
            action_type=ActionType.MOUSE_MOVE_REL,
            move_x=4,
            move_y=-3,
        )
        device.state.tap_active["move"] = True
        await _runtime_tap_grabbed_move(device, move_action, "move", 25)
        device.emit_combo_release("key_b")
        device.emit_combo_release("missing")

        assert device.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]
        assert gamepad.writes == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 255),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0),
        ]
        assert passthrough.writes == [(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0)]
        assert device.state.tap_active == {}
    def test_emit_combo_press_reestablishes_passthrough_hold_tracking(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = _make_grabbed_device(monkeypatch)
        passthrough = _FakeUInput()
        device.uinput = passthrough  # type: ignore[assignment]

        device.emit_combo_press("key_b")

        assert passthrough.writes == [(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1)]
        assert device.state.combo_passthrough_held == {"key_b"}
        assert device.state.held_output_keys["passthrough"] == {evdev.ecodes.KEY_B}
    @pytest.mark.asyncio
    async def test_execute_action_covers_synthetic_non_keyboard_branches(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        callback = AsyncMock()
        macro_player = AsyncMock(return_value={"status": "ok"})
        emergency_resetter = AsyncMock(return_value={"status": "ok", "reset": True})
        mouse = _FakeUInput()
        gamepad = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            broadcast_callback=callback,
            macro_player=macro_player,
            emergency_resetter=emergency_resetter,
            mouse_uinput=mouse,  # type: ignore[arg-type]
            gamepad_uinput=gamepad,  # type: ignore[arg-type]
        )
        passthrough_calls: list[tuple[int, int, int]] = []
        emitted_moves: list[tuple[ActionType, int, int]] = []
        fire_tasks: list[asyncio.Task] = []
        monkeypatch.setattr(
            gdr,
            "emit_configured_mouse_move",
            lambda _device, action: emitted_moves.append(
                (action.action_type, action.move_x, action.move_y)
            ),
        )
        monkeypatch.setattr(
            gda.shared_action_runner,
            "passthrough",
            lambda _device, event, **_kwargs: passthrough_calls.append(
                (event.type, event.code, event.value)
            ),
        )
        monkeypatch.setattr(
            gde,
            "_fire_and_observe",
            lambda coro, _label: fire_tasks.append(asyncio.create_task(coro)) or fire_tasks[-1],
        )

        press = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=1, value=1)
        release = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=1, value=0)

        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.PASSTHROUGH),
            press,
            "btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.MOUSE, target="btn_left"),
            press,
            "mouse_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.MOUSE, target="btn_left"),
            release,
            "mouse_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.MOUSE, target="rel_wheel:1"),
            press,
            "mouse_wheel",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.MOUSE, target="rel_wheel:1"),
            release,
            "mouse_wheel",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(
                action_type=ActionType.GAMEPAD_AXIS,
                target="abs_z",
                axis_value=255,
            ),
            press,
            "axis_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(
                action_type=ActionType.GAMEPAD_AXIS,
                target="abs_z",
                axis_value=255,
            ),
            release,
            "axis_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.GAMEPAD, target="btn_south"),
            press,
            "pad_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.GAMEPAD, target="btn_south"),
            release,
            "pad_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.EXEC, exec_ref=7),
            press,
            "exec_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(
                action_type=ActionType.COMPOSITOR_DISPATCH,
                compositor_dispatcher="workspace",
                compositor_args="2",
            ),
            press,
            "dispatch_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.START_MACRO_RECORDING),
            press,
            "start_rec",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.STOP_MACRO_RECORDING),
            press,
            "stop_rec",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.CANCEL_MACRO_PLAYBACK),
            press,
            "cancel_macro",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.EMERGENCY_RESET),
            press,
            "emergency_reset",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.PROFILE_TOGGLE, profile_name="Gaming"),
            press,
            "toggle_profile",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.MACRO, macro_name="demo"),
            press,
            "macro_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.MACRO, macro_name="demo"),
            release,
            "macro_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.MOUSE_MOVE_REL, move_x=5, move_y=-2),
            press,
            "move_rel",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.MOUSE_MOVE_ABS, move_x=10, move_y=20),
            press,
            "move_abs",
        )

        if fire_tasks:
            await asyncio.gather(*fire_tasks)

        assert passthrough_calls == [(press.type, press.code, press.value)]
        rel_wheel_hi_res = evdev.ecodes.REL_WHEEL_HI_RES  # type: ignore[attr-defined]
        assert mouse.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 0),
            (evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL, 1),
            (evdev.ecodes.EV_REL, rel_wheel_hi_res, 120),
        ]
        assert gamepad.writes == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 255),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 0),
        ]
        assert emitted_moves == [
            (ActionType.MOUSE_MOVE_REL, 5, -2),
            (ActionType.MOUSE_MOVE_ABS, 10, 20),
        ]
        assert callback.await_count == 6
        assert macro_player.await_count == 2
        emergency_resetter.assert_awaited_once_with()
        assert macro_player.await_args_list[0].kwargs["trigger_value"] == 1
        assert macro_player.await_args_list[1].kwargs["trigger_value"] == 0

    @pytest.mark.asyncio
    async def test_emergency_reset_action_without_resetter_does_not_broadcast(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        callback = AsyncMock()
        device = _make_grabbed_device(
            monkeypatch,
            broadcast_callback=callback,
        )

        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.EMERGENCY_RESET),
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=1, value=1),
            "emergency_reset",
        )
        await asyncio.sleep(0)

        callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_repeat_action_replays_last_keyboard_action(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        keyboard = _FakeUInput()
        device = _make_grabbed_device(monkeypatch, keyboard_uinput=keyboard)
        source_press = evdev.InputEvent(
            0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1
        )
        source_release = evdev.InputEvent(
            0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0
        )
        repeat_press = evdev.InputEvent(
            0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1
        )
        repeat_hold = evdev.InputEvent(
            0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 2
        )
        repeat_release = evdev.InputEvent(
            0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 0
        )

        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
            source_press,
            "source",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
            source_release,
            "source",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.REPEAT),
            repeat_press,
            "repeat_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.REPEAT),
            repeat_hold,
            "repeat_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.REPEAT),
            repeat_release,
            "repeat_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.REPEAT),
            repeat_press,
            "repeat_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.REPEAT),
            repeat_release,
            "repeat_btn",
        )

        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 2),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]
        assert len(device.repeat_state.history) == 3

    @pytest.mark.asyncio
    async def test_repeat_action_replays_mapped_gamepad_axis_action(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        gamepad = _FakeUInput()
        device = _make_grabbed_device(monkeypatch, gamepad_uinput=gamepad)
        source_press = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1)
        source_release = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0)
        repeat_press = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1)
        repeat_release = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 0)
        axis_action = dm.MappingAction(
            action_type=ActionType.GAMEPAD_AXIS,
            target="abs_z",
            axis_value=255,
        )

        await _runtime_execute_grabbed_action(device, axis_action, source_press, "source")
        await _runtime_execute_grabbed_action(device, axis_action, source_release, "source")
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.REPEAT),
            repeat_press,
            "repeat_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.REPEAT),
            repeat_release,
            "repeat_btn",
        )

        assert gamepad.writes == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 255),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 255),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0),
        ]

    @pytest.mark.asyncio
    async def test_repeat_exec_refresh_preserves_original_history_source(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from keymasq.keymasqd.runtime.repeat import RepeatHistoryEntry

        device = _make_grabbed_device(monkeypatch)
        device.repeat_state.history.append(
            RepeatHistoryEntry(
                category="special",
                action=dm.MappingAction(action_type=ActionType.EXEC, exec_ref=7),
                source_device="original-device",
                source_button="original-button",
            )
        )

        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.REPEAT),
            evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
            "repeat_btn",
        )

        latest = device.repeat_state.history[-1]
        assert latest.action.action_type == ActionType.EXEC
        assert latest.action.exec_ref == 7
        assert latest.source_device == "original-device"
        assert latest.source_button == "original-button"

    @pytest.mark.asyncio
    async def test_repeat_exec_refresh_checks_exec_ref_identity(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from keymasq.keymasqd.runtime.repeat import (
            RepeatHistoryEntry,
            refresh_repeated_exec_source,
        )

        device = _make_grabbed_device(monkeypatch)
        selected_entry = RepeatHistoryEntry(
            category="special",
            action=dm.MappingAction(action_type=ActionType.EXEC, exec_ref=7),
            source_device="original-device",
            source_button="original-button",
        )
        device.repeat_state.history.append(
            RepeatHistoryEntry(
                category="special",
                action=dm.MappingAction(action_type=ActionType.EXEC, exec_ref=8),
                source_device="other-device",
                source_button="other-button",
            )
        )

        refresh_repeated_exec_source(device.repeat_state, selected_entry)

        latest = device.repeat_state.history[-1]
        assert latest.action.exec_ref == 8
        assert latest.source_device == "other-device"
        assert latest.source_button == "other-button"

    @pytest.mark.asyncio
    async def test_repeat_superkey_exec_refresh_preserves_original_history_source(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from keymasq.keymasqd.runtime.repeat import (
            SUPERKEY_SLOT_TAP,
            RepeatHistoryEntry,
        )

        device = _make_grabbed_device(monkeypatch)
        superkey_config = SuperkeyConfig(
            name="exec-superkey",
            tap_actions=[SuperkeyActionData(action_type="exec", exec_ref=7)],
        )
        device.repeat_state.history.append(
            RepeatHistoryEntry(
                category="special",
                action=dm.MappingAction(
                    action_type=ActionType.SUPERKEY,
                    superkey_config=superkey_config,
                ),
                source_device="original-device",
                source_button="original-button",
                superkey_slot=SUPERKEY_SLOT_TAP,
            )
        )

        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.REPEAT),
            evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
            "repeat_btn",
        )

        latest = device.repeat_state.history[-1]
        assert latest.action.action_type == ActionType.SUPERKEY
        assert latest.action.superkey_config is superkey_config
        assert latest.superkey_slot == SUPERKEY_SLOT_TAP
        assert latest.source_device == "original-device"
        assert latest.source_button == "original-button"

    @pytest.mark.asyncio
    async def test_repeat_profile_action_uses_physical_trigger_lifetime(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from keymasq.keymasqd.runtime.repeat import RepeatHistoryEntry

        events: list[tuple[CommandType, dict[str, object]]] = []

        async def broadcast(event_type: CommandType, data: dict[str, object]) -> None:
            events.append((event_type, data))
            if event_type == CommandType.ACTION_TRIGGER:
                await manager.track_profile_activation(
                    str(data["profile_name"]),
                    "activation-1",
                    str(data["trigger_id"]),
                    data.get("deactivation"),
                )

        manager = DeviceManager(broadcast_callback=broadcast)
        device = _make_grabbed_device(
            monkeypatch,
            button_map={"repeat": "key_f13"},
            broadcast_callback=broadcast,
            profile_activation_trigger_start_observer=manager.observe_profile_trigger_start,
            profile_activation_trigger_end_observer=manager.observe_profile_trigger_end,
            repeat_state=manager.repeat_state,
        )
        device.mapping_getter = lambda: {  # type: ignore[method-assign]
            "repeat": dm.MappingAction(action_type=ActionType.REPEAT),
        }
        manager.repeat_state.history.append(
            RepeatHistoryEntry(
                category="special",
                action=dm.MappingAction(
                    action_type=ActionType.PROFILE_ENABLE,
                    profile_name="Nav",
                    profile_deactivation=ProfileDeactivationPolicy(on_trigger_end=True),
                ),
            )
        )
        repeat_press = evdev.InputEvent(
            0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1
        )
        repeat_release = evdev.InputEvent(
            0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0
        )

        await _runtime_process_grabbed_event(device, repeat_press)
        await asyncio.sleep(0)

        assert (
            CommandType.PROFILE_DEACTIVATE_REQUESTED,
            {
                "profile_name": "Nav",
                "activation_id": "activation-1",
                "reason": "trigger_end",
            },
        ) not in events

        await _runtime_process_grabbed_event(device, repeat_release)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert (
            CommandType.PROFILE_DEACTIVATE_REQUESTED,
            {
                "profile_name": "Nav",
                "activation_id": "activation-1",
                "reason": "trigger_end",
            },
        ) in events

    @pytest.mark.asyncio
    async def test_repeat_action_filters_history_categories(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        keyboard = _FakeUInput()
        mouse = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            keyboard_uinput=keyboard,
            mouse_uinput=mouse,
        )
        press = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1)
        release = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0)

        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.MOUSE, target="btn_left"),
            press,
            "mouse",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.MOUSE, target="btn_left"),
            release,
            "mouse",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
            press,
            "keyboard",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
            release,
            "keyboard",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(
                action_type=ActionType.REPEAT,
                repeat_categories=["mouse"],
            ),
            press,
            "repeat_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(
                action_type=ActionType.REPEAT,
                repeat_categories=["mouse"],
            ),
            release,
            "repeat_btn",
        )

        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]
        assert mouse.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 0),
        ]

    @pytest.mark.asyncio
    async def test_repeat_action_can_replay_passthrough_input(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        keyboard = _FakeUInput()
        passthrough = _FakeUInput()
        device = _make_grabbed_device(monkeypatch, keyboard_uinput=keyboard)
        device.uinput = passthrough  # type: ignore[assignment]
        source_press = evdev.InputEvent(
            0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1
        )
        source_release = evdev.InputEvent(
            0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0
        )
        repeat_press = evdev.InputEvent(
            0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1
        )
        repeat_release = evdev.InputEvent(
            0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 0
        )

        await _runtime_process_grabbed_event(device, source_press)
        await _runtime_process_grabbed_event(device, source_release)
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.REPEAT),
            repeat_press,
            "repeat_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.REPEAT),
            repeat_release,
            "repeat_btn",
        )

        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
        ]
        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
        ]

    @pytest.mark.asyncio
    async def test_repeat_action_replays_passthrough_mouse_click_more_than_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mouse = _FakeUInput()
        passthrough = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            button_map={"left": "btn_left", "repeat": "key_f13"},
            mouse_uinput=mouse,
        )
        device.uinput = passthrough  # type: ignore[assignment]
        device.mapping_getter = lambda: {
            "repeat": dm.MappingAction(action_type=ActionType.REPEAT)
        }
        left_press = evdev.InputEvent(
            0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 1
        )
        left_release = evdev.InputEvent(
            0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 0
        )
        repeat_press = evdev.InputEvent(
            0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1
        )
        repeat_release = evdev.InputEvent(
            0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0
        )

        await _runtime_process_grabbed_event(device, left_press)
        await _runtime_process_grabbed_event(device, left_release)
        await _runtime_process_grabbed_event(device, repeat_press)
        await _runtime_process_grabbed_event(device, repeat_release)
        await _runtime_process_grabbed_event(device, repeat_press)
        await _runtime_process_grabbed_event(device, repeat_release)

        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 0),
        ]
        assert mouse.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 0),
        ]

    @pytest.mark.asyncio
    async def test_repeat_passthrough_gamepad_button_reuses_source_hardware_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from keymasq.keymasqd.runtime.repeat import remember_passthrough_event

        source_gamepad = _FakeUInput()
        default_gamepad = _FakeUInput()
        resolved_output_ids: list[str | None] = []
        device = _make_grabbed_device(
            monkeypatch,
            device_type=DeviceType.GAMEPAD,
            device_types=[DeviceType.GAMEPAD.value],
            gamepad_uinput=default_gamepad,
        )

        def resolve_gamepad_output(output_id: str | None, _context: str) -> SimpleNamespace:
            resolved_output_ids.append(output_id)
            return SimpleNamespace(
                output_id=output_id,
                uinput=source_gamepad if output_id == "1234:5678" else default_gamepad,
                bucket=f"gamepad:{output_id or 'virtual-gamepad-1'}",
            )

        device._gamepad_output_resolver = resolve_gamepad_output  # type: ignore[method-assign, reportPrivateUsage]
        remember_passthrough_event(
            device.repeat_state,
            device,
            evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 1),
            "btn_south",
            evdev_mod=evdev,
        )

        remembered = device.repeat_state.history[-1].action
        assert remembered.action_type == ActionType.GAMEPAD
        assert remembered.output_id == "1234:5678"

        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.REPEAT),
            evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
            "repeat_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.REPEAT),
            evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 0),
            "repeat_btn",
        )

        assert resolved_output_ids == ["1234:5678", "1234:5678"]
        assert source_gamepad.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 0),
        ]
        assert default_gamepad.writes == []

    @pytest.mark.asyncio
    async def test_repeat_passthrough_gamepad_trigger_button_remains_digital(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from keymasq.keymasqd.runtime.repeat import remember_passthrough_event

        source_gamepad = _FakeUInput()
        default_gamepad = _FakeUInput()
        resolved_output_ids: list[str | None] = []
        device = _make_grabbed_device(
            monkeypatch,
            device_type=DeviceType.GAMEPAD,
            device_types=[DeviceType.GAMEPAD.value],
            gamepad_uinput=default_gamepad,
        )

        def resolve_gamepad_output(output_id: str | None, _context: str) -> SimpleNamespace:
            resolved_output_ids.append(output_id)
            return SimpleNamespace(
                output_id=output_id,
                uinput=source_gamepad if output_id == "1234:5678" else default_gamepad,
                bucket=f"gamepad:{output_id or 'virtual-gamepad-1'}",
            )

        device._gamepad_output_resolver = resolve_gamepad_output  # type: ignore[method-assign, reportPrivateUsage]
        remember_passthrough_event(
            device.repeat_state,
            device,
            evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_TL2, 1),
            "btn_tl2",
            evdev_mod=evdev,
        )

        remembered = device.repeat_state.history[-1].action
        assert remembered.action_type == ActionType.GAMEPAD
        assert remembered.target == "btn_tl2"
        assert remembered.output_id == "1234:5678"

        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.REPEAT),
            evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
            "repeat_btn",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.REPEAT),
            evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 0),
            "repeat_btn",
        )

        assert resolved_output_ids == ["1234:5678", "1234:5678"]
        assert source_gamepad.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_TL2, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_TL2, 0),
        ]
        assert default_gamepad.writes == []

    @pytest.mark.asyncio
    async def test_repeat_replays_passthrough_high_res_wheel_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rel_wheel_hi_res = getattr(evdev.ecodes, "REL_WHEEL_HI_RES", None)
        if rel_wheel_hi_res is None:
            pytest.skip("kernel headers do not expose REL_WHEEL_HI_RES")
        mouse = _FakeUInput()
        passthrough = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            button_map={"repeat": "key_f13"},
            mouse_uinput=mouse,
        )
        device.uinput = passthrough  # type: ignore[assignment]
        device.mapping_getter = lambda: {
            "repeat": dm.MappingAction(action_type=ActionType.REPEAT)
        }

        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(0, 0, evdev.ecodes.EV_REL, int(rel_wheel_hi_res), -120),
        )
        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
        )
        await _runtime_process_grabbed_event(
            device,
            evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0),
        )

        assert passthrough.writes == [(evdev.ecodes.EV_REL, int(rel_wheel_hi_res), -120)]
        assert mouse.writes == [
            (evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL, -1),
            (evdev.ecodes.EV_REL, int(rel_wheel_hi_res), -120),
        ]

    @pytest.mark.asyncio
    async def test_repeat_mouse_wheel_rapidfire_emits_relative_events(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mouse = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            mouse_uinput=mouse,
        )
        device._running = True
        wheel_source = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1)

        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.MOUSE, target="rel_wheel:1"),
            wheel_source,
            "wheel",
        )
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(
                action_type=ActionType.REPEAT,
                rapidfire_enabled=True,
                rapidfire_hold_ms=1,
                rapidfire_wait_ms=1,
            ),
            evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
            "repeat_btn",
        )
        await asyncio.sleep(0.01)
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(
                action_type=ActionType.REPEAT,
                rapidfire_enabled=True,
                rapidfire_hold_ms=1,
                rapidfire_wait_ms=1,
            ),
            evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 0),
            "repeat_btn",
        )

        wheel_writes = [
            write
            for write in mouse.writes
            if write[0:2] == (evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL)
        ]
        assert len(wheel_writes) > 1

    def test_repeat_rapidfire_is_limited_to_key_button_and_wheel_actions(self) -> None:
        from keymasq.keymasqd.runtime.repeat import (
            RepeatHistoryEntry,
            RepeatRuntimeState,
            remember_passthrough_event,
            repeat_category_for_action,
            repeat_execution_action,
            select_repeated_action,
        )

        repeat_action = dm.MappingAction(
            action_type=ActionType.REPEAT,
            rapidfire_enabled=True,
            rapidfire_hold_ms=5,
            rapidfire_wait_ms=7,
        )
        key_action = repeat_execution_action(
            repeat_action,
            dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
        )
        wheel_action = repeat_execution_action(
            repeat_action,
            dm.MappingAction(action_type=ActionType.MOUSE, target="rel_wheel:1"),
        )
        macro_action = repeat_execution_action(
            repeat_action,
            dm.MappingAction(action_type=ActionType.MACRO, macro_name="demo"),
        )
        repeat_state = RepeatRuntimeState()
        remember_passthrough_event(
            repeat_state,
            SimpleNamespace(hardware_id="mouse", device_types=["mouse"]),
            evdev.InputEvent(0, 0, evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 10),
            "rel_x",
            evdev_mod=evdev,
        )
        repeat_state.history.append(
            RepeatHistoryEntry(
                category="keyboard",
                action=dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_b"),
            )
        )
        repeat_state.history.append(
            RepeatHistoryEntry(
                category="special",
                action=dm.MappingAction(action_type=ActionType.REPEAT),
            )
        )

        assert key_action.rapidfire_enabled is True
        assert key_action.rapidfire_hold_ms == 5
        assert key_action.rapidfire_wait_ms == 7
        assert wheel_action.rapidfire_enabled is True
        assert macro_action.rapidfire_enabled is False
        assert repeat_category_for_action(
            dm.MappingAction(action_type=ActionType.MOUSE_MOVE_REL, move_x=5)
        ) == "special"
        selected_action = select_repeated_action(repeat_state, repeat_action)
        assert selected_action is not None
        assert selected_action.target == "key_b"

    @pytest.mark.asyncio
    async def test_routed_gamepad_axis_release_all_keys_zeros_target_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = _make_grabbed_device(monkeypatch)
        default_gamepad = _FakeUInput()
        second_gamepad = _FakeUInput()
        device.gamepad_uinput = default_gamepad  # type: ignore[assignment]
        device._gamepad_output_resolver = lambda output_id, context: SimpleNamespace(  # type: ignore[method-assign, reportPrivateUsage]
            output_id=output_id,
            uinput=second_gamepad if output_id == "virtual-gamepad-2" else default_gamepad,
            bucket=f"gamepad:{output_id or 'virtual-gamepad-1'}",
            is_virtual=True,
        )
        press = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 1)

        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(
                action_type=ActionType.GAMEPAD_AXIS,
                target="abs_z",
                axis_value=255,
                output_id="virtual-gamepad-2",
            ),
            press,
            "axis_btn",
        )

        assert second_gamepad.writes == [(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 255)]
        assert device.state.held_output_abs["gamepad:virtual-gamepad-2"] == {
            evdev.ecodes.ABS_Z
        }

        gdo.release_all_keys(
            device,
            evdev_mod=evdev,
            uinput_writer=lambda device: cast(gdt.WritableUInput | None, device),
        )

        assert (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0) in second_gamepad.writes
        assert device.state.held_output_abs["gamepad:virtual-gamepad-2"] == set()

    @pytest.mark.asyncio
    async def test_gamepad_axis_action_writes_configured_value_and_release(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        gamepad = _FakeUInput()
        device = _make_grabbed_device(monkeypatch, gamepad_uinput=gamepad)
        press = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 1)
        release = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 0)
        action = dm.MappingAction(
            action_type=ActionType.GAMEPAD_AXIS,
            target="abs_x",
            axis_value=-32768,
        )

        await _runtime_execute_grabbed_action(device, action, press, "axis_btn")
        await _runtime_execute_grabbed_action(device, action, release, "axis_btn")

        assert gamepad.writes == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, -32768),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 0),
        ]

    @pytest.mark.asyncio
    async def test_gamepad_axis_tap_uses_configured_value(
        self,
        monkeypatch: pytest.MonkeyPatch,
        ) -> None:
        gamepad = _FakeUInput()
        device = _make_grabbed_device(monkeypatch, gamepad_uinput=gamepad)
        press = evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 1)

        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(
                action_type=ActionType.GAMEPAD_AXIS,
                target="abs_rz",
                axis_value=123,
                tap_enabled=True,
                tap_hold_ms=1,
            ),
            press,
            "axis_tap",
        )
        await asyncio.sleep(0.01)

        assert gamepad.writes == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RZ, 123),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RZ, 0),
        ]

    def test_combo_restore_routes_gamepad_output_id_and_tracks_bucket(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = _make_grabbed_device(monkeypatch)
        default_gamepad = _FakeUInput()
        second_gamepad = _FakeUInput()
        device.gamepad_uinput = default_gamepad  # type: ignore[assignment]
        device._gamepad_output_resolver = lambda output_id, context: SimpleNamespace(  # type: ignore[method-assign, reportPrivateUsage]
            output_id=output_id,
            uinput=second_gamepad if output_id == "virtual-gamepad-2" else default_gamepad,
            bucket=f"gamepad:{output_id or 'virtual-gamepad-1'}",
            is_virtual=True,
        )
        device.state.held_source_actions["key_x"] = dm.MappingAction(
            action_type=ActionType.GAMEPAD,
            target="btn_south",
            output_id="virtual-gamepad-2",
        )
        device.state.held_output_keys["gamepad:virtual-gamepad-2"] = {
            evdev.ecodes.BTN_SOUTH
        }

        assert device.combo_passthrough_binding_active("key_x") is True

        device.emit_combo_release("key_x")

        assert default_gamepad.writes == []
        assert second_gamepad.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 0)
        ]
        assert device.state.held_output_keys["gamepad:virtual-gamepad-2"] == set()

        device.emit_combo_press("key_x")

        assert default_gamepad.writes == []
        assert second_gamepad.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.BTN_SOUTH, 1),
        ]
        assert device.state.held_output_keys["gamepad:virtual-gamepad-2"] == {
            evdev.ecodes.BTN_SOUTH
        }

    def test_release_all_keys_clears_missing_routed_abs_bucket_without_key_bucket(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        device = _make_grabbed_device(monkeypatch)
        device._gamepad_output_resolver = lambda output_id, context: None  # type: ignore[method-assign, reportPrivateUsage]
        device.state.held_output_abs["gamepad:virtual-gamepad-2"] = {
            evdev.ecodes.ABS_Z
        }
        device.state.held_output_keys.pop("gamepad:virtual-gamepad-2", None)

        gdo.release_all_keys(
            device,
            evdev_mod=evdev,
            uinput_writer=lambda device: cast(gdt.WritableUInput | None, device),
        )

        assert "gamepad:virtual-gamepad-2" not in device.state.held_output_keys
        assert device.state.held_output_abs["gamepad:virtual-gamepad-2"] == set()

    @pytest.mark.asyncio
    async def test_execute_action_mouse_move_abs_uses_cursor_position_setter(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cursor_position_setter = AsyncMock(return_value={"status": "ok"})
        mouse = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            cursor_position_setter=cursor_position_setter,
            mouse_uinput=mouse,  # type: ignore[arg-type]
        )

        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(action_type=ActionType.MOUSE_MOVE_ABS, move_x=10, move_y=20),
            SimpleNamespace(value=1),
            "move_abs",
        )

        cursor_position_setter.assert_awaited_once_with(10, 20)
        assert mouse.writes == []

    @pytest.mark.asyncio
    async def test_rapidfire_mouse_move_abs_uses_cursor_position_setter(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cursor_position_setter = AsyncMock(return_value={"status": "ok"})
        mouse = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            cursor_position_setter=cursor_position_setter,
            mouse_uinput=mouse,  # type: ignore[arg-type]
        )
        device._running = True
        device.state.rapidfire_active["move_abs"] = True
        action = dm.MappingAction(
            action_type=ActionType.MOUSE_MOVE_ABS,
            move_x=10,
            move_y=20,
        )

        task = asyncio.create_task(
            gdr.rapidfire_move(
                device,
                action,
                "move_abs",
                1,
                100,
                asyncio_mod=gdm.ASYNCIO_RUNTIME,
            )
        )
        await asyncio.sleep(0.02)
        device.state.rapidfire_active["move_abs"] = False
        await task

        cursor_position_setter.assert_awaited()
        cursor_position_setter.assert_any_await(10, 20)
        assert mouse.writes == []

    @pytest.mark.asyncio
    async def test_tap_mouse_move_abs_uses_cursor_position_setter(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cursor_position_setter = AsyncMock(return_value={"status": "ok"})
        mouse = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            cursor_position_setter=cursor_position_setter,
            mouse_uinput=mouse,  # type: ignore[arg-type]
        )
        action = dm.MappingAction(
            action_type=ActionType.MOUSE_MOVE_ABS,
            move_x=10,
            move_y=20,
        )

        await _runtime_tap_grabbed_move(device, action, "move_abs", 1)

        cursor_position_setter.assert_awaited_once_with(10, 20)
        assert mouse.writes == []

    @pytest.mark.asyncio
    async def test_execute_action_covers_superkey_and_tap_move_branches(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cursor_position_setter = AsyncMock(return_value={"status": "ok"})
        cancel_macro_playback = AsyncMock(return_value={"status": "ok"})
        device = _make_grabbed_device(
            monkeypatch,
            cursor_position_setter=cursor_position_setter,
        )
        move_calls: list[tuple[str, int]] = []
        fake_machine = SimpleNamespace(on_down=AsyncMock(), on_up=AsyncMock())
        created_configs: list[SuperkeyConfig] = []
        created_setters: list[object] = []
        created_cancelers: list[object] = []

        monkeypatch.setattr(
            gda,
            "SuperkeyMachine",
            lambda **kwargs: (
                created_configs.append(kwargs["config"]),
                created_setters.append(kwargs.get("cursor_position_setter")),
                created_cancelers.append(kwargs.get("cancel_macro_playback")),
                fake_machine,
            )[-1],
        )
        monkeypatch.setattr(
            gde,
            "_fire_and_observe",
            lambda coro, _label: asyncio.create_task(coro),
        )
        monkeypatch.setattr(
            gda.shared_action_runner,
            "tap_move",
            AsyncMock(
                side_effect=lambda _device, action, event_name, hold_ms, **_kwargs: (
                    move_calls.append((event_name, hold_ms))
                )
            ),
        )

        superkey_action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="super",
                tap_actions=[SuperkeyActionData(action_type="exec", exec_ref=4)],
            ),
        )
        tap_move_action = dm.MappingAction(
            action_type=ActionType.MOUSE_MOVE_REL,
            move_x=1,
            move_y=2,
            tap_enabled=True,
            tap_hold_ms=33,
        )

        await gda.execute_action(
            device,
            superkey_action,
            SimpleNamespace(value=1),
            "super_btn",
            deps=gde.build_action_execution_deps(fire_and_observe_fn=gde._fire_and_observe),
            cancel_macro_playback=cancel_macro_playback,
        )
        await _runtime_execute_grabbed_action(
            device, superkey_action, SimpleNamespace(value=0), "super_btn"
        )
        await _runtime_execute_grabbed_action(
            device, tap_move_action, SimpleNamespace(value=1), "move_btn"
        )
        await asyncio.sleep(0)

        assert created_configs and created_configs[0].name == "super"
        assert created_setters == [cursor_position_setter]
        assert created_cancelers == [cancel_macro_playback]
        fake_machine.on_down.assert_awaited_once()
        fake_machine.on_up.assert_awaited_once()
        assert move_calls == [("move_btn", 33)]

    @pytest.mark.asyncio
    async def test_execute_action_mouse_wheel_rapidfire_emits_relative_events(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mouse = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            mouse_uinput=mouse,  # type: ignore[arg-type]
        )
        device._running = True

        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(
                action_type=ActionType.MOUSE,
                target="rel_hwheel:-1",
                rapidfire_enabled=True,
                rapidfire_hold_ms=1,
                rapidfire_wait_ms=1,
            ),
            SimpleNamespace(value=1),
            "wheel_rf",
        )
        await asyncio.sleep(0.01)
        await _runtime_execute_grabbed_action(
            device,
            dm.MappingAction(
                action_type=ActionType.MOUSE,
                target="rel_hwheel:-1",
                rapidfire_enabled=True,
                rapidfire_hold_ms=1,
                rapidfire_wait_ms=1,
            ),
            SimpleNamespace(value=0),
            "wheel_rf",
        )

        assert any(
            write == (evdev.ecodes.EV_REL, evdev.ecodes.REL_HWHEEL, -1)
            for write in mouse.writes
        )

    @pytest.mark.asyncio
    async def test_execute_action_mouse_wheel_ignores_repeat_without_rapidfire(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mouse = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            mouse_uinput=mouse,  # type: ignore[arg-type]
        )

        action = dm.MappingAction(
            action_type=ActionType.MOUSE,
            target="rel_wheel:1",
        )

        await _runtime_execute_grabbed_action(
            device,
            action,
            SimpleNamespace(value=1),
            "wheel_plain",
        )
        await _runtime_execute_grabbed_action(
            device,
            action,
            SimpleNamespace(value=2),
            "wheel_plain",
        )
        await _runtime_execute_grabbed_action(
            device,
            action,
            SimpleNamespace(value=0),
            "wheel_plain",
        )

        rel_wheel_hi_res = evdev.ecodes.REL_WHEEL_HI_RES  # type: ignore[attr-defined]
        assert mouse.writes == [
            (evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL, 1),
            (evdev.ecodes.EV_REL, rel_wheel_hi_res, 120),
        ]

    @pytest.mark.asyncio
    async def test_execute_action_mouse_wheel_tap_ignores_repeat(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mouse = _FakeUInput()
        device = _make_grabbed_device(
            monkeypatch,
            mouse_uinput=mouse,  # type: ignore[arg-type]
        )

        action = dm.MappingAction(
            action_type=ActionType.MOUSE,
            target="rel_wheel:-1",
            tap_enabled=True,
            tap_hold_ms=1,
        )

        await _runtime_execute_grabbed_action(
            device,
            action,
            SimpleNamespace(value=1),
            "wheel_tap",
        )
        await asyncio.sleep(0)
        await _runtime_execute_grabbed_action(
            device,
            action,
            SimpleNamespace(value=2),
            "wheel_tap",
        )
        await asyncio.sleep(0.01)

        rel_wheel_hi_res = evdev.ecodes.REL_WHEEL_HI_RES  # type: ignore[attr-defined]
        assert mouse.writes == [
            (evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL, -1),
            (evdev.ecodes.EV_REL, rel_wheel_hi_res, -120),
        ]
