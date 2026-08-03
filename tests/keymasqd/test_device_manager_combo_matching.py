import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import evdev
import pytest

from keymasq.common import devices
from keymasq.common.ipc import CommandType
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType, DeviceType
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.runtime.combo import events
from keymasq.keymasqd.runtime.grabbed_device import device as grabbed_device
from keymasq.keymasqd.runtime.grabbed_device.device import GrabbedDevice
from keymasq.keymasqd.runtime.grabbed_device.event import pipeline
from keymasq.keymasqd.runtime.manager_combos import combo_runtime_signature
from tests.keymasqd.device_manager_support import (
    FakeUInput,
    combo_event_runtime_kwargs,
    grabbed_event_processing_deps,
    make_combo_grabbed_device,
    make_combo_runtime_setup,
    make_grabbed_device,
)


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

        monkeypatch.setattr(grabbed_device, "resolve_stable_path", lambda path: f"{path}-stable")
        monkeypatch.setattr(grabbed_device, "get_interface_id", lambda _path: "kbd")

        device = GrabbedDevice(
            path="/dev/input/event-test",
            hardware_id="1234:5678",
            button_map={"key_f13": "key_f13"},
            mapping_getter=lambda: {},
            event_callback=lambda *args, **kwargs: events.on_device_event(
                manager, *args, **kwargs, **combo_event_runtime_kwargs()
            ),
            device_type=DeviceType.KEYBOARD,
        )

        def fail_resolve(_path: str) -> str:
            raise AssertionError("resolve_stable_path should not run in the hot event path")

        def fail_interface(_path: str) -> str:
            raise AssertionError("get_interface_id should not run in the hot event path")

        monkeypatch.setattr(grabbed_device, "resolve_stable_path", fail_resolve)
        monkeypatch.setattr(grabbed_device, "get_interface_id", fail_interface)

        decision = await pipeline.process_event(
            device,
            SimpleNamespace(
                type=evdev.ecodes.EV_KEY,
                code=evdev.ecodes.KEY_F13,
                value=1,
            ),
            deps=grabbed_event_processing_deps(),
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
        assert len(manager.combo_state.active_combos) == 1
        assert manager.combo_state.active_combos[0].action is not None
        assert manager.combo_state.active_combos[0].action.profile_name == "Gaming"

    @pytest.mark.asyncio
    async def test_set_combos_parses_match_across_devices(self):
        manager = DeviceManager()

        await manager.set_combos(
            [
                {
                    "id": "combo-any-device",
                    "name": "Any Device",
                    "profile_name": "Desktop",
                    "match_across_devices": True,
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

        combo = manager.combo_state.active_combos[0]
        binding = combo.steps[0].bindings[0]
        assert combo.match_across_devices is True
        assert binding.hardware_id == ""
        assert binding.source == ""
        assert combo_runtime_signature(combo)[-2] is True
        assert combo_runtime_signature(combo)[-1] is False

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

        assert manager.combo_state.active_combos[0].steps[0].bindings[0].hardware_id == ""

        pressed = await events.on_device_event(
            manager,
            "9999:0001",
            "/dev/input/event-test",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            1,
            None,
            source="kbd",
            **combo_event_runtime_kwargs(),
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

        device = make_grabbed_device(monkeypatch)
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

        press_v = await events.on_device_event(
            manager,
            "1234:5678",
            "/dev/input/event-test",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_V,
            1,
            None,
            source="kbd",
            **combo_event_runtime_kwargs(),
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

        monkeypatch.setattr(devices, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(devices, "get_interface_id", lambda _path: "mouse")

        pressed = await events.on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-mouse",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.BTN_SIDE,
            1,
            None,
            None,
            **combo_event_runtime_kwargs(),
        )
        released = await events.on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-mouse",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.BTN_SIDE,
            0,
            None,
            None,
            **combo_event_runtime_kwargs(),
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
        hardware_id = "1234:5678"
        setup = await make_combo_runtime_setup(
            monkeypatch,
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
            ],
            hardware_id=hardware_id,
            button_map={"key_x": "key_x", "key_c": "key_c"},
        )
        device = setup.device
        passthrough = setup.passthrough
        keyboard = setup.keyboard

        press_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=1)
        press_c = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_C, value=1)
        repeat_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=2)
        release_c = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_C, value=0)

        await pipeline.process_event(device, press_x, deps=grabbed_event_processing_deps())
        await pipeline.process_event(device, press_c, deps=grabbed_event_processing_deps())
        await asyncio.sleep(0)

        assert device.state.combo_recalled_bindings == {"key_x"}
        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 0),
        ]
        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
        ]

        await pipeline.process_event(device, repeat_x, deps=grabbed_event_processing_deps())
        await asyncio.sleep(0)

        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 0),
        ]

        await pipeline.process_event(device, release_c, deps=grabbed_event_processing_deps())
        await asyncio.sleep(0)

        assert device.state.combo_recalled_bindings == set()
        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 1),
        ]
        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0),
        ]

        await pipeline.process_event(device, repeat_x, deps=grabbed_event_processing_deps())
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
        hardware_id = "1234:5678"
        mapping = {
            "key_capslock": MappingAction(action_type=ActionType.SUPPRESS),
        }

        setup = await make_combo_runtime_setup(
            monkeypatch,
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
            ],
            hardware_id=hardware_id,
            button_map={"key_capslock": "key_capslock", "key_x": "key_x"},
            mapping=mapping,
        )
        device = setup.device
        passthrough = setup.passthrough
        keyboard = setup.keyboard

        press_caps = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_CAPSLOCK,
            value=1,
        )
        press_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=1)
        release_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=0)

        await pipeline.process_event(device, press_caps, deps=grabbed_event_processing_deps())
        await pipeline.process_event(device, press_x, deps=grabbed_event_processing_deps())
        await asyncio.sleep(0)
        await pipeline.process_event(device, release_x, deps=grabbed_event_processing_deps())
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
        hardware_id = "1234:5678"
        mapping = {
            "key_capslock": MappingAction(
                action_type=ActionType.KEYBOARD,
                target="key_leftmeta",
            ),
        }
        setup = await make_combo_runtime_setup(
            monkeypatch,
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
            ],
            hardware_id=hardware_id,
            button_map={"key_capslock": "key_capslock", "key_x": "key_x"},
            mapping=mapping,
        )
        device = setup.device
        passthrough = setup.passthrough
        keyboard = setup.keyboard

        press_caps = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_CAPSLOCK,
            value=1,
        )
        press_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=1)
        release_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=0)

        await pipeline.process_event(device, press_caps, deps=grabbed_event_processing_deps())
        await pipeline.process_event(device, press_x, deps=grabbed_event_processing_deps())
        await asyncio.sleep(0)
        await pipeline.process_event(device, release_x, deps=grabbed_event_processing_deps())
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
        hardware_id = "1234:5678"
        mapping = {
            "key_leftctrl": MappingAction(
                action_type=ActionType.KEYBOARD,
                target="key_leftmeta",
            ),
        }
        setup = await make_combo_runtime_setup(
            monkeypatch,
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
            ],
            hardware_id=hardware_id,
            button_map={"key_leftctrl": "key_leftctrl", "key_x": "key_x"},
            mapping=mapping,
        )
        device = setup.device
        passthrough = setup.passthrough
        keyboard = setup.keyboard

        press_ctrl = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_LEFTCTRL,
            value=1,
        )
        press_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=1)
        release_x = SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_X, value=0)

        await pipeline.process_event(device, press_ctrl, deps=grabbed_event_processing_deps())
        await pipeline.process_event(device, press_x, deps=grabbed_event_processing_deps())
        await asyncio.sleep(0)
        await pipeline.process_event(device, release_x, deps=grabbed_event_processing_deps())
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
        hardware_id = "1234:5678"

        setup = await make_combo_runtime_setup(
            monkeypatch,
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
            ],
            hardware_id=hardware_id,
            button_map={
                "key_leftalt": "key_leftalt",
                "key_c": "key_c",
                "key_v": "key_v",
                "key_h": "key_h",
            },
        )
        device = setup.device
        keyboard = setup.keyboard

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

        await pipeline.process_event(device, press_alt, deps=grabbed_event_processing_deps())
        await pipeline.process_event(device, press_c, deps=grabbed_event_processing_deps())
        await asyncio.sleep(0)
        await pipeline.process_event(device, release_c, deps=grabbed_event_processing_deps())
        await asyncio.sleep(0)
        await pipeline.process_event(device, press_h, deps=grabbed_event_processing_deps())
        await pipeline.process_event(device, release_h, deps=grabbed_event_processing_deps())
        await asyncio.sleep(0)
        await pipeline.process_event(device, press_v, deps=grabbed_event_processing_deps())
        await asyncio.sleep(0)
        await pipeline.process_event(device, release_v, deps=grabbed_event_processing_deps())
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
        keyboard = FakeUInput()
        keyboard_passthrough = FakeUInput()
        mouse_passthrough = FakeUInput()

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

        keyboard_device = make_combo_grabbed_device(
            monkeypatch,
            manager,
            path="/dev/input/event-kbd",
            hardware_id=keyboard_hw,
            button_map={
                "key_leftalt": "key_leftalt",
                "key_c": "key_c",
                "key_v": "key_v",
            },
            passthrough_uinput=keyboard_passthrough,
        )

        mouse_device = make_combo_grabbed_device(
            monkeypatch,
            manager,
            path="/dev/input/event-mouse",
            hardware_id=mouse_hw,
            button_map={"btn_left": "btn_left"},
            source="mouse",
            device_type=DeviceType.MOUSE,
            passthrough_uinput=mouse_passthrough,
        )

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

        await pipeline.process_event(
            keyboard_device, press_alt, deps=grabbed_event_processing_deps()
        )
        await pipeline.process_event(keyboard_device, press_c, deps=grabbed_event_processing_deps())
        await asyncio.sleep(0)
        await pipeline.process_event(
            keyboard_device, release_c, deps=grabbed_event_processing_deps()
        )
        await asyncio.sleep(0)
        await pipeline.process_event(
            mouse_device, press_mouse, deps=grabbed_event_processing_deps()
        )
        await pipeline.process_event(
            mouse_device, release_mouse, deps=grabbed_event_processing_deps()
        )
        await asyncio.sleep(0)
        await pipeline.process_event(keyboard_device, press_v, deps=grabbed_event_processing_deps())
        await asyncio.sleep(0)
        await pipeline.process_event(
            keyboard_device, release_v, deps=grabbed_event_processing_deps()
        )
        await asyncio.sleep(0)

        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F14, 0),
        ]
