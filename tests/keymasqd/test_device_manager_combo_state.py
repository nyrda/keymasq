import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import evdev
import pytest

from keymasq.common import devices
from keymasq.common.ipc import CommandType
from keymasq.common.model.core import ActionType, DeviceType
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.runtime.combo import actions, events, lifecycle
from keymasq.keymasqd.runtime.grabbed_device.event import pipeline
from keymasq.keymasqd.runtime.macro import cleanup
from keymasq.keymasqd.runtime.manager_combos import (
    EMERGENCY_CANCEL_COMBO_ID_PREFIX,
    EMERGENCY_CANCEL_DOUBLE_TAP_WINDOW_MS,
)
from tests.keymasqd.device_manager_support import (
    FakeUInput,
    combo_event_runtime_kwargs,
    combo_runtime_deps,
    grabbed_event_processing_deps,
    make_combo_grabbed_device,
)


class TestCombos:
    @pytest.mark.asyncio
    async def test_set_combos_injects_emergency_cancel_for_grabbed_keyboard(self):
        manager = DeviceManager()
        manager.grabbed_devices = {
            "1234:5678": [
                SimpleNamespace(
                    device_type=DeviceType.KEYBOARD,
                    device_types=["keyboard"],
                )
            ]
        }

        result = await manager.set_combos([])

        assert result == {"updated": True, "combo_count": 1}
        combo = manager.combo_state.active_combos[0]
        assert combo.id == f"{EMERGENCY_CANCEL_COMBO_ID_PREFIX}1234:5678"
        assert combo.action is not None
        assert combo.action.action_type == ActionType.SUPERKEY
        assert combo.action.superkey_config is not None
        assert (
            combo.action.superkey_config.double_tap_window_ms
            == EMERGENCY_CANCEL_DOUBLE_TAP_WINDOW_MS
        )
        assert [action.action_type for action in combo.action.superkey_config.tap_actions] == [
            ActionType.CANCEL_MACRO_PLAYBACK.value
        ]
        assert [
            action.action_type for action in combo.action.superkey_config.double_tap_actions
        ] == [
            ActionType.EMERGENCY_RESET.value,
        ]
        assert combo.recall_trigger_keys is True
        assert combo.release_outputs_on_cancel is True
        assert [
            (binding.hardware_id, binding.evdev, binding.source)
            for binding in combo.steps[0].bindings
        ] == [
            ("1234:5678", "ctrl", ""),
            ("1234:5678", "alt", ""),
            ("1234:5678", "key_esc", ""),
        ]

    @pytest.mark.asyncio
    async def test_set_combos_skips_emergency_cancel_when_disabled(self):
        manager = DeviceManager()
        manager.emergency_cancel_combo_enabled = False
        manager.grabbed_devices = {
            "1234:5678": [
                SimpleNamespace(
                    device_type=DeviceType.KEYBOARD,
                    device_types=["keyboard"],
                )
            ]
        }

        result = await manager.set_combos([])

        assert result == {"updated": True, "combo_count": 0}
        assert manager.combo_state.active_combos == []

    @pytest.mark.asyncio
    async def test_set_combos_filters_user_emergency_cancel_duplicate(self):
        manager = DeviceManager()
        manager.grabbed_devices = {
            "1234:5678": [
                SimpleNamespace(
                    device_type=DeviceType.KEYBOARD,
                    device_types=["keyboard"],
                )
            ]
        }

        result = await manager.set_combos(
            [
                {
                    "id": "user-combo",
                    "name": "Reserved",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_leftctrl",
                                },
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "kbd",
                                    "evdev": "key_leftalt",
                                },
                                {"hardware_id": "1234:5678", "source": "kbd", "evdev": "key_esc"},
                            ]
                        }
                    ],
                    "action": {"action": "keyboard", "target": "key_a"},
                }
            ]
        )

        assert result == {"updated": True, "combo_count": 1}
        assert [combo.id for combo in manager.combo_state.active_combos] == [
            f"{EMERGENCY_CANCEL_COMBO_ID_PREFIX}1234:5678"
        ]

    @pytest.mark.asyncio
    async def test_emergency_cancel_combo_cancels_macros_and_releases_outputs(
        self, monkeypatch
    ):
        manager = DeviceManager()
        manager.cancel_macro_playback = AsyncMock(return_value={"cancelled": True})  # type: ignore[method-assign]
        release_tracked_outputs = Mock()
        manager.grabbed_devices = {
            "1234:5678": [
                SimpleNamespace(
                    device_type=DeviceType.KEYBOARD,
                    device_types=["keyboard"],
                    interface_id="kbd",
                    combo_passthrough_binding_active=lambda _evdev: True,
                    emit_combo_release=Mock(),
                    release_tracked_outputs=release_tracked_outputs,
                )
            ]
        }
        await manager.set_combos([])

        monkeypatch.setattr(devices, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(devices, "get_interface_id", lambda _path: "kbd")

        await events.on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_LEFTCTRL,
            1,
            None,
            None,
            **combo_event_runtime_kwargs(),
        )
        await events.on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_LEFTALT,
            1,
            None,
            None,
            **combo_event_runtime_kwargs(),
        )
        decision = await events.on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_ESC,
            1,
            None,
            None,
            **combo_event_runtime_kwargs(),
        )
        await events.on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_ESC,
            0,
            None,
            None,
            **combo_event_runtime_kwargs(),
        )
        await asyncio.sleep(0.25)

        assert decision is not None and decision.consume_current_event is True
        manager.cancel_macro_playback.assert_awaited_once()
        release_tracked_outputs.assert_called_once()

    @pytest.mark.asyncio
    async def test_user_cancel_combo_preserves_grabbed_device_outputs(self):
        manager = DeviceManager()
        manager.emergency_cancel_combo_enabled = False
        manager.cancel_macro_playback = AsyncMock(return_value={"cancelled": True})  # type: ignore[method-assign]
        release_tracked_outputs = Mock()
        manager.grabbed_devices = {
            "1234:5678": [SimpleNamespace(release_tracked_outputs=release_tracked_outputs)]
        }
        await manager.set_combos(
            [
                {
                    "id": "user-cancel",
                    "name": "User cancel",
                    "steps": [
                        {
                            "events": [
                                {"hardware_id": "1234:5678", "evdev": "key_f13"},
                            ]
                        }
                    ],
                    "action": {"action": ActionType.CANCEL_MACRO_PLAYBACK.value},
                }
            ]
        )
        combo = manager.combo_state.active_combos[0]
        assert combo.action is not None
        binding = combo.steps[0].bindings[0]

        await actions.start_combo_action(
            manager,
            combo.id,
            combo.action,
            binding,
            combo.steps[0].bindings,
            deps=combo_runtime_deps(),
        )
        await asyncio.sleep(0)

        manager.cancel_macro_playback.assert_awaited_once()
        release_tracked_outputs.assert_not_called()

    @pytest.mark.asyncio
    async def test_emergency_cancel_combo_double_tap_resets_runtime(self):
        manager = DeviceManager()
        manager.cancel_macro_playback = AsyncMock(return_value={"cancelled": True})  # type: ignore[method-assign]
        manager.emergency_reset = AsyncMock(return_value={"reset": True})  # type: ignore[method-assign]
        manager.grabbed_devices = {
            "1234:5678": [
                SimpleNamespace(
                    device_type=DeviceType.KEYBOARD,
                    device_types=["keyboard"],
                    interface_id="kbd",
                    combo_passthrough_binding_active=lambda _evdev: True,
                    emit_combo_release=Mock(),
                )
            ]
        }
        await manager.set_combos([])
        combo = manager.combo_state.active_combos[0]
        assert combo.action is not None
        binding = combo.steps[0].bindings[-1]

        await actions.start_combo_action(
            manager,
            combo.id,
            combo.action,
            binding,
            combo.steps[0].bindings,
            deps=combo_runtime_deps(),
        )
        await actions.stop_combo_action(manager, combo.id, deps=combo_runtime_deps())
        await actions.start_combo_action(
            manager,
            combo.id,
            combo.action,
            binding,
            combo.steps[0].bindings,
            deps=combo_runtime_deps(),
        )
        await actions.stop_combo_action(manager, combo.id, deps=combo_runtime_deps())
        await asyncio.sleep(0.02)

        manager.cancel_macro_playback.assert_not_awaited()
        manager.emergency_reset.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cancelled", [True, False])
    async def test_cancel_macro_playback_broadcasts_when_cancelled(self, monkeypatch, cancelled):
        events: list[tuple[CommandType, dict[str, object]]] = []

        async def broadcast(event_type: CommandType, data: dict[str, object]) -> None:
            events.append((event_type, data))

        manager = DeviceManager(broadcast_callback=broadcast)
        cancel_macro_playback = AsyncMock(return_value={"status": "ok", "cancelled": cancelled})
        monkeypatch.setattr(cleanup, "cancel_macro_playback", cancel_macro_playback)

        result = await manager.cancel_macro_playback()
        await asyncio.sleep(0)

        assert result == {"status": "ok", "cancelled": cancelled}
        assert events == [
            (
                CommandType.MACRO_PLAYBACK_CANCELLED,
                {"reason": "cancel_macro_playback", "cancelled": cancelled},
            )
        ]

    @pytest.mark.asyncio
    async def test_emergency_reset_releases_devices_and_broadcasts_runtime_reset(self):
        events: list[tuple[CommandType, dict[str, object]]] = []

        async def broadcast(event_type: CommandType, data: dict[str, object]) -> None:
            events.append((event_type, data))

        manager = DeviceManager(broadcast_callback=broadcast)
        manager.release_all_devices = AsyncMock()  # type: ignore[method-assign]

        result = await manager.emergency_reset()
        await asyncio.sleep(0)

        assert result == {"status": "ok", "reset": True}
        manager.release_all_devices.assert_awaited_once()
        assert events == [
            (CommandType.RUNTIME_RESET, {"reason": "emergency_reset"}),
        ]

    @pytest.mark.asyncio
    async def test_runtime_combo_tap_axis_releases_when_runtime_clears(self, monkeypatch):
        manager = DeviceManager()
        manager.output_state.gamepad_uinput = FakeUInput()

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
                        "action": "gamepad_axis",
                        "target": "abs_z",
                        "value": 255,
                        "tap_enabled": True,
                        "tap_hold_ms": 1000,
                    },
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
        await asyncio.sleep(0)
        await lifecycle.clear_combo_runtime(manager, deps=combo_runtime_deps())

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

        monkeypatch.setattr(devices, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(devices, "get_interface_id", lambda _path: "kbd")

        await events.on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_A,
            1,
            None,
            None,
            **combo_event_runtime_kwargs(),
        )
        await events.on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_S,
            1,
            None,
            None,
            **combo_event_runtime_kwargs(),
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

        monkeypatch.setattr(devices, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(devices, "get_interface_id", lambda _path: "kbd")

        await events.on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_LEFTALT,
            1,
            None,
            None,
            **combo_event_runtime_kwargs(),
        )
        await events.on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_1,
            1,
            None,
            None,
            **combo_event_runtime_kwargs(),
        )

        recalled.assert_not_called()

    @pytest.mark.asyncio
    async def test_runtime_combo_rearms_from_held_modifier_after_wrong_key_release(
        self,
        monkeypatch,
    ):
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = FakeUInput()

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

        passthrough = FakeUInput()
        device = make_combo_grabbed_device(
            monkeypatch,
            manager,
            button_map={
                "key_leftmeta": "key_leftmeta",
                "key_4": "key_4",
                "key_1": "key_1",
            },
            keyboard_uinput=manager.output_state.keyboard_uinput,
            passthrough_uinput=passthrough,
        )

        await pipeline.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_LEFTMETA, value=1),
            deps=grabbed_event_processing_deps(),
        )
        await pipeline.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_4, value=1),
            deps=grabbed_event_processing_deps(),
        )
        await pipeline.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_4, value=0),
            deps=grabbed_event_processing_deps(),
        )
        await pipeline.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_1, value=1),
            deps=grabbed_event_processing_deps(),
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

        monkeypatch.setattr(devices, "resolve_stable_path", lambda path: path)
        monkeypatch.setattr(devices, "get_interface_id", lambda _path: "kbd")

        await events.on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            1,
            None,
            None,
            **combo_event_runtime_kwargs(),
        )
        await events.on_device_event(
            manager,
            "1234:5678",
            "/dev/input/by-id/test-kbd",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.KEY_F13,
            0,
            None,
            None,
            **combo_event_runtime_kwargs(),
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
