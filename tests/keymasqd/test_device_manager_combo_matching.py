# ruff: noqa: F403, F405, I001
from tests.keymasqd.device_manager_support import *

class TestCombos:
    @pytest.mark.asyncio
    async def test_grabbed_device_combo_events_use_cached_identity_metadata(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()

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
                    "action": {"action": "suppress"},
                }
            ]
        )

        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: f"{path}-stable")
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"key_f13": "key_f13"},
            mapping_getter=lambda: {},
            event_callback=lambda *args, **kwargs: _runtime_on_device_event(
                manager, *args, **kwargs
            ),
            device_type=DeviceType.KEYBOARD,
        )

        def fail_resolve(_path: str) -> str:
            raise AssertionError("resolve_stable_path should not run in the hot event path")

        def fail_interface(_path: str) -> str:
            raise AssertionError("get_interface_id should not run in the hot event path")

        monkeypatch.setattr(gdm, "resolve_stable_path", fail_resolve)
        monkeypatch.setattr(gdm, "get_interface_id", fail_interface)

        decision = await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(
                type=evdev.ecodes.EV_KEY,
                code=evdev.ecodes.KEY_F13,
                value=1,
            ),
        )

        assert decision is None
    @pytest.mark.asyncio
    async def test_set_combos_parses_runtime_combo(self):
        manager = DeviceManager()

        result = await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Quick Toggle",
                    "profile_name": "Desktop",
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

        assert result == {"updated": True, "combo_count": 1}
        assert len(manager.active_combos) == 1
        assert manager.active_combos[0].action is not None
        assert manager.active_combos[0].action.profile_name == "Gaming"

    @pytest.mark.asyncio
    async def test_set_combos_allows_omitted_hardware_id_as_wildcard(self):
        manager = DeviceManager()

        await manager.set_combos(
            [
                {
                    "id": "combo-any-keyboard",
                    "name": "Any Keyboard",
                    "steps": [{"events": [{"source": "kbd", "evdev": "key_f13"}]}],
                    "action": {"action": "suppress"},
                }
            ]
        )

        assert manager.active_combos[0].steps[0].bindings[0].hardware_id == ""

        pressed = await _runtime_on_device_event(
            manager,
            "9999:0001",
            "/dev/input/event-test",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            1,
            source="kbd",
        )

        assert pressed is not None
        assert pressed.consume_current_event is True

    @pytest.mark.asyncio
    async def test_set_combos_reseeds_held_bindings_after_mapping_reset(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        callback = AsyncMock()

        async def cb(command, data):
            await callback(command, data)

        manager.broadcast_callback = cb

        device = _make_grabbed_device(monkeypatch)
        manager.grabbed_devices["1234:5678"] = [device]

        device.state.combo_passthrough_held.add("key_leftalt")
        await device.reset_mapping_runtime_state()

        assert device.state.combo_passthrough_held == set()
        assert device.state.held_source_actions["key_leftalt"] is None

        await manager.set_combos(
            [
                {
                    "id": "combo-browser-paste",
                    "name": "Browser Paste",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_leftalt",
                                },
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_v",
                                },
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                }
            ]
        )

        press_v = await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/event-test",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_V,
            1,
            source="kbd",
        )

        assert press_v is not None
        assert press_v.consume_current_event is True
        assert press_v.action_transition is not None
        assert press_v.action_transition.combo_id == "combo-browser-paste"
    @pytest.mark.asyncio
    async def test_runtime_combo_match_consumes_events_and_broadcasts(self, monkeypatch):
        manager = DeviceManager()
        callback = AsyncMock()

        async def cb(command, data):
            await callback(command, data)

        manager.broadcast_callback = cb
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

        pressed = await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-mouse",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.BTN_SIDE,
            1,
        )
        released = await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-mouse",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.BTN_SIDE,
            0,
        )
        await asyncio.sleep(0)

        assert pressed is not None and pressed.consume_current_event is True
        assert released is not None and released.consume_current_event is True
        callback.assert_awaited_once()
        sent_command, sent_data = callback.await_args.args
        assert sent_command == CommandType.ACTION_TRIGGER
        assert sent_data["action_type"] == "profile_toggle"
        assert sent_data["profile_name"] == "Gaming"
    @pytest.mark.asyncio
    async def test_combo_recall_repeat_suppression_resumes_after_restore_via_event_loop(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        hardware_id = "1234:5678"
        passthrough = _FakeUInput()
        await manager.set_combos(
            [
                {
                    "id": "combo-recall-repeat",
                    "name": "combo-recall-repeat",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_x",
                                },
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_c",
                                },
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                    "recall_trigger_keys": True,
                    "restore_trigger_keys": ["key_x"],
                }
            ]
        )
        manager.output_state.keyboard_uinput = _FakeUInput()

        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id=hardware_id,
            button_map={"key_x": "key_x", "key_c": "key_c"},
            mapping_getter=lambda: {},
            event_callback=lambda *args, **kwargs: _runtime_on_device_event(
                manager, *args, **kwargs
            ),
            device_type=DeviceType.KEYBOARD,
        )
        device._running = True
        device.uinput = passthrough  # type: ignore[assignment]
        manager.grabbed_devices = {hardware_id: [device]}

        press_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=1)
        press_c = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_C, value=1)
        repeat_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=2)
        release_c = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_C, value=0)

        await _runtime_process_grabbed_event(device, press_x)
        await _runtime_process_grabbed_event(device, press_c)
        await asyncio.sleep(0)

        assert device.state.combo_recalled_bindings == {"key_x"}
        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 0),
        ]
        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
        ]

        await _runtime_process_grabbed_event(device, repeat_x)
        await asyncio.sleep(0)

        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 0),
        ]

        await _runtime_process_grabbed_event(device, release_c)
        await asyncio.sleep(0)

        assert device.state.combo_recalled_bindings == set()
        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 1),
        ]
        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0),
        ]

        await _runtime_process_grabbed_event(device, repeat_x)
        await asyncio.sleep(0)

        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 2),
        ]
    @pytest.mark.asyncio
    async def test_combo_restore_respects_suppress_mapping_for_trigger_key(
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
                    "id": "combo-restore-suppress",
                    "name": "combo-restore-suppress",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_capslock",
                                },
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_x",
                                },
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                    "recall_trigger_keys": True,
                    "restore_trigger_keys": ["key_capslock"],
                }
            ]
        )
        manager.output_state.keyboard_uinput = keyboard

        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        mapping = {
            "key_capslock": dm.MappingAction(action_type=ActionType.SUPPRESS),
        }
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id=hardware_id,
            button_map={"key_capslock": "key_capslock", "key_x": "key_x"},
            mapping_getter=lambda: mapping,
            event_callback=lambda *args, **kwargs: _runtime_on_device_event(
                manager, *args, **kwargs
            ),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=keyboard,  # type: ignore[arg-type]
        )
        device._running = True
        device.uinput = passthrough  # type: ignore[assignment]
        manager.grabbed_devices = {hardware_id: [device]}

        press_caps = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_CAPSLOCK,
            value=1,
        )
        press_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=1)
        release_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=0)

        await _runtime_process_grabbed_event(device, press_caps)
        await _runtime_process_grabbed_event(device, press_x)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(device, release_x)
        await asyncio.sleep(0)

        assert passthrough.writes == []
        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0),
        ]
    @pytest.mark.asyncio
    async def test_combo_restore_replays_simple_keyboard_remap_for_trigger_key(
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
                    "id": "combo-restore-remap",
                    "name": "combo-restore-remap",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_capslock",
                                },
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_x",
                                },
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                    "recall_trigger_keys": True,
                    "restore_trigger_keys": ["key_capslock"],
                }
            ]
        )
        manager.output_state.keyboard_uinput = keyboard

        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        mapping = {
            "key_capslock": dm.MappingAction(
                action_type=ActionType.KEYBOARD,
                target="key_leftmeta",
            ),
        }
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id=hardware_id,
            button_map={"key_capslock": "key_capslock", "key_x": "key_x"},
            mapping_getter=lambda: mapping,
            event_callback=lambda *args, **kwargs: _runtime_on_device_event(
                manager, *args, **kwargs
            ),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=keyboard,  # type: ignore[arg-type]
        )
        device._running = True
        device.uinput = passthrough  # type: ignore[assignment]
        manager.grabbed_devices = {hardware_id: [device]}

        press_caps = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_CAPSLOCK,
            value=1,
        )
        press_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=1)
        release_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=0)

        await _runtime_process_grabbed_event(device, press_caps)
        await _runtime_process_grabbed_event(device, press_x)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(device, release_x)
        await asyncio.sleep(0)

        assert passthrough.writes == []
        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTMETA, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTMETA, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTMETA, 1),
        ]
    @pytest.mark.asyncio
    async def test_combo_restore_recalls_remapped_modifier_trigger_key(
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
                    "id": "combo-restore-remapped-modifier",
                    "name": "combo-restore-remapped-modifier",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_leftctrl",
                                },
                                {
                                    "hardware_id": hardware_id,
                                    "source": "kbd",
                                    "evdev": "key_x",
                                },
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                    "recall_trigger_keys": True,
                    "restore_trigger_keys": ["ctrl"],
                }
            ]
        )
        manager.output_state.keyboard_uinput = keyboard

        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        mapping = {
            "key_leftctrl": dm.MappingAction(
                action_type=ActionType.KEYBOARD,
                target="key_leftmeta",
            ),
        }
        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id=hardware_id,
            button_map={"key_leftctrl": "key_leftctrl", "key_x": "key_x"},
            mapping_getter=lambda: mapping,
            event_callback=lambda *args, **kwargs: _runtime_on_device_event(
                manager, *args, **kwargs
            ),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=keyboard,  # type: ignore[arg-type]
        )
        device._running = True
        device.uinput = passthrough  # type: ignore[assignment]
        manager.grabbed_devices = {hardware_id: [device]}

        press_ctrl = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_LEFTCTRL,
            value=1,
        )
        press_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=1)
        release_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=0)

        await _runtime_process_grabbed_event(device, press_ctrl)
        await _runtime_process_grabbed_event(device, press_x)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(device, release_x)
        await asyncio.sleep(0)

        assert passthrough.writes == []
        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTMETA, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTMETA, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTMETA, 1),
        ]
    @pytest.mark.asyncio
    async def test_combo_single_step_survives_unrelated_same_keyboard_actions(
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
                "key_h": "key_h",
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
        release_c = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_C, value=0)
        press_h = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_H, value=1)
        release_h = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_H, value=0)
        press_v = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_V, value=1)
        release_v = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_V, value=0)

        await _runtime_process_grabbed_event(device, press_alt)
        await _runtime_process_grabbed_event(device, press_c)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(device, release_c)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(device, press_h)
        await _runtime_process_grabbed_event(device, release_h)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(device, press_v)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(device, release_v)
        await asyncio.sleep(0)

        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 0),
        ]
    @pytest.mark.asyncio
    async def test_combo_single_step_survives_unrelated_mouse_click_between_combos(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        keyboard_hw = "1234:5678"
        mouse_hw = "1234:5678"
        keyboard = _FakeUInput()
        keyboard_passthrough = _FakeUInput()
        mouse_passthrough = _FakeUInput()

        await manager.set_combos(
            [
                {
                    "id": "combo-c",
                    "name": "combo-c",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": keyboard_hw,
                                    "source": "kbd",
                                    "evdev": "key_leftalt",
                                },
                                {"hardware_id": keyboard_hw, "source": "kbd", "evdev": "key_c"},
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
                                    "hardware_id": keyboard_hw,
                                    "source": "kbd",
                                    "evdev": "key_leftalt",
                                },
                                {"hardware_id": keyboard_hw, "source": "kbd", "evdev": "key_v"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f14"},
                },
            ]
        )
        manager.output_state.keyboard_uinput = keyboard

        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(
            gdm,
            "get_interface_id",
            lambda path: "mouse" if "mouse" in path else "kbd",
        )

        keyboard_device = GrabbedDevice(
            path="/dev/input/event-kbd",
            hardware_id=keyboard_hw,
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
        keyboard_device._running = True
        keyboard_device.uinput = keyboard_passthrough  # type: ignore[assignment]

        mouse_device = GrabbedDevice(
            path="/dev/input/event-mouse",
            hardware_id=mouse_hw,
            button_map={"btn_left": "btn_left"},
            mapping_getter=lambda: {},
            event_callback=lambda *args, **kwargs: _runtime_on_device_event(
                manager, *args, **kwargs
            ),
            device_type=DeviceType.MOUSE,
        )
        mouse_device._running = True
        mouse_device.uinput = mouse_passthrough  # type: ignore[assignment]
        manager.grabbed_devices = {keyboard_hw: [keyboard_device, mouse_device]}

        press_alt = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_LEFTALT,
            value=1,
        )
        press_c = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_C, value=1)
        release_c = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_C, value=0)
        press_mouse = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.BTN_LEFT, value=1)
        release_mouse = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.BTN_LEFT,
            value=0,
        )
        press_v = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_V, value=1)
        release_v = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_V, value=0)

        await _runtime_process_grabbed_event(keyboard_device, press_alt)
        await _runtime_process_grabbed_event(keyboard_device, press_c)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(keyboard_device, release_c)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(mouse_device, press_mouse)
        await _runtime_process_grabbed_event(mouse_device, release_mouse)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(keyboard_device, press_v)
        await asyncio.sleep(0)
        await _runtime_process_grabbed_event(keyboard_device, release_v)
        await asyncio.sleep(0)

        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 0),
        ]
