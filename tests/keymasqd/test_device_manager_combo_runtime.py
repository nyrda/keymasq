# ruff: noqa: F403, F405, I001
from tests.keymasqd.device_manager_support import *

class TestCombos:
    @pytest.mark.asyncio
    async def test_combo_overlapping_first_step_combos_all_trigger(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        hardware_id = "1234:5678"
        passthrough = _FakeUInput()
        keyboard = _FakeUInput()

        await manager.set_combos(
            [
                {
                    "id": "combo-c",
                    "name": "combo-c",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_leftalt",
                                },
                                {"hardware_id": hardware_id, "source": "kbd", "evdev": "key_c"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                },
                {
                    "id": "combo-v",
                    "name": "combo-v",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_leftalt",
                                },
                                {"hardware_id": hardware_id, "source": "kbd", "evdev": "key_v"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f14"},
                },
                {
                    "id": "combo-c-v",
                    "name": "combo-c-v",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_leftalt",
                                },
                                {"hardware_id": hardware_id, "source": "kbd", "evdev": "key_c"},
                                {"hardware_id": hardware_id, "source": "kbd", "evdev": "key_v"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f15"},
                },
            ]
        )
        manager.output_state.keyboard_uinput = keyboard

        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id=hardware_id,
            button_map={
                "key_leftalt": "key_leftalt",
                "key_c": "key_c",
                "key_v": "key_v",
            },
            mapping_getter=lambda: {},
            event_callback=lambda *args, **kwargs: _runtime_on_device_event(
                manager, *args, **kwargs
            ),
            device_type=DeviceType.KEYBOARD,
        )
        device._running = True
        device.uinput = passthrough  # type: ignore[assignment]
        manager.grabbed_devices = {hardware_id: [device]}

        press_alt = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_LEFTALT,
            value=1,
        )
        press_c = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_C, value=1)
        press_v = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_V, value=1)

        await _runtime_process_grabbed_event(device, press_alt)
        await _runtime_process_grabbed_event(device, press_c)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(device, press_v)
        await asyncio.sleep(0)

        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F15, 1),
        ]
    @pytest.mark.asyncio
    async def test_combo_overlapping_multistep_first_step_releases_outputs_cleanly(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        hardware_id = "1234:5678"
        passthrough = _FakeUInput()
        keyboard = _FakeUInput()

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "combo-1",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_leftmeta",
                                },
                                {"hardware_id": hardware_id, "source": "kbd", "evdev": "key_a"},
                            ]
                        },
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_1",
                                }
                            ]
                        },
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                },
                {
                    "id": "combo-2",
                    "name": "combo-2",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_leftmeta",
                                },
                                {"hardware_id": hardware_id, "source": "kbd", "evdev": "key_a"},
                            ]
                        },
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_2",
                                }
                            ]
                        },
                    ],
                    "action": {"action": "keyboard", "target": "key_f14"},
                },
            ]
        )
        manager.output_state.keyboard_uinput = keyboard

        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id=hardware_id,
            button_map={
                "key_leftmeta": "key_leftmeta",
                "key_a": "key_a",
                "key_1": "key_1",
                "key_2": "key_2",
            },
            mapping_getter=lambda: {},
            event_callback=lambda *args, **kwargs: _runtime_on_device_event(
                manager, *args, **kwargs
            ),
            device_type=DeviceType.KEYBOARD,
        )
        device._running = True
        device.uinput = passthrough  # type: ignore[assignment]
        manager.grabbed_devices = {hardware_id: [device]}

        events = [
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_LEFTMETA, value=1),
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_A, value=1),
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_A, value=0),
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_LEFTMETA, value=0),
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_1, value=1),
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_1, value=0),
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_2, value=1),
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_2, value=0),
        ]

        for event in events:
            await _runtime_process_grabbed_event(device, event)
            await asyncio.sleep(0)

        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTMETA, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTMETA, 0),
        ]
        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 0),
        ]
        assert manager.combo_state.active_actions == {}
        assert manager.combo_state.engine._candidates == {}
        assert device.state.combo_passthrough_held == set()
        assert device.state.held_output_keys["passthrough"] == set()
        assert device.state.held_output_keys["keyboard"] == set()
    @pytest.mark.asyncio
    async def test_combo_overlapping_multistep_first_step_can_hold_second_step_outputs_together(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        hardware_id = "1234:5678"
        passthrough = _FakeUInput()
        keyboard = _FakeUInput()

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "combo-1",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_leftmeta",
                                },
                                {"hardware_id": hardware_id, "source": "kbd", "evdev": "key_a"},
                            ]
                        },
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_1",
                                }
                            ]
                        },
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                },
                {
                    "id": "combo-2",
                    "name": "combo-2",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_leftmeta",
                                },
                                {"hardware_id": hardware_id, "source": "kbd", "evdev": "key_a"},
                            ]
                        },
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_2",
                                }
                            ]
                        },
                    ],
                    "action": {"action": "keyboard", "target": "key_f14"},
                },
            ]
        )
        manager.output_state.keyboard_uinput = keyboard

        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id=hardware_id,
            button_map={
                "key_leftmeta": "key_leftmeta",
                "key_a": "key_a",
                "key_1": "key_1",
                "key_2": "key_2",
            },
            mapping_getter=lambda: {},
            event_callback=lambda *args, **kwargs: _runtime_on_device_event(
                manager, *args, **kwargs
            ),
            device_type=DeviceType.KEYBOARD,
        )
        device._running = True
        device.uinput = passthrough  # type: ignore[assignment]
        manager.grabbed_devices = {hardware_id: [device]}

        events = [
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_LEFTMETA, value=1),
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_A, value=1),
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_LEFTMETA, value=0),
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_A, value=0),
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_1, value=1),
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_2, value=1),
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_1, value=0),
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_2, value=0),
        ]

        for event in events:
            await _runtime_process_grabbed_event(device, event)
            await asyncio.sleep(0)

        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTMETA, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTMETA, 0),
        ]
        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 0),
        ]
        assert manager.combo_state.active_actions == {}
        assert manager.combo_state.engine._candidates == {}
        assert device.state.combo_passthrough_held == set()
        assert device.state.held_output_keys["passthrough"] == set()
        assert device.state.held_output_keys["keyboard"] == set()
    @pytest.mark.asyncio
    async def test_combo_recall_restore_non_modifier_pressed_first_releases_cleanly(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        hardware_id = "1234:5678"
        passthrough = _FakeUInput()
        keyboard = _FakeUInput()

        await manager.set_combos(
            [
                {
                    "id": "combo-zx",
                    "name": "combo-zx",
                    "steps": [
                        {
                            "events": [
                                {"hardware_id": hardware_id, "source": "kbd", "evdev": "key_x"},
                                {"hardware_id": hardware_id, "source": "kbd", "evdev": "key_z"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                    "recall_trigger_keys": True,
                    "restore_trigger_keys": ["key_z"],
                }
            ]
        )
        manager.output_state.keyboard_uinput = keyboard

        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id=hardware_id,
            button_map={"key_x": "key_x", "key_z": "key_z"},
            mapping_getter=lambda: {},
            event_callback=lambda *args, **kwargs: _runtime_on_device_event(
                manager, *args, **kwargs
            ),
            device_type=DeviceType.KEYBOARD,
        )
        device._running = True
        device.uinput = passthrough  # type: ignore[assignment]
        manager.grabbed_devices = {hardware_id: [device]}

        events = [
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_Z, value=1),
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=1),
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=0),
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_Z, value=0),
        ]

        for event in events:
            await _runtime_process_grabbed_event(device, event)
            await asyncio.sleep(0)

        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_Z, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_Z, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_Z, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_Z, 0),
        ]
        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0),
        ]
        assert manager.combo_state.active_actions == {}
        assert manager.combo_state.engine._candidates == {}
        assert device.state.combo_passthrough_held == set()
        assert device.state.combo_recalled_bindings == set()
        assert device.state.held_output_keys["passthrough"] == set()
        assert device.state.held_output_keys["keyboard"] == set()
        assert device.state.held_source_keys == set()
    @pytest.mark.asyncio
    async def test_combo_restore_selected_trigger_skips_repress_after_physical_release(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        hardware_id = "1234:5678"
        passthrough = _FakeUInput()
        keyboard = _FakeUInput()

        await manager.set_combos(
            [
                {
                    "id": "combo-xz",
                    "name": "combo-xz",
                    "steps": [
                        {
                            "events": [
                                {"hardware_id": hardware_id, "source": "kbd", "evdev": "key_x"},
                                {"hardware_id": hardware_id, "source": "kbd", "evdev": "key_z"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                    "recall_trigger_keys": True,
                    "restore_trigger_keys": ["key_x"],
                }
            ]
        )
        manager.output_state.keyboard_uinput = keyboard

        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id=hardware_id,
            button_map={"key_x": "key_x", "key_z": "key_z"},
            mapping_getter=lambda: {},
            event_callback=lambda *args, **kwargs: _runtime_on_device_event(
                manager, *args, **kwargs
            ),
            device_type=DeviceType.KEYBOARD,
        )
        device._running = True
        device.uinput = passthrough  # type: ignore[assignment]
        manager.grabbed_devices = {hardware_id: [device]}

        events = [
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=1),
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_Z, value=1),
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=0),
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_Z, value=0),
        ]

        for event in events:
            await _runtime_process_grabbed_event(device, event)
            await asyncio.sleep(0)

        assert passthrough.writes.count(
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 1)
        ) == 1
        assert passthrough.writes.count(
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 0)
        ) == 1
        assert passthrough.writes[-1] != (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 1)
        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0),
        ]
        assert device.state.combo_passthrough_held == set()
        assert device.state.combo_recalled_bindings == set()
        assert device.state.held_source_keys == set()
        assert device.state.held_output_keys["passthrough"] == set()
        assert manager.combo_state.active_actions == {}
        assert manager.combo_state.engine._candidates == {}
    @pytest.mark.asyncio
    async def test_runtime_combo_broadcast_does_not_block_hot_path(self, monkeypatch):
        manager = DeviceManager()
        blocker = asyncio.Event()

        async def stalled_callback(_command, _data):
            await blocker.wait()

        manager.broadcast_callback = stalled_callback
        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Quick Toggle",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "mouse",
                                    "evdev": "btn_side",
                                }
                            ]
                        }
                    ],
                    "action": {"action": "profile_toggle", "profile_name": "Gaming"},
                }
            ]
        )

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "mouse")

        result = await asyncio.wait_for(
            _runtime_on_device_event(
                manager,
                "1234:5678",
                "/dev/input/by-id/test-mouse",
                evdev.ecodes.EV_KEY,
                evdev.ecodes.BTN_SIDE,
                1,
            ),
            timeout=0.05,
        )

        assert result is not None and result.consume_current_event is True
        blocker.set()
        await asyncio.sleep(0)
    @pytest.mark.asyncio
    async def test_runtime_combo_keyboard_action_mirrors_press_and_release(self, monkeypatch):
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = Mock()

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Quick Save",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_f13",
                                }
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f5"},
                }
            ]
        )

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

        pressed = await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            1,
        )
        released = await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            0,
        )

        assert pressed is not None and pressed.consume_current_event is True
        assert released is not None and released.consume_current_event is True
        assert manager.output_state.keyboard_uinput.write.call_args_list[0].args == (
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F5,
            1,
        )
        assert manager.output_state.keyboard_uinput.write.call_args_list[1].args == (
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F5,
            0,
        )
    @pytest.mark.asyncio
    async def test_runtime_combo_tap_key_releases_when_runtime_clears(self, monkeypatch):
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Quick Save",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_f13",
                                }
                            ]
                        }
                    ],
                    "action": {
                        "action": "keyboard",
                        "target": "key_f5",
                        "tap_enabled": True,
                        "tap_hold_ms": 1000,
                    },
                }
            ]
        )

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

        pressed = await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            1,
        )
        await asyncio.sleep(0)
        await _runtime_clear_combo_runtime(manager)

        assert pressed is not None and pressed.consume_current_event is True
        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F5, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F5, 0),
        ]

    @pytest.mark.asyncio
    async def test_runtime_combo_mouse_button_plus_wheel_pulse_fires_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()

        await manager.set_combos(
            [
                {
                    "id": "combo-wheel",
                    "name": "Back Wheel",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "mouse",
                                    "evdev": "btn_side",
                                },
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "mouse",
                                    "evdev": "wheel_up",
                                },
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f5"},
                }
            ]
        )

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "mouse")

        await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-mouse",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.BTN_SIDE,
            1,
        )
        decision = await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-mouse",
            evdev.ecodes.EV_REL,
            evdev.ecodes.REL_WHEEL,
            1,
        )

        assert decision is not None and decision.consume_current_event is True
        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F5, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F5, 0),
        ]

    @pytest.mark.asyncio
    async def test_runtime_wheel_pulse_waits_for_tap_action_to_start(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()

        await manager.set_combos(
            [
                {
                    "id": "combo-wheel-tap",
                    "name": "Wheel Tap",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "mouse",
                                    "evdev": "wheel_up",
                                },
                            ]
                        }
                    ],
                    "action": {
                        "action": "keyboard",
                        "target": "key_f5",
                        "tap_enabled": True,
                        "tap_hold_ms": 1000,
                    },
                }
            ]
        )

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "mouse")

        decision = await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-mouse",
            evdev.ecodes.EV_REL,
            evdev.ecodes.REL_WHEEL,
            1,
        )

        assert decision is not None and decision.consume_current_event is True
        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F5, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F5, 0),
        ]
        assert manager.combo_state.active_actions == {}

    @pytest.mark.asyncio
    async def test_runtime_wheel_pulse_waits_for_overload_tap_child_to_start(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()

        await manager.set_combos(
            [
                {
                    "id": "combo-wheel-overload-tap",
                    "name": "Wheel Overload Tap",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "mouse",
                                    "evdev": "wheel_up",
                                },
                            ]
                        }
                    ],
                    "action": {
                        "action": "superkey",
                        "superkey": {
                            "name": "wheel-overload",
                            "mode": "overload",
                            "overload_actions": [
                                {
                                    "action": "keyboard",
                                    "target": "key_f5",
                                    "tap_enabled": True,
                                    "tap_hold_ms": 1000,
                                }
                            ],
                        },
                    },
                }
            ]
        )

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "mouse")

        decision = await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-mouse",
            evdev.ecodes.EV_REL,
            evdev.ecodes.REL_WHEEL,
            1,
        )

        assert decision is not None and decision.consume_current_event is True
        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F5, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F5, 0),
        ]
        assert manager.combo_state.active_actions == {}

    @pytest.mark.asyncio
    async def test_runtime_combo_wheel_suppresses_matching_high_res_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rel_wheel_hi_res = getattr(evdev.ecodes, "REL_WHEEL_HI_RES", None)
        if rel_wheel_hi_res is None:
            pytest.skip("kernel headers do not expose REL_WHEEL_HI_RES")

        manager = DeviceManager()
        await manager.set_combos(
            [
                {
                    "id": "combo-wheel",
                    "name": "Wheel",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "mouse",
                                    "evdev": "wheel_down",
                                }
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f5"},
                }
            ]
        )

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "mouse")

        consumed = await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-mouse",
            evdev.ecodes.EV_REL,
            int(rel_wheel_hi_res),
            -120,
        )
        unmapped = await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-mouse",
            evdev.ecodes.EV_REL,
            int(rel_wheel_hi_res),
            120,
        )

        assert consumed is True
        assert unmapped is None

    @pytest.mark.asyncio
    async def test_runtime_combo_wheel_passthrough_until_it_completes_combo(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rel_wheel_hi_res = getattr(evdev.ecodes, "REL_WHEEL_HI_RES", None)
        if rel_wheel_hi_res is None:
            pytest.skip("kernel headers do not expose REL_WHEEL_HI_RES")

        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()
        await manager.set_combos(
            [
                {
                    "id": "combo-wheel",
                    "name": "Back Wheel",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "mouse",
                                    "evdev": "btn_side",
                                },
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "mouse",
                                    "evdev": "wheel_down",
                                },
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f5"},
                }
            ]
        )

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "mouse")

        idle_high_res = await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-mouse",
            evdev.ecodes.EV_REL,
            int(rel_wheel_hi_res),
            -120,
        )
        idle_low_res = await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-mouse",
            evdev.ecodes.EV_REL,
            evdev.ecodes.REL_WHEEL,
            -1,
        )
        await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-mouse",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.BTN_SIDE,
            1,
        )
        completing_high_res = await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-mouse",
            evdev.ecodes.EV_REL,
            int(rel_wheel_hi_res),
            -120,
        )
        completing_low_res = await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-mouse",
            evdev.ecodes.EV_REL,
            evdev.ecodes.REL_WHEEL,
            -1,
        )

        assert idle_high_res is None
        assert idle_low_res is not None and idle_low_res.passthrough_current_event is True
        assert completing_high_res is True
        assert completing_low_res is not None and completing_low_res.consume_current_event is True
        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F5, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F5, 0),
        ]

    @pytest.mark.asyncio
    async def test_runtime_combo_refresh_preserves_unchanged_active_combo_when_payload_expands(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        keyboard_uinput = _FakeUInput()
        manager.output_state.keyboard_uinput = keyboard_uinput
        hardware_id = "1234:5678"
        combo_payload = {
            "id": "combo-hold",
            "name": "Hold F13",
            "steps": [
                {
                    "events": [
                        {"hardware_id": hardware_id, "source": "kbd", "evdev": "key_f13"},
                    ]
                }
            ],
            "action": {"action": "keyboard", "target": "key_f14"},
        }
        unrelated_payload = {
            "id": "combo-unrelated",
            "name": "Unrelated",
            "steps": [
                {
                    "events": [
                        {"hardware_id": hardware_id, "source": "kbd", "evdev": "key_f15"},
                    ]
                }
            ],
            "action": {"action": "keyboard", "target": "key_f16"},
        }

        await manager.set_combos([combo_payload])
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

        pressed = await _runtime_on_device_event(
            manager,
            hardware_id,
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            1,
        )
        assert pressed is not None and pressed.consume_current_event is True
        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
        ]

        await manager.set_combos([combo_payload, unrelated_payload])

        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
        ]

        released = await _runtime_on_device_event(
            manager,
            hardware_id,
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            0,
        )

        assert released is not None and released.consume_current_event is True
        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 0),
        ]

    @pytest.mark.asyncio
    async def test_runtime_combo_refresh_stops_changed_active_combo(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        keyboard_uinput = _FakeUInput()
        manager.output_state.keyboard_uinput = keyboard_uinput
        hardware_id = "1234:5678"
        combo_payload = {
            "id": "combo-hold",
            "name": "Hold F13",
            "steps": [
                {
                    "events": [
                        {"hardware_id": hardware_id, "source": "kbd", "evdev": "key_f13"},
                    ]
                }
            ],
            "action": {"action": "keyboard", "target": "key_f14"},
        }
        changed_payload = {
            **combo_payload,
            "action": {"action": "keyboard", "target": "key_f15"},
        }

        await manager.set_combos([combo_payload])
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

        await _runtime_on_device_event(
            manager,
            hardware_id,
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            1,
        )
        await manager.set_combos([changed_payload])

        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 0),
        ]

    @pytest.mark.asyncio
    async def test_grab_refresh_preserves_unchanged_active_combo(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        keyboard_uinput = _FakeUInput()
        manager.output_state.keyboard_uinput = keyboard_uinput
        hardware_id = "1234:5678"
        combo_payload = {
            "id": "combo-hold",
            "name": "Hold F13",
            "steps": [
                {
                    "events": [
                        {"hardware_id": hardware_id, "source": "kbd", "evdev": "key_f13"},
                    ]
                }
            ],
            "action": {"action": "keyboard", "target": "key_f14"},
        }

        await manager.set_combos([combo_payload])
        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

        await _runtime_on_device_event(
            manager,
            hardware_id,
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            1,
        )

        await manager._refresh_combo_runtime_preserving_unchanged()

        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
        ]

        await _runtime_on_device_event(
            manager,
            hardware_id,
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            0,
        )

        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 0),
        ]
