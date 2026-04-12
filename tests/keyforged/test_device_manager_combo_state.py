# ruff: noqa: F403, F405, I001
from tests.keyforged.device_manager_support import *

class TestCombos:
    @pytest.mark.asyncio
    async def test_runtime_combo_tap_trigger_releases_when_runtime_clears(self, monkeypatch):
        manager = DeviceManager()
        manager.output_state.gamepad_uinput = _FakeUInput()

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Quick Trigger",
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
                    "action": {
                        "action": "gamepad",
                        "target": "btn_lt",
                        "tap_enabled": True,
                        "tap_hold_ms": 1000,
                    },
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
        await asyncio.sleep(0)
        await _runtime_clear_combo_runtime(manager)

        assert pressed is not None and pressed.consume_current_event is True
        assert manager.output_state.gamepad_uinput.writes == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 255),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0),
        ]
    @pytest.mark.asyncio
    async def test_runtime_combo_recall_uses_matching_grabbed_device(self, monkeypatch):
        manager = DeviceManager()
        recalled = Mock()
        fake_device = type(
            "FakeDevice",
            (),
            {"interface_id": "kbd", "emit_combo_release": recalled},
        )()
        manager.grabbed_devices = {"1234:5678": [fake_device]}

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
                                    "evdev": "key_a",
                                },
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_s",
                                },
                            ]
                        }
                    ],
                    "action": {"action": "suppress"},
                }
            ]
        )

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

        await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_A,
            1,
        )
        await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_S,
            1,
        )

        recalled.assert_called_once_with("key_a")
    @pytest.mark.asyncio
    async def test_runtime_combo_does_not_recall_modifier_keys(self, monkeypatch):
        manager = DeviceManager()
        recalled = Mock()
        fake_device = type(
            "FakeDevice",
            (),
            {"interface_id": "kbd", "emit_combo_release": recalled},
        )()
        manager.grabbed_devices = {"1234:5678": [fake_device]}

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Alt Combo",
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
                                    "evdev": "key_1",
                                },
                            ]
                        }
                    ],
                    "action": {"action": "suppress"},
                }
            ]
        )

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

        await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_LEFTALT,
            1,
        )
        await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_1,
            1,
        )

        recalled.assert_not_called()
    @pytest.mark.asyncio
    async def test_runtime_combo_rearms_from_held_modifier_after_wrong_key_release(
        self,
        monkeypatch,
    ):
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = _FakeUInput()

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Meta One",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_leftmeta",
                                },
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_1",
                                },
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_f13"},
                }
            ]
        )

        monkeypatch.setattr(gdm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(gdm, "get_interface_id", lambda _path: "kbd")

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={
                "key_leftmeta": "key_leftmeta",
                "key_4": "key_4",
                "key_1": "key_1",
            },
            mapping_getter=lambda: {},
            event_callback=lambda *args, **kwargs: _runtime_on_device_event(
                manager, *args, **kwargs
            ),
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=manager.output_state.keyboard_uinput,  # type: ignore[arg-type]
            mouse_uinput=_FakeUInput(),  # type: ignore[arg-type]
            gamepad_uinput=_FakeUInput(),  # type: ignore[arg-type]
        )
        passthrough = _FakeUInput()
        device.uinput = passthrough  # type: ignore[assignment]
        device._running = True
        manager.grabbed_devices = {"1234:5678": [device]}

        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_LEFTMETA, value=1)
        )
        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_4, value=1)
        )
        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_4, value=0)
        )
        await _runtime_process_grabbed_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_1, value=1)
        )

        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTMETA, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_4, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_4, 0),
        ]
        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
        ]
    @pytest.mark.asyncio
    async def test_runtime_combo_hold_macro_stops_on_release(self, monkeypatch):
        manager = DeviceManager()
        manager.play_macro = AsyncMock(return_value={"status": "ok"})  # type: ignore[method-assign]

        await manager.set_combos(
            [
                {
                    "id": "combo-1",
                    "name": "Hold Macro",
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
                        "action": "macro",
                        "macro_name": "hold",
                        "macro_loop_mode": "hold",
                    },
                }
            ]
        )

        monkeypatch.setattr(dm, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(dm, "get_interface_id", lambda _path: "kbd")

        await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            1,
        )
        await _runtime_on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            0,
        )

        assert manager.play_macro.await_count == 2
        press_call = manager.play_macro.await_args_list[0].kwargs
        release_call = manager.play_macro.await_args_list[1].kwargs
        assert press_call["trigger_value"] == 1
        assert press_call["source_device"] == "combo"
        assert press_call["source_button"] == "combo:combo-1"
        assert release_call["trigger_value"] == 0
        assert release_call["source_device"] == "combo"
        assert release_call["source_button"] == "combo:combo-1"
