# ruff: noqa: F403, F405, I001
from tests.keymasqd.integration_support import *


@pytest.mark.skipif(not os.access("/dev/uinput", os.W_OK), reason="No uinput access")
@pytest.mark.asyncio
class TestIntegrationCombos(IntegrationTestBase):
    async def test_combo_single_step_is_transparent_until_match_then_recalls(
        self,
        full_system,
        virtual_keyboard,
    ):
        _server, manager = full_system
        keyboard_path = virtual_keyboard.device.path
        hardware_id = "abcd:ef01"

        result = await manager.grab_device(
            hardware_id=hardware_id,
            evdev_paths=[keyboard_path],
            button_map={
                "key_leftctrl": "key_leftctrl",
                "key_a": "key_a",
            },
        )
        assert result["grabbed"] is True

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Quick Action",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "evdev": "key_leftctrl",
                                },
                                {
                                    "hardware_id": hardware_id,
                                    "evdev": "key_a",
                                },
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                }
            ]
        )

        manager.output_state.keyboard_uinput = Mock()
        grabbed = manager.grabbed_devices[hardware_id][0]

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 1)
        virtual_keyboard.syn()
        await self._wait_until(
            lambda: evdev.ecodes.KEY_LEFTCTRL
            in grabbed.state.held_output_keys["passthrough"],
            reason="left ctrl passthrough hold",
        )

        assert evdev.ecodes.KEY_LEFTCTRL in grabbed.state.held_output_keys["passthrough"]
        assert manager.output_state.keyboard_uinput.write.call_count == 0

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)
        virtual_keyboard.syn()
        await self._wait_until(
            lambda: manager.output_state.keyboard_uinput.write.call_count >= 1,
            reason="combo action press",
        )

        assert evdev.ecodes.KEY_LEFTCTRL in grabbed.state.held_output_keys["passthrough"]
        assert evdev.ecodes.KEY_A not in grabbed.state.held_output_keys["passthrough"]
        assert manager.output_state.keyboard_uinput.write.call_args_list[0].args == (
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            1,
        )

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0)
        virtual_keyboard.syn()
        await self._wait_until(
            lambda: manager.output_state.keyboard_uinput.write.call_count >= 2,
            reason="combo action release",
        )

        assert manager.output_state.keyboard_uinput.write.call_args_list[1].args == (
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            0,
        )

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 0)
        virtual_keyboard.syn()
        await self._wait_until(
            lambda: grabbed.state.held_output_keys["passthrough"] == set(),
            reason="left ctrl passthrough release",
        )

        assert grabbed.state.held_output_keys["passthrough"] == set()

    async def test_combo_multi_step_timeout_starts_after_release_phase(
        self,
        full_system,
        virtual_keyboard,
    ):
        _server, manager = full_system
        keyboard_path = virtual_keyboard.device.path
        hardware_id = "abcd:ef01"

        result = await manager.grab_device(
            hardware_id=hardware_id,
            evdev_paths=[keyboard_path],
            button_map={
                "key_leftctrl": "key_leftctrl",
                "key_a": "key_a",
                "key_1": "key_1",
            },
        )
        assert result["grabbed"] is True

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Quick Action",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "evdev": "key_leftctrl",
                                },
                                {
                                    "hardware_id": hardware_id,
                                    "evdev": "key_a",
                                },
                            ]
                        },
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "evdev": "key_1",
                                }
                            ],
                            "timeout_ms": 80,
                        },
                    ],
                    "action": {"action": "keyboard", "target": "key_f14"},
                }
            ]
        )

        manager.output_state.keyboard_uinput = Mock()
        grabbed = manager.grabbed_devices[hardware_id][0]

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 1)
        virtual_keyboard.syn()
        await self._wait_until(
            lambda: evdev.ecodes.KEY_LEFTCTRL
            in grabbed.state.held_output_keys["passthrough"],
            reason="left ctrl passthrough hold",
        )
        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)
        virtual_keyboard.syn()
        await self._wait_until(
            lambda: any(
                candidate.releasing
                for candidate in manager.combo_state.engine._candidates.values()
            ),
            reason="combo first step release phase",
        )

        assert manager.combo_state.engine.next_deadline() is None

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0)
        virtual_keyboard.syn()
        await self._wait_until(
            lambda: any(
                candidate.releasing and len(candidate.pressed_bindings) == 1
                for candidate in manager.combo_state.engine._candidates.values()
            ),
            reason="combo first step partial release",
        )
        assert manager.combo_state.engine.next_deadline() is None

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 0)
        virtual_keyboard.syn()
        await self._wait_until(
            lambda: manager.combo_state.engine.next_deadline() is not None,
            reason="combo second step deadline",
        )
        assert manager.combo_state.engine.next_deadline() is not None

        await self._wait_until(
            lambda: manager.combo_state.engine.next_deadline() is None,
            reason="combo second step timeout",
        )
        assert manager.combo_state.engine.next_deadline() is None

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 1)
        virtual_keyboard.syn()
        await self._wait_until(
            lambda: evdev.ecodes.KEY_1 in grabbed.state.held_output_keys["passthrough"],
            reason="key 1 passthrough hold after timeout",
        )

        assert evdev.ecodes.KEY_1 in grabbed.state.held_output_keys["passthrough"]
        assert manager.output_state.keyboard_uinput.write.call_count == 0

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 0)
        virtual_keyboard.syn()
        await self._wait_until(
            lambda: grabbed.state.held_output_keys["passthrough"] == set(),
            reason="key 1 passthrough release",
        )

        assert grabbed.state.held_output_keys["passthrough"] == set()

    async def test_combo_multi_step_pattern_hold_starts_after_step_timeout_window(
        self,
        full_system,
        virtual_keyboard,
    ):
        _server, manager = full_system
        keyboard_path = virtual_keyboard.device.path
        hardware_id = "abcd:ef01"

        result = await manager.grab_device(
            hardware_id=hardware_id,
            evdev_paths=[keyboard_path],
            button_map={
                "key_leftctrl": "key_leftctrl",
                "key_a": "key_a",
                "key_1": "key_1",
            },
        )
        assert result["grabbed"] is True

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Pattern Hold",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "evdev": "key_leftctrl",
                                },
                                {
                                    "hardware_id": hardware_id,
                                    "evdev": "key_a",
                                },
                            ]
                        },
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "evdev": "key_1",
                                }
                            ],
                            "timeout_ms": 80,
                        },
                    ],
                    "action": {
                        "action": "superkey",
                        "superkey": {
                            "name": "combo-pattern",
                            "mode": "pattern",
                            "hold_threshold_ms": 20,
                            "hold_actions": [{"action": "keyboard", "target": "key_f15"}],
                            "double_tap_actions": [
                                {"action": "keyboard", "target": "key_f16"}
                            ],
                            "tap_hold_actions": [
                                {"action": "keyboard", "target": "key_f17"}
                            ],
                        },
                    },
                }
            ]
        )

        manager.output_state.keyboard_uinput = Mock()

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.05)
        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.05)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.05)
        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.05)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        assert manager.output_state.keyboard_uinput.write.call_args_list[0].args == (
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F15,
            1,
        )

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        assert manager.output_state.keyboard_uinput.write.call_args_list[1].args == (
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F15,
            0,
        )

    async def test_combo_single_step_rearms_when_modifier_stays_held(
        self,
        full_system,
        virtual_keyboard,
    ):
        _server, manager = full_system
        keyboard_path = virtual_keyboard.device.path
        hardware_id = "abcd:ef01"

        result = await manager.grab_device(
            hardware_id=hardware_id,
            evdev_paths=[keyboard_path],
            button_map={
                "key_leftctrl": "key_leftctrl",
                "key_1": "key_1",
            },
        )
        assert result["grabbed"] is True

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Repeatable Combo",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "evdev": "key_leftctrl",
                                },
                                {
                                    "hardware_id": hardware_id,
                                    "evdev": "key_1",
                                },
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                }
            ]
        )

        manager.output_state.keyboard_uinput = Mock()
        grabbed = manager.grabbed_devices[hardware_id][0]

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        press_release_values = [
            call.args[2]
            for call in manager.output_state.keyboard_uinput.write.call_args_list
            if call.args[:2] == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13)
        ]
        assert press_release_values == [1, 0, 1, 0]
        assert evdev.ecodes.KEY_LEFTCTRL in grabbed.state.held_output_keys["passthrough"]

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        assert grabbed.state.held_output_keys["passthrough"] == set()

    async def test_combo_single_step_releasing_non_completing_key_stops_action(
        self,
        full_system,
        virtual_keyboard,
    ):
        _server, manager = full_system
        keyboard_path = virtual_keyboard.device.path
        hardware_id = "abcd:ef01"

        result = await manager.grab_device(
            hardware_id=hardware_id,
            evdev_paths=[keyboard_path],
            button_map={
                "key_leftalt": "key_leftalt",
                "key_2": "key_2",
            },
        )
        assert result["grabbed"] is True

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Alt Two",
                    "steps": [
                        {
                            "events": [
                                {"hardware_id": hardware_id, "evdev": "key_leftalt"},
                                {"hardware_id": hardware_id, "evdev": "key_2"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                }
            ]
        )

        manager.output_state.keyboard_uinput = Mock()

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTALT, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_2, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTALT, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        press_release_values = [
            call.args[2]
            for call in manager.output_state.keyboard_uinput.write.call_args_list
            if call.args[:2] == (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13)
        ]
        assert press_release_values == [1, 0]

    async def test_combo_single_step_rearm_keeps_other_modifier_combos_available(
        self,
        full_system,
        virtual_keyboard,
    ):
        _server, manager = full_system
        keyboard_path = virtual_keyboard.device.path
        hardware_id = "abcd:ef01"

        result = await manager.grab_device(
            hardware_id=hardware_id,
            evdev_paths=[keyboard_path],
            button_map={
                "key_leftctrl": "key_leftctrl",
                "key_1": "key_1",
                "key_2": "key_2",
            },
        )
        assert result["grabbed"] is True

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "First Combo",
                    "steps": [
                        {
                            "events": [
                                {"hardware_id": hardware_id, "evdev": "key_leftctrl"},
                                {"hardware_id": hardware_id, "evdev": "key_1"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                },
                {
                    "id": "combo-2",
                    "name": "Second Combo",
                    "steps": [
                        {
                            "events": [
                                {"hardware_id": hardware_id, "evdev": "key_leftctrl"},
                                {"hardware_id": hardware_id, "evdev": "key_2"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f14"},
                },
            ]
        )

        manager.output_state.keyboard_uinput = Mock()

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)
        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_2, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)
        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_2, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        output_codes = [
            call.args[1]
            for call in manager.output_state.keyboard_uinput.write.call_args_list
            if call.args[0] == evdev.ecodes.EV_KEY and call.args[2] == 1
        ]
        assert evdev.ecodes.KEY_F13 in output_codes
        assert evdev.ecodes.KEY_F14 in output_codes

    async def test_combo_single_step_held_completing_key_allows_sibling_combo(
        self,
        full_system,
        virtual_keyboard,
    ):
        _server, manager = full_system
        keyboard_path = virtual_keyboard.device.path
        hardware_id = "abcd:ef01"

        result = await manager.grab_device(
            hardware_id=hardware_id,
            evdev_paths=[keyboard_path],
            button_map={
                "key_leftalt": "key_leftalt",
                "key_1": "key_1",
                "key_2": "key_2",
            },
        )
        assert result["grabbed"] is True

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "First Combo",
                    "steps": [
                        {
                            "events": [
                                {"hardware_id": hardware_id, "evdev": "key_leftalt"},
                                {"hardware_id": hardware_id, "evdev": "key_1"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                },
                {
                    "id": "combo-2",
                    "name": "Second Combo",
                    "steps": [
                        {
                            "events": [
                                {"hardware_id": hardware_id, "evdev": "key_leftalt"},
                                {"hardware_id": hardware_id, "evdev": "key_2"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f14"},
                },
            ]
        )

        manager.output_state.keyboard_uinput = Mock()

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTALT, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_2, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        output_presses = [
            call.args[1]
            for call in manager.output_state.keyboard_uinput.write.call_args_list
            if call.args[0] == evdev.ecodes.EV_KEY and call.args[2] == 1
        ]
        assert output_presses == [evdev.ecodes.KEY_F13, evdev.ecodes.KEY_F14]

    async def test_combo_single_step_releasing_one_active_sibling_stops_only_that_action(
        self,
        full_system,
        virtual_keyboard,
    ):
        _server, manager = full_system
        keyboard_path = virtual_keyboard.device.path
        hardware_id = "abcd:ef01"

        result = await manager.grab_device(
            hardware_id=hardware_id,
            evdev_paths=[keyboard_path],
            button_map={
                "key_leftalt": "key_leftalt",
                "key_1": "key_1",
                "key_2": "key_2",
            },
        )
        assert result["grabbed"] is True

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "First Combo",
                    "steps": [
                        {
                            "events": [
                                {"hardware_id": hardware_id, "evdev": "key_leftalt"},
                                {"hardware_id": hardware_id, "evdev": "key_1"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                },
                {
                    "id": "combo-2",
                    "name": "Second Combo",
                    "steps": [
                        {
                            "events": [
                                {"hardware_id": hardware_id, "evdev": "key_leftalt"},
                                {"hardware_id": hardware_id, "evdev": "key_2"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f14"},
                },
            ]
        )

        manager.output_state.keyboard_uinput = Mock()

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTALT, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_2, 1)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_1, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        virtual_keyboard.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_2, 0)
        virtual_keyboard.syn()
        await asyncio.sleep(0.08)

        writes = [
            call.args
            for call in manager.output_state.keyboard_uinput.write.call_args_list
            if call.args[0] == evdev.ecodes.EV_KEY
            and call.args[1] in {evdev.ecodes.KEY_F13, evdev.ecodes.KEY_F14}
        ]
        assert writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 0),
        ]
