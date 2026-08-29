import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import evdev
import pytest

from keymasq.common.ipc import CommandType
from keymasq.common.model.actions import MappingAction, ProfileDeactivationPolicy
from keymasq.common.model.core import ActionType, SuperkeyMode
from keymasq.keymasqd.combo_engine import RuntimeCombo, RuntimeComboBinding, RuntimeComboStep
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.runtime.combo import actions, events, lifecycle
from keymasq.keymasqd.runtime.combo.execution import action_needs_release
from keymasq.keymasqd.superkey_state import SuperkeyActionData, SuperkeyConfig
from tests.keymasqd.device_manager_support import (
    FakeUInput,
    combo_runtime_deps,
)

_DEFAULT_FAKE_COMBO_BINDINGS = {"key_leftmeta", "key_c"}


class FakeComboDevice:
    def __init__(
        self,
        *,
        hardware_id: str = "1234:5678",
        interface_id: str = "kbd",
        active: set[str] | None = None,
        held: set[str] | None = None,
    ) -> None:
        active_bindings = _DEFAULT_FAKE_COMBO_BINDINGS if active is None else active
        held_bindings = active_bindings if held is None else held
        self.hardware_id = hardware_id
        self.interface_id = interface_id
        self.active = set(active_bindings)
        self.held = set(held_bindings)
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


