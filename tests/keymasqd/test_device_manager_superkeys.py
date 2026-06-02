import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import evdev
import pytest

from keymasq.common.ipc import CommandType
from keymasq.common.models import (
    ActionType,
    DeviceType,
    ProfileDeactivationPolicy,
    SuperkeyMode,
)
from keymasq.keymasqd import device_manager as dm
from keymasq.keymasqd.combo_engine import ComboDecision
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.runtime import grabbed_device_events as gde
from keymasq.keymasqd.superkey_state import SuperkeyActionData, SuperkeyConfig
from tests.keymasqd.device_manager_support import (
    FakeUInput,
    grabbed_event_processing_deps,
    make_grabbed_device,
)


class TestSuperkeys:
    @pytest.mark.asyncio
    async def test_mapping_reset_clears_combo_passthrough_hold_but_preserves_passthrough_release(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        decisions = [
            ComboDecision(passthrough_current_event=True, reset_candidates=True),
            None,
        ]
        mapping_state: dict[str, dm.MappingAction] = {}

        async def event_callback(*_args):
            return decisions.pop(0)

        passthrough_uinput = FakeUInput()
        mapped_uinput = FakeUInput()
        device = make_grabbed_device(
            monkeypatch,
            button_map={"key_1": "key_1"},
            mapping=mapping_state,
            event_callback=event_callback,
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=mapped_uinput,
            passthrough_uinput=passthrough_uinput,
            running=True,
        )

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

        await gde.process_event(device, press_event, deps=grabbed_event_processing_deps())

        assert device.state.combo_passthrough_held == {"key_1"}
        assert "key_1" not in device.state.held_source_actions

        await device.reset_mapping_runtime_state()
        mapping_state["key_1"] = dm.MappingAction(
            action_type=ActionType.KEYBOARD,
            target="key_a",
        )

        assert device.state.combo_passthrough_held == set()
        assert device.state.held_source_actions["key_1"] is None

        await gde.process_event(device, release_event, deps=grabbed_event_processing_deps())

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
        device = make_grabbed_device(monkeypatch)
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
        passthrough = FakeUInput()
        device = make_grabbed_device(monkeypatch)
        device.uinput = passthrough  # type: ignore[assignment]
        device._running = True
        device.state.combo_passthrough_held.add("key_x")
        device.mark_combo_recalled_binding("key_x")

        repeat_event = SimpleNamespace(
            type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_X,
            value=2,
        )

        await gde.process_event(device, repeat_event, deps=grabbed_event_processing_deps())

        assert passthrough.writes == []
        assert device.state.combo_passthrough_held == {"key_x"}
        assert device.state.combo_recalled_bindings == {"key_x"}

        device.clear_combo_recalled_binding("key_x")
        await gde.process_event(device, repeat_event, deps=grabbed_event_processing_deps())

        assert passthrough.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_X, 2),
        ]

    @pytest.mark.asyncio
    async def test_combo_recalled_modifier_uses_normalized_name_for_suppression(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        passthrough = FakeUInput()
        device = make_grabbed_device(monkeypatch)
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

        await gde.process_event(device, repeat_event, deps=grabbed_event_processing_deps())
        await gde.process_event(device, release_event, deps=grabbed_event_processing_deps())

        assert passthrough.writes == []
        assert device.state.combo_recalled_bindings == set()
        assert device.state.combo_passthrough_held == set()

    @pytest.mark.asyncio
    async def test_combo_recalled_release_clears_suppression_without_passthrough(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        callback = AsyncMock(return_value=None)
        passthrough = FakeUInput()
        device = make_grabbed_device(monkeypatch)
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

        await gde.process_event(device, release_event, deps=grabbed_event_processing_deps())

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
        passthrough = FakeUInput()
        device = make_grabbed_device(monkeypatch)
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

        await gde.process_event(device, press_event, deps=grabbed_event_processing_deps())

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
        passthrough = FakeUInput()
        device = make_grabbed_device(monkeypatch)
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
            await gde.process_event(device, key_event, deps=grabbed_event_processing_deps())
            await gde.process_event(device, rel_event, deps=grabbed_event_processing_deps())

        assert "[hw 1234:5678 kbd] type=1 code=45 name=key_x value=2" in caplog.text
        assert "REL_X" not in caplog.text
        assert "type=2 code=0" not in caplog.text

    @pytest.mark.asyncio
    async def test_superkey_release_after_reset_does_not_recreate_stale_machine(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mapping_state = {
            "btn_side": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=SuperkeyConfig(
                    name="test",
                    tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
                ),
            )
        }

        device = make_grabbed_device(
            monkeypatch,
            interface_id="mouse",
            button_map={"btn_side": "btn_side"},
            mapping=mapping_state,
            device_type=DeviceType.MOUSE,
            running=True,
        )

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

        await gde.process_event(device, press_event, deps=grabbed_event_processing_deps())
        assert "btn_side" in device.state.superkey_machines

        await device.reset_superkeys()
        assert device.state.superkey_machines == {}

        await gde.process_event(device, release_event, deps=grabbed_event_processing_deps())

        assert device.state.superkey_machines == {}

    @pytest.mark.asyncio
    async def test_shared_superkey_config_on_two_inputs_holds_output_until_both_release(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
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

        keyboard_uinput = FakeUInput()
        device = make_grabbed_device(
            monkeypatch,
            interface_id="mouse",
            button_map={"btn_side": "btn_side", "btn_extra": "btn_extra"},
            mapping=mapping_state,
            device_type=DeviceType.MOUSE,
            keyboard_uinput=keyboard_uinput,
            running=True,
        )

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

        await gde.process_event(device, side_press, deps=grabbed_event_processing_deps())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await gde.process_event(device, extra_press, deps=grabbed_event_processing_deps())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert set(device.state.superkey_machines) == {"btn_side", "btn_extra"}
        assert device.state.superkey_machines["btn_side"].state.value == "holding"
        assert device.state.superkey_machines["btn_extra"].state.value == "holding"
        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
        ]

        await gde.process_event(device, side_release, deps=grabbed_event_processing_deps())
        await asyncio.sleep(0)

        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
        ]
        assert device.state.held_output_keys["keyboard"] == {evdev.ecodes.KEY_A}

        await gde.process_event(device, extra_release, deps=grabbed_event_processing_deps())
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

        keyboard_uinput = FakeUInput()
        device = make_grabbed_device(
            monkeypatch,
            button_map={"key_f13": "key_f13"},
            mapping=mapping_state,
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=keyboard_uinput,
            running=True,
        )

        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F13, value=1),
            deps=grabbed_event_processing_deps(),
        )
        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F13, value=2),
            deps=grabbed_event_processing_deps(),
        )
        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F13, value=0),
            deps=grabbed_event_processing_deps(),
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
    async def test_repeat_replays_overload_superkey_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from keymasq.keymasqd.runtime.repeat import SUPERKEY_SLOT_OVERLOAD

        mapping_state = {
            "key_f13": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=SuperkeyConfig(
                    name="bigA",
                    mode=SuperkeyMode.OVERLOAD,
                    overload_actions=[
                        dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_leftshift"),
                        dm.MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
                    ],
                ),
            ),
            "key_f14": dm.MappingAction(action_type=ActionType.REPEAT),
        }

        keyboard_uinput = FakeUInput()
        device = make_grabbed_device(
            monkeypatch,
            button_map={"key_f13": "key_f13", "key_f14": "key_f14"},
            mapping=mapping_state,
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=keyboard_uinput,
            running=True,
        )

        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F13, value=1),
            deps=grabbed_event_processing_deps(),
        )
        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F13, value=0),
            deps=grabbed_event_processing_deps(),
        )

        latest = device.repeat_state.history[-1]
        assert latest.action.action_type == ActionType.SUPERKEY
        assert latest.action.superkey_config is mapping_state["key_f13"].superkey_config
        assert latest.superkey_slot == SUPERKEY_SLOT_OVERLOAD

        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F14, value=1),
            deps=grabbed_event_processing_deps(),
        )
        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F14, value=0),
            deps=grabbed_event_processing_deps(),
        )

        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTSHIFT, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTSHIFT, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTSHIFT, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTSHIFT, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]
        assert device.repeat_state.history[-1].superkey_slot == SUPERKEY_SLOT_OVERLOAD

    @pytest.mark.asyncio
    async def test_repeat_replays_split_overload_superkey_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mapping_state = {
            "key_f13": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=SuperkeyConfig(
                    name="repeat-split-overload",
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
            ),
            "key_f14": dm.MappingAction(action_type=ActionType.REPEAT),
        }

        keyboard_uinput = FakeUInput()
        device = make_grabbed_device(
            monkeypatch,
            button_map={"key_f13": "key_f13", "key_f14": "key_f14"},
            mapping=mapping_state,
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=keyboard_uinput,
            running=True,
        )

        for code, value in (
            (evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.KEY_F13, 0),
            (evdev.ecodes.KEY_F14, 1),
            (evdev.ecodes.KEY_F14, 0),
        ):
            await gde.process_event(
                device,
                SimpleNamespace(type=evdev.ecodes.EV_KEY, code=code, value=value),
                deps=grabbed_event_processing_deps(),
            )

        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 0),
        ]

    @pytest.mark.asyncio
    async def test_repeat_replays_pattern_superkey_resolved_slot(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from keymasq.keymasqd.runtime.repeat import SUPERKEY_SLOT_DOUBLE_TAP

        mapping_state = {
            "key_f13": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=SuperkeyConfig(
                    name="wpctl_volume_rocker",
                    mode=SuperkeyMode.PATTERN,
                    double_tap_window_ms=250,
                    hold_threshold_ms=250,
                    double_tap_actions=[
                        SuperkeyActionData(action_type="keyboard", target="key_b"),
                    ],
                ),
            ),
            "key_f14": dm.MappingAction(action_type=ActionType.REPEAT),
        }

        keyboard_uinput = FakeUInput()
        device = make_grabbed_device(
            monkeypatch,
            button_map={"key_f13": "key_f13", "key_f14": "key_f14"},
            mapping=mapping_state,
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=keyboard_uinput,
            running=True,
        )

        for value in (1, 0, 1, 0):
            await gde.process_event(
                device,
                SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F13, value=value),
                deps=grabbed_event_processing_deps(),
            )

        latest = device.repeat_state.history[-1]
        assert latest.action.action_type == ActionType.SUPERKEY
        assert latest.action.superkey_config is mapping_state["key_f13"].superkey_config
        assert latest.superkey_slot == SUPERKEY_SLOT_DOUBLE_TAP

        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F14, value=1),
            deps=grabbed_event_processing_deps(),
        )
        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F14, value=0),
            deps=grabbed_event_processing_deps(),
        )

        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
        ]
        assert device.repeat_state.history[-1].superkey_slot == SUPERKEY_SLOT_DOUBLE_TAP

    @pytest.mark.asyncio
    async def test_repeat_skips_superkey_profile_paths(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        action_triggers: list[dict[str, object]] = []

        async def broadcast(event_type: CommandType, data: dict[str, object]) -> None:
            if event_type == CommandType.ACTION_TRIGGER:
                action_triggers.append(data)
                await manager.track_profile_activation(
                    str(data["profile_name"]),
                    f"activation-{len(action_triggers)}",
                    str(data["trigger_id"]),
                    data["deactivation"],
                )
                return

        manager = DeviceManager(broadcast_callback=broadcast)
        mapping_state = {
            "key_f13": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=SuperkeyConfig(
                    name="repeat-hold-profile",
                    mode=SuperkeyMode.PATTERN,
                    hold_threshold_ms=1,
                    hold_actions=[
                        SuperkeyActionData(
                            action_type="profile_enable",
                            profile_name="Nav",
                            profile_deactivation=ProfileDeactivationPolicy(on_trigger_end=True),
                        ),
                    ],
                ),
            ),
            "key_f14": dm.MappingAction(action_type=ActionType.REPEAT),
        }

        device = make_grabbed_device(
            monkeypatch,
            button_map={"key_f13": "key_f13", "key_f14": "key_f14"},
            mapping=mapping_state,
            device_type=DeviceType.KEYBOARD,
            broadcast_callback=broadcast,
            profile_activation_trigger_start_observer=manager.observe_profile_trigger_start,
            profile_activation_trigger_end_observer=manager.observe_profile_trigger_end,
            running=True,
        )

        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F13, value=1),
            deps=grabbed_event_processing_deps(),
        )
        await asyncio.sleep(0.01)
        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F13, value=0),
            deps=grabbed_event_processing_deps(),
        )
        await asyncio.sleep(0.01)

        assert len(action_triggers) == 1
        assert list(device.repeat_state.history) == []

        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F14, value=1),
            deps=grabbed_event_processing_deps(),
        )
        await asyncio.sleep(0.01)

        assert len(action_triggers) == 1

        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F14, value=0),
            deps=grabbed_event_processing_deps(),
        )
        await asyncio.sleep(0.01)

        assert len(action_triggers) == 1

    @pytest.mark.asyncio
    async def test_overload_superkey_profile_lifetime_follows_child_trigger(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events: list[tuple[CommandType, dict[str, object]]] = []
        action_triggers: list[dict[str, object]] = []

        async def broadcast(event_type: CommandType, data: dict[str, object]) -> None:
            if event_type == CommandType.ACTION_TRIGGER:
                action_triggers.append(data)
                await manager.track_profile_activation(
                    str(data["profile_name"]),
                    "activation-1",
                    str(data["trigger_id"]),
                    data["deactivation"],
                )
                return
            events.append((event_type, data))

        manager = DeviceManager(broadcast_callback=broadcast)
        mapping_state = {
            "key_f13": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=SuperkeyConfig(
                    name="overload",
                    mode=SuperkeyMode.OVERLOAD,
                    overload_actions=[
                        dm.MappingAction(
                            action_type=ActionType.PROFILE_ENABLE,
                            profile_name="Nav",
                            profile_deactivation=ProfileDeactivationPolicy(on_trigger_end=True),
                        ),
                    ],
                ),
            )
        }

        device = make_grabbed_device(
            monkeypatch,
            button_map={"key_f13": "key_f13"},
            mapping=mapping_state,
            device_type=DeviceType.KEYBOARD,
            broadcast_callback=broadcast,
            profile_activation_trigger_start_observer=manager.observe_profile_trigger_start,
            profile_activation_trigger_end_observer=manager.observe_profile_trigger_end,
            running=True,
        )

        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F13, value=1),
            deps=grabbed_event_processing_deps(),
        )
        await asyncio.sleep(0.05)

        assert action_triggers == [
            {
                "action_type": "profile_enable",
                "profile_name": "Nav",
                "source_device": "1234:5678",
                "source_button": "key_f13#overload#0",
                "trigger_id": "1234:5678:key_f13#overload#0",
                "deactivation": {
                    "on_trigger_end": True,
                },
            }
        ]
        assert [
            event for event in events if event[0] == CommandType.PROFILE_DEACTIVATE_REQUESTED
        ] == []

        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F13, value=0),
            deps=grabbed_event_processing_deps(),
        )
        await asyncio.sleep(0.05)

        assert (
            CommandType.PROFILE_DEACTIVATE_REQUESTED,
            {
                "profile_name": "Nav",
                "activation_id": "activation-1",
                "reason": "trigger_end",
            },
        ) in events

    @pytest.mark.asyncio
    async def test_split_overload_superkey_pulses_down_and_up_actions(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
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

        keyboard_uinput = FakeUInput()
        device = make_grabbed_device(
            monkeypatch,
            button_map={"key_f13": "key_f13"},
            mapping=mapping_state,
            device_type=DeviceType.KEYBOARD,
            keyboard_uinput=keyboard_uinput,
            running=True,
        )

        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F13, value=1),
            deps=grabbed_event_processing_deps(),
        )
        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F13, value=2),
            deps=grabbed_event_processing_deps(),
        )
        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.KEY_F13, value=0),
            deps=grabbed_event_processing_deps(),
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

        keyboard_uinput = FakeUInput()
        device = make_grabbed_device(
            monkeypatch,
            interface_id="mouse",
            button_map={"btn_side": "btn_side", "btn_extra": "btn_extra"},
            mapping=mapping_state,
            device_type=DeviceType.MOUSE,
            keyboard_uinput=keyboard_uinput,
            running=True,
        )

        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.BTN_SIDE, value=1),
            deps=grabbed_event_processing_deps(),
        )
        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.BTN_EXTRA, value=1),
            deps=grabbed_event_processing_deps(),
        )
        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.BTN_SIDE, value=0),
            deps=grabbed_event_processing_deps(),
        )

        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
        ]
        assert device.state.held_output_keys["keyboard"] == {evdev.ecodes.KEY_A}

        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.BTN_EXTRA, value=0),
            deps=grabbed_event_processing_deps(),
        )

        assert keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]

    @pytest.mark.asyncio
    async def test_overload_superkey_refcounts_shared_axis_outputs_across_two_inputs(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        shared_config = SuperkeyConfig(
            name="overload_shared_axis",
            mode=SuperkeyMode.OVERLOAD,
            overload_actions=[
                dm.MappingAction(
                    action_type=ActionType.GAMEPAD_AXIS,
                    target="abs_x",
                    axis_value=-32768,
                ),
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

        gamepad_uinput = FakeUInput()
        device = make_grabbed_device(
            monkeypatch,
            interface_id="mouse",
            button_map={"btn_side": "btn_side", "btn_extra": "btn_extra"},
            mapping=mapping_state,
            device_type=DeviceType.MOUSE,
            gamepad_uinput=gamepad_uinput,
            running=True,
        )

        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.BTN_SIDE, value=1),
            deps=grabbed_event_processing_deps(),
        )
        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.BTN_EXTRA, value=1),
            deps=grabbed_event_processing_deps(),
        )
        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.BTN_SIDE, value=0),
            deps=grabbed_event_processing_deps(),
        )

        assert gamepad_uinput.writes == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, -32768),
        ]
        assert device.state.held_output_abs["gamepad"] == {evdev.ecodes.ABS_X}

        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.BTN_EXTRA, value=0),
            deps=grabbed_event_processing_deps(),
        )

        assert gamepad_uinput.writes == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, -32768),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 0),
        ]
        assert device.state.held_output_abs["gamepad"] == set()

    @pytest.mark.asyncio
    async def test_pattern_superkey_gamepad_axis_tracks_held_axis_for_cleanup(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = SuperkeyConfig(
            name="axis_hold",
            mode=SuperkeyMode.PATTERN,
            hold_threshold_ms=1,
            hold_actions=[
                SuperkeyActionData(
                    action_type="gamepad_axis",
                    target="abs_z",
                    axis_value=255,
                ),
            ],
        )
        mapping_state = {
            "btn_side": dm.MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=config,
            ),
        }

        gamepad_uinput = FakeUInput()
        device = make_grabbed_device(
            monkeypatch,
            interface_id="mouse",
            button_map={"btn_side": "btn_side"},
            mapping=mapping_state,
            device_type=DeviceType.MOUSE,
            gamepad_uinput=gamepad_uinput,
            running=True,
        )

        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.BTN_SIDE, value=1),
            deps=grabbed_event_processing_deps(),
        )
        await asyncio.sleep(0.01)

        assert gamepad_uinput.writes == [(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 255)]
        assert device.state.held_output_abs["gamepad"] == {evdev.ecodes.ABS_Z}

        await gde.process_event(
            device,
            SimpleNamespace(type=evdev.ecodes.EV_KEY, code=evdev.ecodes.BTN_SIDE, value=0),
            deps=grabbed_event_processing_deps(),
        )

        assert gamepad_uinput.writes == [
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 255),
            (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0),
        ]
        assert device.state.held_output_abs["gamepad"] == set()

    @pytest.mark.asyncio
    async def test_reset_mapping_runtime_state_seeds_startup_held_action_and_releases_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mapping_state = {
            "key_f13": dm.MappingAction(
                action_type=ActionType.KEYBOARD,
                target="key_a",
            )
        }

        device = make_grabbed_device(
            monkeypatch,
            button_map={"key_f13": "key_f13"},
            mapping=mapping_state,
            device_type=DeviceType.KEYBOARD,
            running=True,
        )
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

        device = make_grabbed_device(
            monkeypatch,
            interface_id="mouse",
            button_map={"btn_side": "btn_side"},
            mapping=mapping_state,
            device_type=DeviceType.MOUSE,
            broadcast_callback=stalled_callback,
            running=True,
        )

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

        await asyncio.wait_for(
            gde.process_event(device, press_event, deps=grabbed_event_processing_deps()),
            timeout=0.05,
        )
        await asyncio.wait_for(
            gde.process_event(device, release_event, deps=grabbed_event_processing_deps()),
            timeout=0.05,
        )

        blocker.set()
        await asyncio.sleep(0)
