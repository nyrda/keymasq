# ruff: noqa: F403, F405, I001
from tests.keymasqd.device_manager_support import *

class TestComboActionDispatch:
    @pytest.mark.asyncio
    async def test_profile_activation_trigger_end_follows_combo_lifecycle(self) -> None:
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
        binding = dm.RuntimeComboBinding("1234:5678", "btn_side", "mouse")

        await _runtime_start_combo_action(
            manager,
            "profile",
            dm.MappingAction(action_type=ActionType.PROFILE_ENABLE, profile_name="Nav"),
            binding,
        )
        await manager.track_profile_activation(
            "Nav",
            "activation-1",
            "1234:5678:combo:profile",
            {"on_trigger_end": True},
        )
        assert [
            event for event in events if event[0] == CommandType.PROFILE_DEACTIVATE_REQUESTED
        ] == []

        await _runtime_stop_combo_action(manager, "profile")
        await asyncio.wait_for(deactivate_event.wait(), timeout=1.0)

        assert expected_deactivate in events

    @pytest.mark.asyncio
    async def test_combo_overload_superkey_starts_children_and_releases_in_reverse_order(
        self,
    ) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()
        binding = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-overload",
                mode=SuperkeyMode.OVERLOAD,
                overload_actions=[
                    dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
                    dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_b"),
                ],
            ),
        )

        await _runtime_start_combo_action(manager, "combo-overload", action, binding)
        await _runtime_stop_combo_action(manager, "combo-overload")

        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]

    @pytest.mark.asyncio
    async def test_combo_overload_superkey_profile_lifetime_follows_child_trigger(
        self,
    ) -> None:
        events: list[tuple[CommandType, dict[str, object]]] = []
        action_triggers: list[dict[str, object]] = []
        action_trigger_event = asyncio.Event()
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
            if event_type == CommandType.ACTION_TRIGGER:
                action_triggers.append(data)
                await manager.track_profile_activation(
                    str(data["profile_name"]),
                    "activation-1",
                    str(data["trigger_id"]),
                    data["deactivation"],
                )
                action_trigger_event.set()
                return
            event = (event_type, data)
            events.append(event)
            if event == expected_deactivate:
                deactivate_event.set()

        manager = DeviceManager(broadcast_callback=broadcast)
        binding = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-overload-profile",
                mode=SuperkeyMode.OVERLOAD,
                overload_actions=[
                    dm.MappingAction(
                        action_type=ActionType.PROFILE_ENABLE,
                        profile_name="Nav",
                        profile_deactivation=ProfileDeactivationPolicy(
                            on_trigger_end=True
                        ),
                    ),
                ],
            ),
        )

        await _runtime_start_combo_action(manager, "combo-overload-profile", action, binding)
        await asyncio.wait_for(action_trigger_event.wait(), timeout=1.0)
        await asyncio.sleep(0)

        assert action_triggers == [
            {
                "action_type": "profile_enable",
                "profile_name": "Nav",
                "source_device": "1234:5678",
                "source_button": "combo:combo-overload-profile#overload#0",
                "trigger_id": "1234:5678:combo:combo-overload-profile#overload#0",
                "deactivation": {
                    "on_trigger_end": True,
                },
            }
        ]
        assert [
            event for event in events if event[0] == CommandType.PROFILE_DEACTIVATE_REQUESTED
        ] == []

        await _runtime_stop_combo_action(manager, "combo-overload-profile")
        await asyncio.wait_for(deactivate_event.wait(), timeout=1.0)

        assert expected_deactivate in events

    @pytest.mark.asyncio
    async def test_combo_split_overload_superkey_pulses_down_and_up_children(
        self,
    ) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()
        binding = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-split-overload",
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

        await _runtime_start_combo_action(manager, "combo-split-overload", action, binding)
        await _runtime_stop_combo_action(manager, "combo-split-overload")

        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 0),
        ]

    def test_combo_overload_superkey_rejects_nested_superkey_children(
        self,
    ) -> None:
        with pytest.raises(ValueError, match="nested superkeys are not allowed"):
            SuperkeyConfig(
                name="combo-overload",
                mode=SuperkeyMode.OVERLOAD,
                overload_actions=[
                    dm.MappingAction(action_type=ActionType.SUPERKEY, superkey_name="nested"),
                ],
            )

    @pytest.mark.asyncio
    async def test_combo_superkey_without_config_does_not_start_profile_trigger(
        self,
    ) -> None:
        starts: list[str] = []
        ends: list[str] = []
        manager = DeviceManager()
        manager.observe_profile_trigger_start = starts.append  # type: ignore[method-assign]
        manager.observe_profile_trigger_end = ends.append  # type: ignore[method-assign]
        binding = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")

        await _runtime_start_combo_action(
            manager,
            "combo-invalid-superkey",
            dm.MappingAction(action_type=ActionType.SUPERKEY),
            binding,
        )

        assert starts == []
        assert ends == []
        assert "combo-invalid-superkey" not in manager.combo_state.active_actions

    @pytest.mark.asyncio
    async def test_combo_hold_macro_waits_for_press_registration_before_active(
        self,
    ) -> None:
        manager = DeviceManager()
        binding = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        press_started = asyncio.Event()
        press_registered = asyncio.Event()
        press_continue = asyncio.Event()

        async def play_macro(**kwargs: object) -> dict[str, object]:
            if kwargs["trigger_value"] == 1:
                press_started.set()
                await press_continue.wait()
                press_registered.set()
            else:
                assert press_registered.is_set()
            return {"status": "ok"}

        manager.play_macro = play_macro  # type: ignore[method-assign]
        action = dm.MappingAction(
            action_type=ActionType.MACRO,
            macro_name="hold",
            macro_loop_mode="hold",
        )

        start_task = asyncio.create_task(
            _runtime_start_combo_action(manager, "macro-hold", action, binding)
        )
        await asyncio.wait_for(press_started.wait(), timeout=1.0)
        await asyncio.sleep(0)

        assert not start_task.done()

        press_continue.set()
        await start_task
        await _runtime_stop_combo_action(manager, "macro-hold")

        assert press_registered.is_set()

    @pytest.mark.asyncio
    async def test_combo_overload_restore_runs_after_child_release_for_overlapping_key(
        self,
    ) -> None:
        class FakeComboDevice:
            def __init__(self) -> None:
                self.hardware_id = "1234:5678"
                self.interface_id = "kbd"
                self.active = {"key_leftmeta", "key_c"}
                self.held = {"key_leftmeta", "key_c"}
                self.recalled: set[str] = set()
                self.releases: list[str] = []
                self.presses: list[str] = []

            def emit_combo_release(self, evdev_name: str) -> None:
                self.releases.append(evdev_name)
                self.active.discard(evdev_name)

            def emit_combo_press(self, evdev_name: str) -> None:
                self.presses.append(evdev_name)
                self.active.add(evdev_name)

            def combo_passthrough_binding_active(self, evdev_name: str) -> bool:
                return evdev_name in self.active

            def combo_source_binding_held(self, evdev_name: str) -> bool:
                return evdev_name in self.held

            def mark_combo_recalled_binding(self, evdev_name: str) -> None:
                self.recalled.add(evdev_name)

            def clear_combo_recalled_binding(self, evdev_name: str) -> None:
                self.recalled.discard(evdev_name)

            def combo_passthrough_held_modifiers(self) -> set[str]:
                return set()

        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()
        fake_device = FakeComboDevice()
        manager.grabbed_devices = {"1234:5678": [fake_device]}
        trigger_meta = dm.RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_leftmeta",
        )
        trigger_c = dm.RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_c",
        )
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-overload-overlap",
                mode=SuperkeyMode.OVERLOAD,
                overload_actions=[
                    dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_leftmeta"),
                ],
            ),
        )
        manager.active_combos = [
            dm.RuntimeCombo(
                id="combo-overload-overlap",
                name="combo-overload-overlap",
                steps=[dm.RuntimeComboStep(bindings=(trigger_meta, trigger_c))],
                action=action,
                recall_trigger_keys=True,
                restore_trigger_keys=["meta"],
            )
        ]

        await _runtime_start_combo_action(
            manager,
            "combo-overload-overlap",
            action,
            trigger_c,
            trigger_bindings=(trigger_meta, trigger_c),
        )

        fake_device.held = {"key_leftmeta"}
        await _runtime_stop_combo_action(manager, "combo-overload-overlap")

        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTMETA, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTMETA, 0),
        ]
        assert fake_device.releases == ["key_c", "key_leftmeta"]
        assert fake_device.presses == ["key_leftmeta"]
        assert fake_device.active == {"key_leftmeta"}
        assert fake_device.recalled == {"key_c"}
    @pytest.mark.asyncio
    async def test_combo_recall_trigger_keys_restores_selected_keys_on_immediate_action_completion(
        self,
    ) -> None:
        class FakeComboDevice:
            def __init__(self) -> None:
                self.hardware_id = "1234:5678"
                self.interface_id = "kbd"
                self.active = {"key_leftmeta", "key_c"}
                self.held = {"key_leftmeta", "key_c"}
                self.recalled: set[str] = set()
                self.releases: list[str] = []
                self.presses: list[str] = []

            def emit_combo_release(self, evdev_name: str) -> None:
                self.releases.append(evdev_name)
                self.active.discard(evdev_name)

            def emit_combo_press(self, evdev_name: str) -> None:
                self.presses.append(evdev_name)
                self.active.add(evdev_name)

            def combo_passthrough_binding_active(self, evdev_name: str) -> bool:
                return evdev_name in self.active

            def combo_source_binding_held(self, evdev_name: str) -> bool:
                return evdev_name in self.held

            def mark_combo_recalled_binding(self, evdev_name: str) -> None:
                self.recalled.add(evdev_name)

            def clear_combo_recalled_binding(self, evdev_name: str) -> None:
                self.recalled.discard(evdev_name)

            def combo_passthrough_held_modifiers(self) -> set[str]:
                return set()

        manager = DeviceManager()
        fake_device = FakeComboDevice()
        manager.grabbed_devices = {"1234:5678": [fake_device]}
        trigger_meta = dm.RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_leftmeta",
        )
        trigger_c = dm.RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_c",
        )
        action = dm.MappingAction(action_type=ActionType.SUPPRESS)
        manager.active_combos = [
            dm.RuntimeCombo(
                id="combo-recall-immediate",
                name="combo-recall-immediate",
                steps=[dm.RuntimeComboStep(bindings=(trigger_meta, trigger_c))],
                action=action,
                recall_trigger_keys=True,
                restore_trigger_keys=["meta"],
            )
        ]

        await _runtime_start_combo_action(
            manager,
            "combo-recall-immediate",
            action,
            trigger_c,
            trigger_bindings=(trigger_meta, trigger_c),
        )

        assert fake_device.releases == ["key_c", "key_leftmeta"]
        assert fake_device.presses == ["key_leftmeta"]
        assert fake_device.active == {"key_leftmeta"}
        assert fake_device.recalled == {"key_c"}
    @pytest.mark.asyncio
    async def test_combo_recall_trigger_keys_restores_selected_keys_after_action_stop(
        self,
    ) -> None:
        class FakeComboDevice:
            def __init__(self) -> None:
                self.hardware_id = "1234:5678"
                self.interface_id = "kbd"
                self.active = {"key_leftmeta", "key_c"}
                self.held = {"key_leftmeta", "key_c"}
                self.recalled: set[str] = set()
                self.releases: list[str] = []
                self.presses: list[str] = []

            def emit_combo_release(self, evdev_name: str) -> None:
                self.releases.append(evdev_name)
                self.active.discard(evdev_name)

            def emit_combo_press(self, evdev_name: str) -> None:
                self.presses.append(evdev_name)
                self.active.add(evdev_name)

            def combo_passthrough_binding_active(self, evdev_name: str) -> bool:
                return evdev_name in self.active

            def combo_source_binding_held(self, evdev_name: str) -> bool:
                return evdev_name in self.held

            def mark_combo_recalled_binding(self, evdev_name: str) -> None:
                self.recalled.add(evdev_name)

            def clear_combo_recalled_binding(self, evdev_name: str) -> None:
                self.recalled.discard(evdev_name)

            def combo_passthrough_held_modifiers(self) -> set[str]:
                return set()

        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()
        fake_device = FakeComboDevice()
        manager.grabbed_devices = {"1234:5678": [fake_device]}
        trigger_meta = dm.RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_leftmeta",
        )
        trigger_c = dm.RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_c",
        )
        action = dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_f5")
        manager.active_combos = [
            dm.RuntimeCombo(
                id="combo-recall-hold",
                name="combo-recall-hold",
                steps=[dm.RuntimeComboStep(bindings=(trigger_meta, trigger_c))],
                action=action,
                recall_trigger_keys=True,
                restore_trigger_keys=["meta"],
            )
        ]

        await _runtime_start_combo_action(
            manager,
            "combo-recall-hold",
            action,
            trigger_c,
            trigger_bindings=(trigger_meta, trigger_c),
        )

        assert fake_device.releases == ["key_c", "key_leftmeta"]
        assert fake_device.presses == []
        assert fake_device.recalled == {"key_c", "key_leftmeta"}
        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F5, 1)
        ]

        fake_device.held = {"key_leftmeta"}
        await _runtime_stop_combo_action(manager, "combo-recall-hold")

        assert fake_device.presses == ["key_leftmeta"]
        assert fake_device.recalled == {"key_c"}
        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F5, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F5, 0),
        ]
    @pytest.mark.asyncio
    async def test_combo_pattern_superkey_single_step_supports_double_tap(self) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()
        binding = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern",
                mode=SuperkeyMode.PATTERN,
                tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
                double_tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_b")],
            ),
        )
        manager.active_combos = [
            dm.RuntimeCombo(
                id="combo-pattern",
                name="combo-pattern",
                steps=[dm.RuntimeComboStep(bindings=(binding,))],
                action=action,
            )
        ]

        await _runtime_start_combo_action(manager, "combo-pattern", action, binding)
        await _runtime_stop_combo_action(manager, "combo-pattern")
        await _runtime_start_combo_action(manager, "combo-pattern", action, binding)
        await _runtime_stop_combo_action(manager, "combo-pattern")
        await asyncio.sleep(0.02)

        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
        ]
    @pytest.mark.asyncio
    async def test_combo_pattern_superkey_single_step_supports_tap_hold(self) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()
        binding = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern-tap-hold",
                mode=SuperkeyMode.PATTERN,
                hold_threshold_ms=0,
                tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
                double_tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_c")],
                tap_hold_actions=[SuperkeyActionData(action_type="keyboard", target="key_b")],
            ),
        )
        manager.active_combos = [
            dm.RuntimeCombo(
                id="combo-pattern-tap-hold",
                name="combo-pattern-tap-hold",
                steps=[dm.RuntimeComboStep(bindings=(binding,))],
                action=action,
            )
        ]

        await _runtime_start_combo_action(manager, "combo-pattern-tap-hold", action, binding)
        await _runtime_stop_combo_action(manager, "combo-pattern-tap-hold")
        await _runtime_start_combo_action(manager, "combo-pattern-tap-hold", action, binding)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await _runtime_stop_combo_action(manager, "combo-pattern-tap-hold")
        await asyncio.sleep(0.02)

        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
        ]
    @pytest.mark.asyncio
    async def test_combo_pattern_superkey_multistep_ignores_double_tap_slots(self) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()
        first = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        second = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_b")
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern-multi",
                mode=SuperkeyMode.PATTERN,
                tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_c")],
                double_tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_d")],
            ),
        )
        manager.active_combos = [
            dm.RuntimeCombo(
                id="combo-pattern-multi",
                name="combo-pattern-multi",
                steps=[
                    dm.RuntimeComboStep(bindings=(first,)),
                    dm.RuntimeComboStep(bindings=(second,)),
                ],
                action=action,
            )
        ]

        await _runtime_start_combo_action(manager, "combo-pattern-multi", action, second)
        await _runtime_stop_combo_action(manager, "combo-pattern-multi")
        await asyncio.sleep(0.02)

        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_C, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_C, 0),
        ]
    @pytest.mark.asyncio
    async def test_combo_pattern_superkey_multistep_supports_hold_only(self) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()
        first = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        second = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_b")
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern-hold",
                mode=SuperkeyMode.PATTERN,
                hold_threshold_ms=0,
                hold_actions=[SuperkeyActionData(action_type="keyboard", target="key_e")],
                tap_hold_actions=[SuperkeyActionData(action_type="keyboard", target="key_f")],
            ),
        )
        manager.active_combos = [
            dm.RuntimeCombo(
                id="combo-pattern-hold",
                name="combo-pattern-hold",
                steps=[
                    dm.RuntimeComboStep(bindings=(first,)),
                    dm.RuntimeComboStep(bindings=(second,)),
                ],
                action=action,
            )
        ]

        await _runtime_start_combo_action(manager, "combo-pattern-hold", action, second)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await _runtime_stop_combo_action(manager, "combo-pattern-hold")
        await asyncio.sleep(0.02)

        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_E, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_E, 0),
        ]
    @pytest.mark.asyncio
    async def test_clear_combo_runtime_releases_active_pattern_superkey_hold(self) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()
        binding = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern-clear",
                mode=SuperkeyMode.PATTERN,
                hold_threshold_ms=0,
                hold_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
            ),
        )
        manager.active_combos = [
            dm.RuntimeCombo(
                id="combo-pattern-clear",
                name="combo-pattern-clear",
                steps=[dm.RuntimeComboStep(bindings=(binding,))],
                action=action,
            )
        ]

        await _runtime_start_combo_action(manager, "combo-pattern-clear", action, binding)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await _runtime_clear_combo_runtime(manager)

        assert manager.combo_state.superkey_machines == {}
        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]
    @pytest.mark.asyncio
    async def test_clear_combo_scope_stops_pending_pattern_superkey_machine(self) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()
        binding = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern-scope",
                mode=SuperkeyMode.PATTERN,
                tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
                double_tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_b")],
            ),
        )
        manager.active_combos = [
            dm.RuntimeCombo(
                id="combo-pattern-scope",
                name="combo-pattern-scope",
                steps=[dm.RuntimeComboStep(bindings=(binding,))],
                action=action,
            )
        ]

        await _runtime_start_combo_action(manager, "combo-pattern-scope", action, binding)
        await _runtime_stop_combo_action(manager, "combo-pattern-scope")

        assert "combo-pattern-scope" in manager.combo_state.superkey_machines

        await _runtime_clear_combo_scope(manager, "1234:5678", "kbd")

        assert manager.combo_state.superkey_machines == {}

    @pytest.mark.asyncio
    async def test_clear_combo_scope_keeps_wildcard_pattern_machine_for_other_device(
        self,
    ) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()
        configured = dm.RuntimeComboBinding(hardware_id="", source="", evdev="key_a")
        trigger = dm.RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_a",
        )
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern-wildcard-scope",
                mode=SuperkeyMode.PATTERN,
                tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
                double_tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_b")],
            ),
        )
        manager.active_combos = [
            dm.RuntimeCombo(
                id="combo-pattern-wildcard-scope",
                name="combo-pattern-wildcard-scope",
                steps=[dm.RuntimeComboStep(bindings=(configured,))],
                action=action,
            )
        ]

        await _runtime_start_combo_action(
            manager,
            "combo-pattern-wildcard-scope",
            action,
            trigger,
        )

        await _runtime_clear_combo_scope(manager, "9999:0001", "kbd")

        assert "combo-pattern-wildcard-scope" in manager.combo_state.active_actions
        assert "combo-pattern-wildcard-scope" in manager.combo_state.superkey_machines

        await _runtime_stop_combo_action(manager, "combo-pattern-wildcard-scope")
        assert "combo-pattern-wildcard-scope" in manager.combo_state.superkey_machines

        await _runtime_clear_combo_scope(manager, "9999:0001", "kbd")
        assert "combo-pattern-wildcard-scope" in manager.combo_state.superkey_machines

        await _runtime_clear_combo_scope(manager, "1234:5678", "kbd")

        assert manager.combo_state.superkey_machines == {}
        assert manager.combo_state.superkey_machine_bindings == {}

    @pytest.mark.asyncio
    async def test_combo_pattern_superkey_recreates_cached_machine_for_new_trigger_device(
        self,
    ) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()
        configured = dm.RuntimeComboBinding(hardware_id="", source="", evdev="key_a")
        trigger_a = dm.RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_a",
        )
        trigger_b = dm.RuntimeComboBinding(
            hardware_id="9999:0001",
            source="kbd",
            evdev="key_a",
        )
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern-wildcard-reuse",
                mode=SuperkeyMode.PATTERN,
                tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
                double_tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_b")],
            ),
        )
        manager.active_combos = [
            dm.RuntimeCombo(
                id="combo-pattern-wildcard-reuse",
                name="combo-pattern-wildcard-reuse",
                steps=[dm.RuntimeComboStep(bindings=(configured,))],
                action=action,
            )
        ]

        await _runtime_start_combo_action(
            manager,
            "combo-pattern-wildcard-reuse",
            action,
            trigger_a,
        )
        await _runtime_stop_combo_action(manager, "combo-pattern-wildcard-reuse")
        first_machine = manager.combo_state.superkey_machines[
            "combo-pattern-wildcard-reuse"
        ]

        await _runtime_start_combo_action(
            manager,
            "combo-pattern-wildcard-reuse",
            action,
            trigger_b,
        )
        second_machine = manager.combo_state.superkey_machines[
            "combo-pattern-wildcard-reuse"
        ]

        assert second_machine is not first_machine
        assert second_machine.source_device == "9999:0001"
        assert (
            manager.combo_state.superkey_machine_bindings["combo-pattern-wildcard-reuse"]
            == (trigger_b,)
        )

    @pytest.mark.asyncio
    async def test_clear_combo_scope_checks_all_cached_pattern_trigger_bindings(
        self,
    ) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()
        configured_a = dm.RuntimeComboBinding(hardware_id="", source="", evdev="key_a")
        configured_b = dm.RuntimeComboBinding(hardware_id="", source="", evdev="key_b")
        trigger_a = dm.RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_a",
        )
        trigger_b = dm.RuntimeComboBinding(
            hardware_id="9999:0001",
            source="kbd",
            evdev="key_b",
        )
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern-multi-scope",
                mode=SuperkeyMode.PATTERN,
                tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
                double_tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_b")],
            ),
        )
        manager.active_combos = [
            dm.RuntimeCombo(
                id="combo-pattern-multi-scope",
                name="combo-pattern-multi-scope",
                steps=[dm.RuntimeComboStep(bindings=(configured_a, configured_b))],
                action=action,
            )
        ]

        await _runtime_start_combo_action(
            manager,
            "combo-pattern-multi-scope",
            action,
            trigger_b,
            trigger_bindings=(trigger_a, trigger_b),
        )
        await _runtime_stop_combo_action(manager, "combo-pattern-multi-scope")

        assert (
            manager.combo_state.superkey_machine_bindings["combo-pattern-multi-scope"]
            == (trigger_a, trigger_b)
        )

        await _runtime_clear_combo_scope(manager, "1234:5678", "kbd")

        assert manager.combo_state.superkey_machines == {}
        assert manager.combo_state.superkey_machine_bindings == {}

    @pytest.mark.asyncio
    async def test_combo_pattern_superkey_replaces_stale_cached_machine_when_config_changes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        binding = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        old_config = SuperkeyConfig(
            name="combo-pattern-stale",
            mode=SuperkeyMode.PATTERN,
            tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
        )
        new_config = SuperkeyConfig(
            name="combo-pattern-stale",
            mode=SuperkeyMode.PATTERN,
            tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_b")],
        )
        old_machine = SimpleNamespace(
            config=old_config,
            event_name="combo:combo-pattern-stale",
            stop=AsyncMock(),
        )
        created: list[object] = []

        class _FakeMachine:
            def __init__(self, **kwargs) -> None:
                self.config = kwargs["config"]
                self.event_name = kwargs["event_name"]
                self.stop = AsyncMock()
                self.on_down = AsyncMock()
                self.on_up = AsyncMock()
                created.append(self)

        monkeypatch.setattr(cdm, "SuperkeyMachine", _FakeMachine)
        manager.combo_state.superkey_machines["combo-pattern-stale"] = old_machine  # type: ignore[assignment]
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=new_config,
        )
        manager.active_combos = [
            dm.RuntimeCombo(
                id="combo-pattern-stale",
                name="combo-pattern-stale",
                steps=[dm.RuntimeComboStep(bindings=(binding,))],
                action=action,
            )
        ]

        await _runtime_start_combo_action(manager, "combo-pattern-stale", action, binding)

        old_machine.stop.assert_awaited_once()
        assert len(created) == 1
        assert manager.combo_state.superkey_machines["combo-pattern-stale"] is created[0]
    @pytest.mark.asyncio
    async def test_start_and_stop_combo_action_cover_additional_synthetic_paths(self) -> None:
        manager = DeviceManager()
        manager.output_state.mouse_uinput = _FakeUInput()
        manager.output_state.gamepad_uinput = _FakeUInput()
        manager.play_macro = AsyncMock(return_value={"status": "ok"})  # type: ignore[method-assign]
        resolve_code = Mock(return_value=evdev.ecodes.BTN_SOUTH)

        binding = dm.RuntimeComboBinding(hardware_id="1234:5678", source="mouse", evdev="btn_side")

        await _runtime_start_combo_action(
            manager,
            "mouse-move",
            dm.MappingAction(action_type=ActionType.MOUSE_MOVE_REL, move_x=3, move_y=-1),
            binding,
        )
        await _runtime_start_combo_action(
            manager,
            "wheel",
            dm.MappingAction(action_type=ActionType.MOUSE, target="rel_wheel:1"),
            binding,
        )
        await _runtime_start_combo_action(
            manager,
            "tap-wheel",
            dm.MappingAction(
                action_type=ActionType.MOUSE,
                target="rel_wheel:-1",
                tap_enabled=True,
                tap_hold_ms=1,
            ),
            binding,
        )
        await asyncio.sleep(0.01)
        await _runtime_start_combo_action(
            manager,
            "rapid-wheel",
            dm.MappingAction(
                action_type=ActionType.MOUSE,
                target="rel_hwheel:1",
                rapidfire_enabled=True,
                rapidfire_hold_ms=1,
                rapidfire_wait_ms=1,
            ),
            binding,
        )
        await asyncio.sleep(0.01)
        await _runtime_stop_combo_action(manager, "rapid-wheel")
        await _runtime_start_combo_action(
            manager,
            "macro",
            dm.MappingAction(
                action_type=ActionType.MACRO,
                macro_name="demo",
                macro_loop_mode="hold",
            ),
            binding,
        )
        await _runtime_start_combo_action(
            manager,
            "exec",
            dm.MappingAction(action_type=ActionType.EXEC, exec_ref=9),
            binding,
        )
        await _runtime_start_combo_action(
            manager,
            "dispatch",
            dm.MappingAction(
                action_type=ActionType.COMPOSITOR_DISPATCH,
                compositor_dispatcher="workspace",
                compositor_args="2",
            ),
            binding,
        )
        await _runtime_start_combo_action(
            manager,
            "record",
            dm.MappingAction(action_type=ActionType.START_MACRO_RECORDING),
            binding,
        )
        await _runtime_start_combo_action(
            manager,
            "profile",
            dm.MappingAction(action_type=ActionType.PROFILE_ENABLE, profile_name="Gaming"),
            binding,
        )
        await _runtime_start_combo_action(
            manager,
            "axis",
            dm.MappingAction(
                action_type=ActionType.GAMEPAD_AXIS,
                target="abs_z",
                axis_value=255,
            ),
            binding,
            resolve_code_fn=resolve_code,
        )
        await _runtime_stop_combo_action(manager, "axis")
        await _runtime_start_combo_action(
            manager,
            "tap-axis",
            dm.MappingAction(
                action_type=ActionType.GAMEPAD_AXIS,
                target="abs_z",
                axis_value=255,
                tap_enabled=True,
                tap_hold_ms=25,
            ),
            binding,
            resolve_code_fn=resolve_code,
        )
        await _runtime_stop_combo_action(manager, "tap-axis")
        await _runtime_start_combo_action(
            manager,
            "rapid-axis",
            dm.MappingAction(
                action_type=ActionType.GAMEPAD_AXIS,
                target="abs_z",
                axis_value=255,
                rapidfire_enabled=True,
                rapidfire_hold_ms=10,
                rapidfire_wait_ms=10,
            ),
            binding,
            resolve_code_fn=resolve_code,
        )
        await _runtime_stop_combo_action(manager, "rapid-axis")
        await _runtime_stop_combo_action(manager, "macro")

        assert manager.play_macro.await_count == 2
        assert manager.play_macro.await_args_list[0].kwargs["trigger_value"] == 1
        assert manager.play_macro.await_args_list[1].kwargs["trigger_value"] == 0
        assert manager.output_state.mouse_uinput.writes[0] == (
            evdev.ecodes.EV_REL,
            evdev.ecodes.REL_X,
            3,
        )
        assert manager.output_state.mouse_uinput.writes[1] == (
            evdev.ecodes.EV_REL,
            evdev.ecodes.REL_Y,
            -1,
        )
        assert (
            evdev.ecodes.EV_REL,
            evdev.ecodes.REL_WHEEL,
            1,
        ) in manager.output_state.mouse_uinput.writes
        assert (
            evdev.ecodes.EV_REL,
            evdev.ecodes.REL_WHEEL,
            -1,
        ) in manager.output_state.mouse_uinput.writes
        assert (
            evdev.ecodes.EV_REL,
            evdev.ecodes.REL_HWHEEL,
            1,
        ) in manager.output_state.mouse_uinput.writes

    @pytest.mark.asyncio
    async def test_start_combo_action_mouse_move_abs_uses_cursor_position_setter(self) -> None:
        manager = DeviceManager()
        manager.output_state.mouse_uinput = _FakeUInput()
        manager.set_cursor_position = AsyncMock(return_value={"status": "ok"})  # type: ignore[method-assign]
        binding = dm.RuntimeComboBinding(hardware_id="1234:5678", source="mouse", evdev="btn_side")

        await _runtime_start_combo_action(
            manager,
            "mouse-move-abs",
            dm.MappingAction(action_type=ActionType.MOUSE_MOVE_ABS, move_x=33, move_y=44),
            binding,
        )

        manager.set_cursor_position.assert_awaited_once_with(33, 44)
        assert manager.output_state.mouse_uinput.writes == []

    @pytest.mark.asyncio
    async def test_combo_pattern_superkey_passes_cursor_position_setter(self, monkeypatch) -> None:
        manager = DeviceManager()
        binding = dm.RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        action = dm.MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern-abs",
                mode=SuperkeyMode.PATTERN,
                tap_actions=[
                    SuperkeyActionData(action_type="mouse_move_abs", move_x=12, move_y=34)
                ],
            ),
        )
        created_setters: list[object] = []

        class _FakeMachine:
            def __init__(self, **kwargs) -> None:
                self.config = kwargs["config"]
                self.event_name = kwargs["event_name"]
                self.stop = AsyncMock()
                self.on_down = AsyncMock()
                self.on_up = AsyncMock()
                created_setters.append(kwargs.get("cursor_position_setter"))

        monkeypatch.setattr(cdm, "SuperkeyMachine", _FakeMachine)

        await _runtime_start_combo_action(manager, "combo-pattern-abs", action, binding)

        assert created_setters == [manager.set_cursor_position]