class TestComboActionDispatch:
    def test_combo_payload_handles_list_style_evdev_aliases(self) -> None:
        evdev_mod = SimpleNamespace(
            ecodes=SimpleNamespace(
                EV_KEY=evdev.ecodes.EV_KEY,
                EV_REL=evdev.ecodes.EV_REL,
                bytype={
                    evdev.ecodes.EV_KEY: {
                        evdev.ecodes.BTN_SOUTH: ["BTN_A", "BTN_GAMEPAD", "BTN_SOUTH"]
                    }
                },
            )
        )

        payload = events.build_combo_event_payload(
            "1234:5678",
            "/dev/input/event7",
            evdev.ecodes.EV_KEY,
            evdev.ecodes.BTN_SOUTH,
            1,
            stable_path=None,
            source=None,
            evdev_mod=evdev_mod,
            resolve_stable_path_fn=lambda path: f"{path}-stable",
            get_interface_id_fn=lambda _path: "pad",
        )

        assert payload is not None
        assert payload["evdev"] == "btn_south"
        assert payload["source"] == "pad"

    def test_combo_action_needs_release_tracks_natural_mouse_move_tap(self) -> None:
        action = MappingAction(
            action_type=ActionType.MOUSE_MOVE_NATURAL_ABS,
            tap_enabled=True,
        )

        assert action_needs_release(action) is True

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
        binding = RuntimeComboBinding("1234:5678", "btn_side", "mouse")

        await actions.start_combo_action(
            manager,
            "profile",
            MappingAction(action_type=ActionType.PROFILE_ENABLE, profile_name="Nav"),
            binding,
            (binding,),
            deps=combo_runtime_deps(),
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

        await actions.stop_combo_action(manager, "profile", deps=combo_runtime_deps())
        await asyncio.wait_for(deactivate_event.wait(), timeout=1.0)

        assert expected_deactivate in events

    @pytest.mark.asyncio
    async def test_combo_overload_superkey_starts_children_and_releases_in_reverse_order(
        self,
    ) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = FakeUInput()
        binding = RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        action = MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-overload",
                mode=SuperkeyMode.OVERLOAD,
                overload_actions=[
                    MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
                    MappingAction(action_type=ActionType.KEYBOARD, target="key_b"),
                ],
            ),
        )

        await actions.start_combo_action(
            manager, "combo-overload", action, binding, (binding,), deps=combo_runtime_deps()
        )
        await actions.stop_combo_action(manager, "combo-overload", deps=combo_runtime_deps())

        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]

    @pytest.mark.asyncio
    async def test_combo_stop_releases_tracked_output_when_release_action_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        keyboard = FakeUInput()
        manager.output_state.keyboard_uinput = keyboard
        binding = RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_a",
        )
        action = MappingAction(action_type=ActionType.KEYBOARD, target="key_f13")

        await actions.start_combo_action(
            manager,
            "combo-key",
            action,
            binding,
            (binding,),
            deps=combo_runtime_deps(),
        )
        runtime = manager.combo_state.active_actions["combo-key"].action_runtime
        monkeypatch.setattr(
            actions.action_runner,
            "execute_action",
            AsyncMock(side_effect=RuntimeError("release failed")),
        )

        with pytest.raises(RuntimeError, match="release failed"):
            await actions.stop_combo_action(
                manager,
                "combo-key",
                deps=combo_runtime_deps(),
            )

        assert keyboard.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F13, 0),
        ]
        assert runtime is not None
        assert runtime.running is False

    @pytest.mark.asyncio
    async def test_combo_repeat_replays_last_combo_action_and_releases_child(self) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = FakeUInput()
        binding = RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_f13")

        await actions.start_combo_action(
            manager,
            "source",
            MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
            binding,
            (binding,),
            deps=combo_runtime_deps(),
        )
        await actions.stop_combo_action(manager, "source", deps=combo_runtime_deps())
        await actions.start_combo_action(
            manager,
            "repeat",
            MappingAction(action_type=ActionType.REPEAT),
            binding,
            (binding,),
            deps=combo_runtime_deps(),
        )

        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
        ]
        assert "repeat" in manager.combo_state.active_actions
        state = manager.combo_state.active_actions["repeat"]
        assert state.action_runtime is not None
        assert "combo:repeat#repeat" in state.action_runtime.state.repeat_active_actions

        await actions.stop_combo_action(manager, "repeat", deps=combo_runtime_deps())
        await actions.start_combo_action(
            manager,
            "repeat-again",
            MappingAction(action_type=ActionType.REPEAT),
            binding,
            (binding,),
            deps=combo_runtime_deps(),
        )
        await actions.stop_combo_action(manager, "repeat-again", deps=combo_runtime_deps())

        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]
        assert len(manager.repeat_state.history) == 3

    @pytest.mark.asyncio
    async def test_combo_repeat_skips_profile_actions(self) -> None:
        action_triggers: list[dict[str, object]] = []

        async def broadcast(event_type: CommandType, data: dict[str, object]) -> None:
            if event_type == CommandType.ACTION_TRIGGER:
                action_triggers.append(data)

        manager = DeviceManager(broadcast_callback=broadcast)
        binding = RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_f13")
        profile_action = MappingAction(
            action_type=ActionType.PROFILE_ENABLE,
            profile_name="Nav",
            profile_deactivation=ProfileDeactivationPolicy(on_trigger_end=True),
        )

        await actions.start_combo_action(
            manager,
            "source-profile",
            profile_action,
            binding,
            (binding,),
            deps=combo_runtime_deps(),
        )
        await asyncio.sleep(0)
        await actions.stop_combo_action(manager, "source-profile", deps=combo_runtime_deps())

        assert len(action_triggers) == 1
        assert list(manager.repeat_state.history) == []

        await actions.start_combo_action(
            manager,
            "repeat-profile",
            MappingAction(action_type=ActionType.REPEAT),
            binding,
            (binding,),
            deps=combo_runtime_deps(),
        )
        await asyncio.sleep(0)

        assert len(action_triggers) == 1
        assert "repeat-profile" not in manager.combo_state.active_actions

    @pytest.mark.asyncio
    async def test_combo_repeat_replays_overload_superkey_path(self) -> None:
        from keymasq.keymasqd.runtime.repeat import SUPERKEY_SLOT_OVERLOAD

        manager = DeviceManager()
        manager.output_state.keyboard_uinput = FakeUInput()
        binding = RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_f13")
        action = MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-overload-repeat",
                mode=SuperkeyMode.OVERLOAD,
                overload_actions=[
                    MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
                    MappingAction(action_type=ActionType.KEYBOARD, target="key_b"),
                ],
            ),
        )

        await actions.start_combo_action(
            manager, "source", action, binding, (binding,), deps=combo_runtime_deps()
        )
        await actions.stop_combo_action(manager, "source", deps=combo_runtime_deps())

        latest = manager.repeat_state.history[-1]
        assert latest.action.action_type == ActionType.SUPERKEY
        assert latest.action.superkey_config is action.superkey_config
        assert latest.superkey_slot == SUPERKEY_SLOT_OVERLOAD

        await actions.start_combo_action(
            manager,
            "repeat",
            MappingAction(action_type=ActionType.REPEAT),
            binding,
            (binding,),
            deps=combo_runtime_deps(),
        )
        assert "repeat" not in manager.combo_state.active_actions
        assert "repeat#repeat" not in manager.combo_state.active_actions

        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]
        assert manager.repeat_state.history[-1].superkey_slot == SUPERKEY_SLOT_OVERLOAD

    @pytest.mark.asyncio
    async def test_combo_repeat_replays_split_overload_superkey_path(self) -> None:
        from keymasq.keymasqd.runtime.repeat import SUPERKEY_SLOT_OVERLOAD

        manager = DeviceManager()
        manager.output_state.keyboard_uinput = FakeUInput()
        binding = RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_f13")
        action = MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-split-overload-repeat",
                mode=SuperkeyMode.OVERLOAD,
                overload_actions=[
                    MappingAction(action_type=ActionType.KEYBOARD, target="key_leftctrl"),
                ],
                overload_down_actions=[
                    MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
                ],
                overload_up_actions=[
                    MappingAction(action_type=ActionType.KEYBOARD, target="key_b"),
                ],
            ),
        )

        await actions.start_combo_action(
            manager, "source", action, binding, (binding,), deps=combo_runtime_deps()
        )
        await actions.stop_combo_action(manager, "source", deps=combo_runtime_deps())
        await actions.start_combo_action(
            manager,
            "repeat",
            MappingAction(action_type=ActionType.REPEAT),
            binding,
            (binding,),
            deps=combo_runtime_deps(),
        )

        assert manager.output_state.keyboard_uinput.writes == [
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
        assert "repeat" not in manager.combo_state.active_actions
        assert manager.repeat_state.history[-1].superkey_slot == SUPERKEY_SLOT_OVERLOAD

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
        binding = RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        action = MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-overload-profile",
                mode=SuperkeyMode.OVERLOAD,
                overload_actions=[
                    MappingAction(
                        action_type=ActionType.PROFILE_ENABLE,
                        profile_name="Nav",
                        profile_deactivation=ProfileDeactivationPolicy(on_trigger_end=True),
                    ),
                ],
            ),
        )

        await actions.start_combo_action(
            manager,
            "combo-overload-profile",
            action,
            binding,
            (binding,),
            deps=combo_runtime_deps(),
        )
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

        await actions.stop_combo_action(
            manager, "combo-overload-profile", deps=combo_runtime_deps()
        )
        await asyncio.wait_for(deactivate_event.wait(), timeout=1.0)

        assert expected_deactivate in events

    @pytest.mark.asyncio
    async def test_combo_split_overload_superkey_pulses_down_and_up_children(
        self,
    ) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = FakeUInput()
        binding = RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        action = MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-split-overload",
                mode=SuperkeyMode.OVERLOAD,
                overload_actions=[
                    MappingAction(action_type=ActionType.KEYBOARD, target="key_leftctrl"),
                ],
                overload_down_actions=[
                    MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
                ],
                overload_up_actions=[
                    MappingAction(action_type=ActionType.KEYBOARD, target="key_b"),
                ],
            ),
        )

        await actions.start_combo_action(
            manager, "combo-split-overload", action, binding, (binding,), deps=combo_runtime_deps()
        )
        await actions.stop_combo_action(manager, "combo-split-overload", deps=combo_runtime_deps())

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
                    MappingAction(action_type=ActionType.SUPERKEY, superkey_name="nested"),
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
        binding = RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")

        await actions.start_combo_action(
            manager,
            "combo-invalid-superkey",
            MappingAction(action_type=ActionType.SUPERKEY),
            binding,
            (binding,),
            deps=combo_runtime_deps(),
        )

        assert starts == []
        assert ends == []
        assert "combo-invalid-superkey" not in manager.combo_state.active_actions

    @pytest.mark.asyncio
    async def test_combo_hold_macro_waits_for_press_registration_before_active(
        self,
    ) -> None:
        manager = DeviceManager()
        binding = RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
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
        action = MappingAction(
            action_type=ActionType.MACRO,
            macro_name="hold",
            macro_loop_mode="hold",
        )

        start_task = asyncio.create_task(
            actions.start_combo_action(
                manager, "macro-hold", action, binding, (binding,), deps=combo_runtime_deps()
            )
        )
        await asyncio.wait_for(press_started.wait(), timeout=1.0)
        await asyncio.sleep(0)

        assert not start_task.done()

        press_continue.set()
        await start_task
        await actions.stop_combo_action(manager, "macro-hold", deps=combo_runtime_deps())

        assert press_registered.is_set()

    @pytest.mark.asyncio
    async def test_combo_overload_restore_runs_after_child_release_for_overlapping_key(
        self,
    ) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = FakeUInput()
        fake_device = FakeComboDevice()
        manager.grabbed_devices = {"1234:5678": [fake_device]}
        trigger_meta = RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_leftmeta",
        )
        trigger_c = RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_c",
        )
        action = MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-overload-overlap",
                mode=SuperkeyMode.OVERLOAD,
                overload_actions=[
                    MappingAction(action_type=ActionType.KEYBOARD, target="key_leftmeta"),
                ],
            ),
        )
        manager.combo_state.active_combos = [
            RuntimeCombo(
                id="combo-overload-overlap",
                name="combo-overload-overlap",
                steps=[RuntimeComboStep(bindings=(trigger_meta, trigger_c))],
                action=action,
                recall_trigger_keys=True,
                restore_trigger_keys=["meta"],
            )
        ]

        await actions.start_combo_action(
            manager,
            "combo-overload-overlap",
            action,
            trigger_c,
            (trigger_meta, trigger_c),
            deps=combo_runtime_deps(),
        )

        fake_device.held = {"key_leftmeta"}
        await actions.stop_combo_action(
            manager, "combo-overload-overlap", deps=combo_runtime_deps()
        )

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
        manager = DeviceManager()
        fake_device = FakeComboDevice()
        manager.grabbed_devices = {"1234:5678": [fake_device]}
        trigger_meta = RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_leftmeta",
        )
        trigger_c = RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_c",
        )
        action = MappingAction(action_type=ActionType.SUPPRESS)
        manager.combo_state.active_combos = [
            RuntimeCombo(
                id="combo-recall-immediate",
                name="combo-recall-immediate",
                steps=[RuntimeComboStep(bindings=(trigger_meta, trigger_c))],
                action=action,
                recall_trigger_keys=True,
                restore_trigger_keys=["meta"],
            )
        ]

        await actions.start_combo_action(
            manager,
            "combo-recall-immediate",
            action,
            trigger_c,
            (trigger_meta, trigger_c),
            deps=combo_runtime_deps(),
        )

        assert fake_device.releases == ["key_c", "key_leftmeta"]
        assert fake_device.presses == ["key_leftmeta"]
        assert fake_device.active == {"key_leftmeta"}
        assert fake_device.recalled == {"key_c"}

    @pytest.mark.asyncio
    async def test_combo_recall_trigger_keys_restores_selected_keys_after_action_stop(
        self,
    ) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = FakeUInput()
        fake_device = FakeComboDevice()
        manager.grabbed_devices = {"1234:5678": [fake_device]}
        trigger_meta = RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_leftmeta",
        )
        trigger_c = RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_c",
        )
        action = MappingAction(action_type=ActionType.KEYBOARD, target="key_f5")
        manager.combo_state.active_combos = [
            RuntimeCombo(
                id="combo-recall-hold",
                name="combo-recall-hold",
                steps=[RuntimeComboStep(bindings=(trigger_meta, trigger_c))],
                action=action,
                recall_trigger_keys=True,
                restore_trigger_keys=["meta"],
            )
        ]

        await actions.start_combo_action(
            manager,
            "combo-recall-hold",
            action,
            trigger_c,
            (trigger_meta, trigger_c),
            deps=combo_runtime_deps(),
        )

        assert fake_device.releases == ["key_c", "key_leftmeta"]
        assert fake_device.presses == []
        assert fake_device.recalled == {"key_c", "key_leftmeta"}
        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F5, 1)
        ]

        fake_device.held = {"key_leftmeta"}
        await actions.stop_combo_action(manager, "combo-recall-hold", deps=combo_runtime_deps())

        assert fake_device.presses == ["key_leftmeta"]
        assert fake_device.recalled == {"key_c"}
        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F5, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_F5, 0),
        ]

    @pytest.mark.asyncio
    async def test_combo_pattern_superkey_single_step_supports_double_tap(self) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = FakeUInput()
        binding = RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        action = MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern",
                mode=SuperkeyMode.PATTERN,
                tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
                double_tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_b")],
            ),
        )
        manager.combo_state.active_combos = [
            RuntimeCombo(
                id="combo-pattern",
                name="combo-pattern",
                steps=[RuntimeComboStep(bindings=(binding,))],
                action=action,
            )
        ]

        await actions.start_combo_action(
            manager, "combo-pattern", action, binding, (binding,), deps=combo_runtime_deps()
        )
        await actions.stop_combo_action(manager, "combo-pattern", deps=combo_runtime_deps())
        await actions.start_combo_action(
            manager, "combo-pattern", action, binding, (binding,), deps=combo_runtime_deps()
        )
        await actions.stop_combo_action(manager, "combo-pattern", deps=combo_runtime_deps())
        await asyncio.sleep(0.02)

        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
        ]

    @pytest.mark.asyncio
    async def test_combo_pattern_superkey_single_step_supports_tap_hold(self) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = FakeUInput()
        binding = RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        action = MappingAction(
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
        manager.combo_state.active_combos = [
            RuntimeCombo(
                id="combo-pattern-tap-hold",
                name="combo-pattern-tap-hold",
                steps=[RuntimeComboStep(bindings=(binding,))],
                action=action,
            )
        ]

        await actions.start_combo_action(
            manager,
            "combo-pattern-tap-hold",
            action,
            binding,
            (binding,),
            deps=combo_runtime_deps(),
        )
        await actions.stop_combo_action(
            manager, "combo-pattern-tap-hold", deps=combo_runtime_deps()
        )
        await actions.start_combo_action(
            manager,
            "combo-pattern-tap-hold",
            action,
            binding,
            (binding,),
            deps=combo_runtime_deps(),
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await actions.stop_combo_action(
            manager, "combo-pattern-tap-hold", deps=combo_runtime_deps()
        )
        await asyncio.sleep(0.02)

        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
        ]

    @pytest.mark.asyncio
    async def test_combo_pattern_superkey_multistep_ignores_double_tap_slots(self) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = FakeUInput()
        first = RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        second = RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_b")
        action = MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern-multi",
                mode=SuperkeyMode.PATTERN,
                tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_c")],
                double_tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_d")],
            ),
        )
        manager.combo_state.active_combos = [
            RuntimeCombo(
                id="combo-pattern-multi",
                name="combo-pattern-multi",
                steps=[
                    RuntimeComboStep(bindings=(first,)),
                    RuntimeComboStep(bindings=(second,)),
                ],
                action=action,
            )
        ]

        await actions.start_combo_action(
            manager, "combo-pattern-multi", action, second, (second,), deps=combo_runtime_deps()
        )
        await actions.stop_combo_action(manager, "combo-pattern-multi", deps=combo_runtime_deps())
        await asyncio.sleep(0.02)

        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_C, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_C, 0),
        ]

    @pytest.mark.asyncio
    async def test_combo_pattern_superkey_multistep_supports_hold_only(self) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = FakeUInput()
        first = RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        second = RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_b")
        action = MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern-hold",
                mode=SuperkeyMode.PATTERN,
                hold_threshold_ms=0,
                hold_actions=[SuperkeyActionData(action_type="keyboard", target="key_e")],
                tap_hold_actions=[SuperkeyActionData(action_type="keyboard", target="key_f")],
            ),
        )
        manager.combo_state.active_combos = [
            RuntimeCombo(
                id="combo-pattern-hold",
                name="combo-pattern-hold",
                steps=[
                    RuntimeComboStep(bindings=(first,)),
                    RuntimeComboStep(bindings=(second,)),
                ],
                action=action,
            )
        ]

        await actions.start_combo_action(
            manager, "combo-pattern-hold", action, second, (second,), deps=combo_runtime_deps()
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await actions.stop_combo_action(manager, "combo-pattern-hold", deps=combo_runtime_deps())
        await asyncio.sleep(0.02)

        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_E, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_E, 0),
        ]

    @pytest.mark.asyncio
    async def test_clear_combo_runtime_releases_active_pattern_superkey_hold(self) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = FakeUInput()
        binding = RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        action = MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern-clear",
                mode=SuperkeyMode.PATTERN,
                hold_threshold_ms=0,
                hold_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
            ),
        )
        manager.combo_state.active_combos = [
            RuntimeCombo(
                id="combo-pattern-clear",
                name="combo-pattern-clear",
                steps=[RuntimeComboStep(bindings=(binding,))],
                action=action,
            )
        ]

        await actions.start_combo_action(
            manager, "combo-pattern-clear", action, binding, (binding,), deps=combo_runtime_deps()
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await lifecycle.clear_combo_runtime(manager, deps=combo_runtime_deps())

        assert manager.combo_state.superkey_machines == {}
        assert manager.output_state.keyboard_uinput.writes == [
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
            (evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        ]

    @pytest.mark.asyncio
    async def test_clear_combo_scope_stops_pending_pattern_superkey_machine(self) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = FakeUInput()
        binding = RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        action = MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern-scope",
                mode=SuperkeyMode.PATTERN,
                tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
                double_tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_b")],
            ),
        )
        manager.combo_state.active_combos = [
            RuntimeCombo(
                id="combo-pattern-scope",
                name="combo-pattern-scope",
                steps=[RuntimeComboStep(bindings=(binding,))],
                action=action,
            )
        ]

        await actions.start_combo_action(
            manager, "combo-pattern-scope", action, binding, (binding,), deps=combo_runtime_deps()
        )
        await actions.stop_combo_action(manager, "combo-pattern-scope", deps=combo_runtime_deps())

        assert "combo-pattern-scope" in manager.combo_state.superkey_machines

        await lifecycle.clear_combo_runtime_for_binding_scope(
            manager, "1234:5678", "kbd", deps=combo_runtime_deps()
        )

        assert manager.combo_state.superkey_machines == {}

    @pytest.mark.asyncio
    async def test_clear_combo_scope_keeps_wildcard_pattern_machine_for_other_device(
        self,
    ) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = FakeUInput()
        configured = RuntimeComboBinding(hardware_id="", source="", evdev="key_a")
        trigger = RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_a",
        )
        action = MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern-wildcard-scope",
                mode=SuperkeyMode.PATTERN,
                tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
                double_tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_b")],
            ),
        )
        manager.combo_state.active_combos = [
            RuntimeCombo(
                id="combo-pattern-wildcard-scope",
                name="combo-pattern-wildcard-scope",
                steps=[RuntimeComboStep(bindings=(configured,))],
                action=action,
            )
        ]

        await actions.start_combo_action(
            manager,
            "combo-pattern-wildcard-scope",
            action,
            trigger,
            (trigger,),
            deps=combo_runtime_deps(),
        )

        await lifecycle.clear_combo_runtime_for_binding_scope(
            manager, "9999:0001", "kbd", deps=combo_runtime_deps()
        )

        assert "combo-pattern-wildcard-scope" in manager.combo_state.active_actions
        assert "combo-pattern-wildcard-scope" in manager.combo_state.superkey_machines

        await actions.stop_combo_action(
            manager, "combo-pattern-wildcard-scope", deps=combo_runtime_deps()
        )
        assert "combo-pattern-wildcard-scope" in manager.combo_state.superkey_machines

        await lifecycle.clear_combo_runtime_for_binding_scope(
            manager, "9999:0001", "kbd", deps=combo_runtime_deps()
        )
        assert "combo-pattern-wildcard-scope" in manager.combo_state.superkey_machines

        await lifecycle.clear_combo_runtime_for_binding_scope(
            manager, "1234:5678", "kbd", deps=combo_runtime_deps()
        )

        assert manager.combo_state.superkey_machines == {}
        assert manager.combo_state.superkey_machine_bindings == {}

    @pytest.mark.asyncio
    async def test_combo_pattern_superkey_recreates_cached_machine_for_new_trigger_device(
        self,
    ) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = FakeUInput()
        configured = RuntimeComboBinding(hardware_id="", source="", evdev="key_a")
        trigger_a = RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_a",
        )
        trigger_b = RuntimeComboBinding(
            hardware_id="9999:0001",
            source="kbd",
            evdev="key_a",
        )
        action = MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern-wildcard-reuse",
                mode=SuperkeyMode.PATTERN,
                tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
                double_tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_b")],
            ),
        )
        manager.combo_state.active_combos = [
            RuntimeCombo(
                id="combo-pattern-wildcard-reuse",
                name="combo-pattern-wildcard-reuse",
                steps=[RuntimeComboStep(bindings=(configured,))],
                action=action,
            )
        ]

        await actions.start_combo_action(
            manager,
            "combo-pattern-wildcard-reuse",
            action,
            trigger_a,
            (trigger_a,),
            deps=combo_runtime_deps(),
        )
        await actions.stop_combo_action(
            manager, "combo-pattern-wildcard-reuse", deps=combo_runtime_deps()
        )
        first_machine = manager.combo_state.superkey_machines["combo-pattern-wildcard-reuse"]

        await actions.start_combo_action(
            manager,
            "combo-pattern-wildcard-reuse",
            action,
            trigger_b,
            (trigger_b,),
            deps=combo_runtime_deps(),
        )
        second_machine = manager.combo_state.superkey_machines["combo-pattern-wildcard-reuse"]

        assert second_machine is not first_machine
        assert second_machine.source_device == "9999:0001"
        assert manager.combo_state.superkey_machine_bindings["combo-pattern-wildcard-reuse"] == (
            trigger_b,
        )

    @pytest.mark.asyncio
    async def test_clear_combo_scope_checks_all_cached_pattern_trigger_bindings(
        self,
    ) -> None:
        manager = DeviceManager()
        manager.output_state.keyboard_uinput = FakeUInput()
        configured_a = RuntimeComboBinding(hardware_id="", source="", evdev="key_a")
        configured_b = RuntimeComboBinding(hardware_id="", source="", evdev="key_b")
        trigger_a = RuntimeComboBinding(
            hardware_id="1234:5678",
            source="kbd",
            evdev="key_a",
        )
        trigger_b = RuntimeComboBinding(
            hardware_id="9999:0001",
            source="kbd",
            evdev="key_b",
        )
        action = MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=SuperkeyConfig(
                name="combo-pattern-multi-scope",
                mode=SuperkeyMode.PATTERN,
                tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_a")],
                double_tap_actions=[SuperkeyActionData(action_type="keyboard", target="key_b")],
            ),
        )
        manager.combo_state.active_combos = [
            RuntimeCombo(
                id="combo-pattern-multi-scope",
                name="combo-pattern-multi-scope",
                steps=[RuntimeComboStep(bindings=(configured_a, configured_b))],
                action=action,
            )
        ]

        await actions.start_combo_action(
            manager,
            "combo-pattern-multi-scope",
            action,
            trigger_b,
            (trigger_a, trigger_b),
            deps=combo_runtime_deps(),
        )
        await actions.stop_combo_action(
            manager, "combo-pattern-multi-scope", deps=combo_runtime_deps()
        )

        assert manager.combo_state.superkey_machine_bindings["combo-pattern-multi-scope"] == (
            trigger_a,
            trigger_b,
        )

        await lifecycle.clear_combo_runtime_for_binding_scope(
            manager, "1234:5678", "kbd", deps=combo_runtime_deps()
        )

        assert manager.combo_state.superkey_machines == {}
        assert manager.combo_state.superkey_machine_bindings == {}

    @pytest.mark.asyncio
    async def test_combo_pattern_superkey_replaces_stale_cached_machine_when_config_changes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = DeviceManager()
        binding = RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
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

        monkeypatch.setattr(actions, "SuperkeyMachine", _FakeMachine)
        manager.combo_state.superkey_machines["combo-pattern-stale"] = old_machine  # type: ignore[assignment]
        action = MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_config=new_config,
        )
        manager.combo_state.active_combos = [
            RuntimeCombo(
                id="combo-pattern-stale",
                name="combo-pattern-stale",
                steps=[RuntimeComboStep(bindings=(binding,))],
                action=action,
            )
        ]

        await actions.start_combo_action(
            manager, "combo-pattern-stale", action, binding, (binding,), deps=combo_runtime_deps()
        )

        old_machine.stop.assert_awaited_once()
        assert len(created) == 1
        assert manager.combo_state.superkey_machines["combo-pattern-stale"] is created[0]

    @pytest.mark.asyncio
    async def test_start_and_stop_combo_action_cover_additional_synthetic_paths(self) -> None:
        manager = DeviceManager()
        manager.output_state.mouse_uinput = FakeUInput()
        manager.output_state.gamepad_uinput = FakeUInput()
        manager.play_macro = AsyncMock(return_value={"status": "ok"})  # type: ignore[method-assign]
        resolve_code = Mock(return_value=evdev.ecodes.BTN_SOUTH)

        binding = RuntimeComboBinding(hardware_id="1234:5678", source="mouse", evdev="btn_side")

        await actions.start_combo_action(
            manager,
            "mouse-move",
            MappingAction(action_type=ActionType.MOUSE_MOVE_REL, move_x=3, move_y=-1),
            binding,
            (binding,),
            deps=combo_runtime_deps(),
        )
        await actions.start_combo_action(
            manager,
            "wheel",
            MappingAction(action_type=ActionType.MOUSE, target="rel_wheel:1"),
            binding,
            (binding,),
            deps=combo_runtime_deps(),
        )
        await actions.start_combo_action(
            manager,
            "tap-wheel",
            MappingAction(
                action_type=ActionType.MOUSE,
                target="rel_wheel:-1",
                tap_enabled=True,
                tap_hold_ms=1,
            ),
            binding,
            (binding,),
            deps=combo_runtime_deps(),
        )
        await asyncio.sleep(0.01)
        await actions.start_combo_action(
            manager,
            "rapid-wheel",
            MappingAction(
                action_type=ActionType.MOUSE,
                target="rel_hwheel:1",
                rapidfire_enabled=True,
                rapidfire_hold_ms=1,
                rapidfire_wait_ms=1,
            ),
            binding,
            (binding,),
            deps=combo_runtime_deps(),
        )
        await asyncio.sleep(0.01)
        await actions.stop_combo_action(manager, "rapid-wheel", deps=combo_runtime_deps())
        await actions.start_combo_action(
            manager,
            "macro",
            MappingAction(
                action_type=ActionType.MACRO,
                macro_name="demo",
                macro_loop_mode="hold",
            ),
            binding,
            (binding,),
            deps=combo_runtime_deps(),
        )
        await actions.start_combo_action(
            manager,
            "exec",
            MappingAction(action_type=ActionType.EXEC, exec_ref=9),
            binding,
            (binding,),
            deps=combo_runtime_deps(),
        )
        await actions.start_combo_action(
            manager,
            "dispatch",
            MappingAction(
                action_type=ActionType.COMPOSITOR_DISPATCH,
                compositor_dispatcher="workspace",
                compositor_args="2",
            ),
            binding,
            (binding,),
            deps=combo_runtime_deps(),
        )
        await actions.start_combo_action(
            manager,
            "record",
            MappingAction(action_type=ActionType.START_MACRO_RECORDING),
            binding,
            (binding,),
            deps=combo_runtime_deps(),
        )
        await actions.start_combo_action(
            manager,
            "profile",
            MappingAction(action_type=ActionType.PROFILE_ENABLE, profile_name="Gaming"),
            binding,
            (binding,),
            deps=combo_runtime_deps(),
        )
        await actions.start_combo_action(
            manager,
            "axis",
            MappingAction(
                action_type=ActionType.GAMEPAD_AXIS,
                target="abs_z",
                axis_value=255,
            ),
            binding,
            (binding,),
            deps=combo_runtime_deps(resolve_code_fn=resolve_code),
        )
        await actions.stop_combo_action(manager, "axis", deps=combo_runtime_deps())
        await actions.start_combo_action(
            manager,
            "tap-axis",
            MappingAction(
                action_type=ActionType.GAMEPAD_AXIS,
                target="abs_z",
                axis_value=255,
                tap_enabled=True,
                tap_hold_ms=25,
            ),
            binding,
            (binding,),
            deps=combo_runtime_deps(resolve_code_fn=resolve_code),
        )
        await actions.stop_combo_action(manager, "tap-axis", deps=combo_runtime_deps())
        await actions.start_combo_action(
            manager,
            "rapid-axis",
            MappingAction(
                action_type=ActionType.GAMEPAD_AXIS,
                target="abs_z",
                axis_value=255,
                rapidfire_enabled=True,
                rapidfire_hold_ms=10,
                rapidfire_wait_ms=10,
            ),
            binding,
            (binding,),
            deps=combo_runtime_deps(resolve_code_fn=resolve_code),
        )
        await actions.stop_combo_action(manager, "rapid-axis", deps=combo_runtime_deps())
        await actions.stop_combo_action(manager, "macro", deps=combo_runtime_deps())

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
    async def test_mpris_combo_action_tracks_release_lifecycle(self) -> None:
        events: list[tuple[CommandType, dict[str, object]]] = []

        async def broadcast(event_type: CommandType, data: dict[str, object]) -> None:
            events.append((event_type, data))

        manager = DeviceManager(broadcast_callback=broadcast)
        binding = RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_m")

        await actions.start_combo_action(
            manager,
            "media-stop",
            MappingAction(action_type=ActionType.MPRIS, mpris_command="stop"),
            binding,
            (binding,),
            deps=combo_runtime_deps(),
        )
        await asyncio.sleep(0)

        assert "media-stop" in manager.combo_state.active_actions
        assert events == [
            (
                CommandType.ACTION_TRIGGER,
                {
                    "action_type": "mpris",
                    "command": "stop",
                    "source_device": "1234:5678",
                    "source_button": "combo:media-stop",
                    "trigger_id": "1234:5678:combo:media-stop",
                },
            )
        ]

        await actions.stop_combo_action(manager, "media-stop", deps=combo_runtime_deps())

        assert "media-stop" not in manager.combo_state.active_actions

    @pytest.mark.asyncio
    async def test_start_combo_action_mouse_move_abs_uses_cursor_position_setter(self) -> None:
        manager = DeviceManager()
        manager.output_state.mouse_uinput = FakeUInput()
        manager.set_cursor_position = AsyncMock(return_value={"status": "ok"})  # type: ignore[method-assign]
        binding = RuntimeComboBinding(hardware_id="1234:5678", source="mouse", evdev="btn_side")

        await actions.start_combo_action(
            manager,
            "mouse-move-abs",
            MappingAction(action_type=ActionType.MOUSE_MOVE_ABS, move_x=33, move_y=44),
            binding,
            (binding,),
            deps=combo_runtime_deps(),
        )

        manager.set_cursor_position.assert_awaited_once_with(33, 44)
        assert manager.output_state.mouse_uinput.writes == []

    @pytest.mark.asyncio
    async def test_combo_pattern_superkey_passes_cursor_position_setter(self, monkeypatch) -> None:
        manager = DeviceManager()
        binding = RuntimeComboBinding(hardware_id="1234:5678", source="kbd", evdev="key_a")
        action = MappingAction(
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

        monkeypatch.setattr(actions, "SuperkeyMachine", _FakeMachine)

        await actions.start_combo_action(
            manager, "combo-pattern-abs", action, binding, (binding,), deps=combo_runtime_deps()
        )

        assert created_setters == [manager.set_cursor_position]
