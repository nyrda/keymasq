from types import SimpleNamespace
from typing import cast

from keymasq.session.manager.payload import action as action_payload
from keymasq.session.manager.payload import combo as combo_serializer
from keymasq.session.manager.payload import mapping as mapping_payload


def _manager_with_superkeys(*configs, analog_controls=()):
    from keymasq.session.manager.state import ExecRuntimeState, ProfileRuntimeState

    by_name = {config.name: config for config in configs}
    analog_by_name = {config.name: config for config in analog_controls}
    return SimpleNamespace(
        exec_state=ExecRuntimeState(),
        profile_state=ProfileRuntimeState(),
        analog_controls=SimpleNamespace(
            get_analog_control=lambda name: analog_by_name.get(name),
        ),
        superkeys=SimpleNamespace(get_superkey=lambda name: by_name.get(name)),
    )


def test_profile_to_mapping_serializes_high_value_action_payloads() -> None:
    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.core import (
        ActionType,
        SuperkeyMode,
    )
    from keymasq.common.model.superkeys import SuperkeyConfig
    from keymasq.session.profile.types import ResolvedDeviceProfile

    superkey = SuperkeyConfig(
        name="launcher",
        mode=SuperkeyMode.OVERLOAD,
        overload_actions=[
            MappingAction(action_type=ActionType.KEYBOARD, target="key_space"),
            MappingAction(action_type=ActionType.EXEC, cmd="notify-send launcher"),
        ],
    )
    manager = _manager_with_superkeys(superkey)
    resolved = ResolvedDeviceProfile(
        hardware_id="kbd",
        mappings={
            "a": MappingAction(
                action_type=ActionType.KEYBOARD,
                target="key_a",
                rapidfire_enabled=True,
                rapidfire_hold_ms=7,
                rapidfire_wait_ms=9,
                tap_enabled=True,
                tap_hold_ms=12,
            ),
            "move": MappingAction(
                action_type=ActionType.MOUSE_MOVE_ABS,
                target="cursor",
                move_x=320,
                move_y=240,
            ),
            "exec": MappingAction(action_type=ActionType.EXEC, cmd="echo hi"),
            "dispatch": MappingAction(
                action_type=ActionType.COMPOSITOR_DISPATCH,
                compositor_id="hyprland",
                compositor_dispatcher="workspace",
                compositor_args="2",
            ),
            "mpris": MappingAction(
                action_type=ActionType.MPRIS,
                mpris_command="play_pause",
            ),
            "macro": MappingAction(
                action_type=ActionType.MACRO,
                macro_name="paste",
                macro_replay_mouse_movement=False,
                macro_replay_mouse_clicks=True,
                macro_speed=1.25,
                macro_loop_mode="count",
                macro_loop_count=3,
                macro_loop_stop_behavior="cancel_run",
                macro_move_to_start=True,
                macro_start_x=10,
                macro_start_y=20,
                macro_block_mouse_movement=True,
            ),
            "profile": MappingAction(
                action_type=ActionType.PROFILE_TOGGLE,
                profile_name="Gaming",
            ),
            "repeat": MappingAction(
                action_type=ActionType.REPEAT,
                repeat_categories=["keyboard", "mouse"],
                rapidfire_enabled=True,
                rapidfire_hold_ms=11,
                rapidfire_wait_ms=13,
            ),
            "super": MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_name="launcher",
            ),
        },
    )

    mapping = mapping_payload.serialize(manager, resolved, "kbd")

    assert mapping["a"] == {
        "action": "keyboard",
        "target": "key_a",
        "rapidfire_enabled": True,
        "rapidfire_hold_ms": 7,
        "rapidfire_wait_ms": 9,
        "tap_enabled": True,
        "tap_hold_ms": 12,
    }
    assert mapping["move"] == {
        "action": "mouse_move_abs",
        "target": "cursor",
        "x": 320,
        "y": 240,
    }
    assert mapping["exec"] == {"action": "exec", "exec_ref": 1}
    assert manager.exec_state.exec_refs[1].cmd == "echo hi"
    assert manager.exec_state.exec_refs[1].owner == "device"
    assert manager.exec_state.exec_refs[1].hardware_id == "kbd"
    assert manager.exec_state.device_exec_refs == {"kbd": {1, 2}}
    assert mapping["dispatch"] == {
        "action": "compositor_dispatch",
        "compositor": "hyprland",
        "dispatcher": "workspace",
        "args": "2",
    }
    assert mapping["mpris"] == {"action": "mpris", "command": "play_pause"}
    macro_mapping = cast(dict[str, object], mapping["macro"])
    assert macro_mapping["macro_name"] == "paste"
    assert macro_mapping["macro_loop_count"] == 3
    assert macro_mapping["macro_block_mouse_movement"] is True
    assert mapping["profile"] == {"action": "profile_toggle", "profile_name": "Gaming"}
    assert mapping["repeat"] == {
        "action": "repeat",
        "repeat_categories": ["keyboard", "mouse"],
        "rapidfire_enabled": True,
        "rapidfire_hold_ms": 11,
        "rapidfire_wait_ms": 13,
    }
    super_mapping = cast(dict[str, object], mapping["super"])
    superkey_payload = cast(dict[str, object], super_mapping["superkey"])
    overload_actions = cast(list[dict[str, object]], superkey_payload["overload_actions"])
    assert overload_actions[1] == {
        "action": "exec",
        "exec_ref": 2,
    }
    assert manager.exec_state.exec_refs[2].cmd == "notify-send launcher"
    assert manager.exec_state.exec_refs[2].owner == "device"
    assert manager.exec_state.exec_refs[2].hardware_id == "kbd"


def test_shared_mapping_action_serializer_preserves_inspector_contract() -> None:
    from keymasq.common.model.actions import (
        MappingAction,
        ProfileDeactivationPolicy,
    )
    from keymasq.common.model.core import ActionType

    assert action_payload.serialize_mapping_action(
        MappingAction(
            action_type=ActionType.MACRO,
            macro_name="paste",
            macro_replay_mouse_movement=False,
            macro_speed=1.5,
            macro_loop_mode="count",
            macro_loop_count=2,
            macro_move_to_start=True,
            macro_start_x=10,
            macro_start_y=20,
        )
    ) == {
        "action": "macro",
        "target": "paste",
        "replay_mouse_movement": False,
        "replay_mouse_clicks": True,
        "speed": 1.5,
        "loop_mode": "count",
        "loop_count": 2,
        "loop_stop_behavior": "finish_run",
        "move_to_start": True,
        "start_x": 10,
        "start_y": 20,
        "block_mouse_movement": False,
    }
    assert action_payload.serialize_mapping_action(
        MappingAction(
            action_type=ActionType.PROFILE_ENABLE,
            profile_name="Gaming",
            source_profile_name="Runtime",
            profile_deactivation=ProfileDeactivationPolicy(after_actions=1),
        )
    ) == {
        "action": "profile_enable",
        "source_profile_name": "Runtime",
        "profile_name": "Gaming",
        "target": "Gaming",
        "deactivation": {"after_actions": 1},
    }
    assert action_payload.serialize_mapping_action(
        MappingAction(
            action_type=ActionType.GAMEPAD_AXIS,
            target="x",
            output_id="virtual-gamepad-2",
            axis_value=123,
        )
    ) == {
        "action": "gamepad_axis",
        "target": "abs_x",
        "output_id": "virtual-gamepad-2",
        "value": 123,
    }
    assert action_payload.serialize_mapping_action(
        MappingAction(action_type=ActionType.REPEAT, repeat_categories=["keyboard"])
    ) == {
        "action": "repeat",
        "repeat_categories": ["keyboard"],
    }
    assert action_payload.serialize_mapping_action(
        MappingAction(action_type=ActionType.PLAY_MACRO_SLOT, macro_recording_slot=2)
    ) == {
        "action": "play_macro_slot",
        "recording_slot": 2,
    }
    analog_action = MappingAction(
        action_type=ActionType.ANALOG_CONTROL,
        analog_control_name="Legacy Control",
    )
    analog_action.analog_control_names = []
    assert action_payload.serialize_mapping_action(analog_action) == {
        "action": "analog_control",
        "analog_control_name": "Legacy Control",
    }


def test_shared_action_serializer_preserves_mode_specific_policies() -> None:
    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.core import ActionType
    from keymasq.session.profile.types import ResolvedDeviceProfile

    manager = _manager_with_superkeys()
    exec_action = MappingAction(action_type=ActionType.EXEC, cmd="echo hi")
    dispatch_action = MappingAction(
        action_type=ActionType.COMPOSITOR_DISPATCH,
        compositor_dispatcher="  workspace  ",
        compositor_args="2",
    )

    mapping = mapping_payload.serialize(
        manager,
        ResolvedDeviceProfile(hardware_id="kbd", mappings={"exec": exec_action}),
        "kbd",
    )
    exec_mapping = cast(dict[str, object], mapping["exec"])
    combo_payload = action_payload.combo_action_to_payload(
        manager,
        dispatch_action,
        step_count=1,
    )
    combo_signature = action_payload.combo_action_signature_payload(
        manager,
        dispatch_action,
        step_count=1,
    )

    assert action_payload.action_signature_payload(manager, exec_action, "kbd") == {
        "action": "exec",
        "cmd": "echo hi",
    }
    assert exec_mapping == {"action": "exec", "exec_ref": 1}
    assert combo_payload == {
        "action": "compositor_dispatch",
        "dispatcher": "workspace",
        "args": "2",
    }
    assert combo_signature == {
        "action": "compositor_dispatch",
        "dispatcher": "  workspace  ",
        "args": "2",
    }


def test_gamepad_payloads_include_output_id_and_signature_changes() -> None:
    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.core import ActionType
    from keymasq.common.model.profiles import (
        ComboEvent,
        ComboStep,
    )
    from keymasq.session.profile.types import (
        ResolvedCombo,
        ResolvedDeviceProfile,
    )

    manager = _manager_with_superkeys()
    action = MappingAction(
        action_type=ActionType.GAMEPAD,
        target="btn_a",
        output_id="virtual-gamepad-2",
    )
    resolved = ResolvedDeviceProfile(hardware_id="pad", mappings={"x": action})

    mapping = mapping_payload.serialize(manager, resolved, "pad")
    mapped_action = cast(dict[str, object], mapping["x"])
    assert mapped_action["output_id"] == "virtual-gamepad-2"

    combo_payload = combo_serializer.serialize_all(
        manager,
        [
            ResolvedCombo(
                id="combo",
                name="combo",
                steps=[ComboStep(events=[ComboEvent(hardware_id="pad", evdev="btn_x")])],
                action=action,
            )
        ],
    )
    combo_action = cast(dict[str, object], combo_payload[0]["action"])
    assert combo_action["output_id"] == "virtual-gamepad-2"

    default_sig = mapping_payload.signature(
        manager,
        ResolvedDeviceProfile(
            hardware_id="pad",
            mappings={"x": MappingAction(action_type=ActionType.GAMEPAD, target="btn_a")},
        ),
        "pad",
    )
    routed_sig = mapping_payload.signature(manager, resolved, "pad")
    assert default_sig != routed_sig


def test_repeat_combo_payload_includes_categories_and_rapidfire() -> None:
    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.core import ActionType
    from keymasq.common.model.profiles import (
        ComboEvent,
        ComboStep,
    )
    from keymasq.session.profile.types import ResolvedCombo

    manager = _manager_with_superkeys()
    combo_payload = combo_serializer.serialize_all(
        manager,
        [
            ResolvedCombo(
                id="repeat-combo",
                name="Repeat Combo",
                steps=[ComboStep(events=[ComboEvent(hardware_id="kbd", evdev="key_f13")])],
                action=MappingAction(
                    action_type=ActionType.REPEAT,
                    repeat_categories=["keyboard", "gamepad"],
                    rapidfire_enabled=True,
                    rapidfire_hold_ms=15,
                    rapidfire_wait_ms=25,
                ),
            )
        ],
    )

    assert combo_payload[0]["action"] == {
        "action": "repeat",
        "repeat_categories": ["keyboard", "gamepad"],
        "rapidfire_enabled": True,
        "rapidfire_hold_ms": 15,
        "rapidfire_wait_ms": 25,
    }


def test_profile_to_mapping_serializes_multiple_analog_controls() -> None:
    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.analog import AnalogControlConfig
    from keymasq.common.model.core import ActionType
    from keymasq.session.profile.types import ResolvedDeviceProfile

    manager = _manager_with_superkeys(
        analog_controls=[
            AnalogControlConfig(name="Mouse"),
            AnalogControlConfig(name="WASD"),
        ],
    )
    resolved = ResolvedDeviceProfile(
        hardware_id="pad",
        mappings={
            "left_stick": MappingAction(
                action_type=ActionType.ANALOG_CONTROL,
                analog_control_names=["Mouse", "WASD"],
            )
        },
    )

    mapping = mapping_payload.serialize(manager, resolved, "pad")
    action = cast(dict[str, object], mapping["left_stick"])
    controls = cast(list[dict[str, object]], action["analog_controls"])

    assert "analog_control" not in action
    assert [control["name"] for control in controls] == ["Mouse", "WASD"]


def test_profile_to_mapping_normalizes_obsolete_mouse_plus_digital() -> None:
    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.analog import (
        AnalogActionThreshold,
        AnalogControlConfig,
        AnalogMouseMotionConfig,
    )
    from keymasq.common.model.core import ActionType
    from keymasq.session.profile.types import ResolvedDeviceProfile

    manager = _manager_with_superkeys(
        analog_controls=[
            AnalogControlConfig(
                name="Old Combined",
                mouse_motion=AnalogMouseMotionConfig(enabled=True),
                thresholds=[
                    AnalogActionThreshold(
                        axis="x",
                        trigger_min=0.65,
                        trigger_max=1.0,
                        release_min=0.55,
                        release_max=1.0,
                        actions=[MappingAction(action_type=ActionType.KEYBOARD, target="key_e")],
                    )
                ],
            ),
        ],
    )
    resolved = ResolvedDeviceProfile(
        hardware_id="pad",
        mappings={
            "left_stick": MappingAction(
                action_type=ActionType.ANALOG_CONTROL,
                analog_control_name="Old Combined",
            )
        },
    )

    mapping = mapping_payload.serialize(manager, resolved, "pad")
    action = cast(dict[str, object], mapping["left_stick"])
    control = cast(dict[str, object], action["analog_control"])
    mouse_motion = cast(dict[str, object], control["mouse_motion"])
    thresholds = cast(list[dict[str, object]], control["thresholds"])

    assert mouse_motion["enabled"] is False
    assert len(thresholds) == 1


def test_combo_payloads_filter_invalid_actions_and_track_exec_refs() -> None:
    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.core import (
        ActionType,
        SuperkeyMode,
    )
    from keymasq.common.model.profiles import (
        ComboEvent,
        ComboStep,
    )
    from keymasq.common.model.superkeys import (
        SuperkeyAction,
        SuperkeyConfig,
    )
    from keymasq.session.profile.types import ResolvedCombo

    superkey = SuperkeyConfig(
        name="pattern",
        mode=SuperkeyMode.PATTERN,
        tap_actions=[SuperkeyAction(action_type=ActionType.EXEC, cmd="echo tap")],
        double_tap_actions=[
            SuperkeyAction(
                action_type=ActionType.COMPOSITOR_DISPATCH,
                compositor_dispatcher="togglefloating",
            )
        ],
        hold_actions=[
            SuperkeyAction(
                action_type=ActionType.MACRO,
                macro_name="hold_macro",
                macro_loop_mode="hold",
                macro_loop_stop_behavior="cancel_run",
            )
        ],
        tap_hold_actions=[
            SuperkeyAction(action_type=ActionType.PROFILE_ENABLE, profile_name="Work")
        ],
    )
    manager = _manager_with_superkeys(superkey)
    good_step = ComboStep(
        events=[
            ComboEvent(hardware_id="kbd", source="main", evdev="key_a"),
            ComboEvent(hardware_id="", source="ignored", evdev="key_b"),
        ],
        timeout_ms=250,
    )
    empty_step = ComboStep(events=[ComboEvent(hardware_id="kbd", evdev="")])

    combos = [
        ResolvedCombo(
            id="empty",
            name="empty",
            steps=[good_step],
            action=MappingAction(action_type=ActionType.EXEC),
        ),
        ResolvedCombo(
            id="dispatch",
            name="dispatch",
            steps=[good_step, empty_step],
            action=MappingAction(
                action_type=ActionType.COMPOSITOR_DISPATCH,
                compositor_dispatcher="workspace",
                compositor_args="3",
            ),
            profile_name="Default",
            recall_trigger_keys=True,
            restore_trigger_keys=["key_a"],
        ),
        ResolvedCombo(
            id="super",
            name="super",
            steps=[good_step],
            action=MappingAction(action_type=ActionType.SUPERKEY, superkey_name="pattern"),
        ),
    ]

    combo_payload = combo_serializer.serialize_all(manager, combos)

    assert [combo["id"] for combo in combo_payload] == ["dispatch", "super"]
    assert combo_payload[0] == {
        "id": "dispatch",
        "name": "dispatch",
        "profile_name": "Default",
        "steps": [
            {
                "events": [
                    {"hardware_id": "kbd", "evdev": "key_a", "source": "main"},
                    {"evdev": "key_b", "source": "ignored"},
                ],
                "timeout_ms": 250,
            }
        ],
        "action": {
            "action": "compositor_dispatch",
            "dispatcher": "workspace",
            "args": "3",
        },
        "match_across_devices": False,
        "recall_trigger_keys": True,
        "restore_trigger_keys": ["key_a"],
    }
    combo_action = cast(dict[str, object], combo_payload[1]["action"])
    super_action = cast(dict[str, object], combo_action["superkey"])
    assert super_action["tap_actions"] == [{"action": "exec", "exec_ref": 1}]
    assert super_action["double_tap_actions"] == [
        {
            "action": "compositor_dispatch",
            "dispatcher": "togglefloating",
            "args": "",
        }
    ]
    hold_actions = cast(list[dict[str, object]], super_action["hold_actions"])
    assert hold_actions[0]["macro_name"] == "hold_macro"
    assert super_action["tap_hold_actions"] == [
        {"action": "profile_enable", "profile_name": "Work"}
    ]
    assert manager.exec_state.combo_exec_refs == {1}
    assert manager.exec_state.exec_refs[1].cmd == "echo tap"
    assert manager.exec_state.exec_refs[1].owner == "combo"


def test_combo_payload_and_signature_include_match_across_devices() -> None:
    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.core import ActionType
    from keymasq.common.model.profiles import (
        ComboEvent,
        ComboStep,
    )
    from keymasq.session.profile.types import ResolvedCombo

    manager = _manager_with_superkeys()
    base_combo = ResolvedCombo(
        id="any-device",
        name="Any Device",
        steps=[ComboStep(events=[ComboEvent(evdev="key_f13")])],
        action=MappingAction(action_type=ActionType.SUPPRESS),
        match_across_devices=False,
    )
    any_device_combo = ResolvedCombo(
        id="any-device",
        name="Any Device",
        steps=[ComboStep(events=[ComboEvent(evdev="key_f13")])],
        action=MappingAction(action_type=ActionType.SUPPRESS),
        match_across_devices=True,
    )

    base_signature = combo_serializer.signature(manager, [base_combo])
    any_device_signature = combo_serializer.signature(manager, [any_device_combo])
    combo_payload = combo_serializer.serialize(manager, any_device_combo)

    assert base_signature != any_device_signature
    assert '"match_across_devices":false' in base_signature
    assert '"match_across_devices":true' in any_device_signature
    assert combo_payload is not None
    assert combo_payload["match_across_devices"] is True


def test_payload_signatures_and_log_view_are_stable() -> None:
    from keymasq.common.model.actions import MappingAction
    from keymasq.common.model.core import ActionType
    from keymasq.common.model.profiles import (
        ComboEvent,
        ComboStep,
    )
    from keymasq.session.profile.types import (
        ResolvedCombo,
        ResolvedDeviceProfile,
    )

    manager = _manager_with_superkeys()
    resolved = ResolvedDeviceProfile(
        hardware_id="kbd",
        mappings={
            "b": MappingAction(action_type=ActionType.EXEC, cmd="echo b"),
            "a": MappingAction(
                action_type=ActionType.MACRO,
                macro_name="typed",
                macro_events=[{"t_us": 1}, {"t_us": 2}],
            ),
        },
    )
    combo = ResolvedCombo(
        id="combo",
        name="Combo",
        profile_name="Default",
        steps=[
            ComboStep(
                events=[
                    ComboEvent(hardware_id="kbd", source="z", evdev="key_z"),
                    ComboEvent(hardware_id="kbd", source="a", evdev="key_a"),
                ]
            )
        ],
        action=MappingAction(action_type=ActionType.MACRO, macro_name="typed"),
    )

    mapping_signature = mapping_payload.signature(manager, resolved, "kbd")
    combo_signature = combo_serializer.signature(manager, [combo])
    log_view = mapping_payload.log_view(
        {
            "macro": {
                "action": "macro",
                "macro_events": [{"t_us": 1}, {"t_us": 2}],
            },
            "raw": "unchanged",
        }
    )

    assert '"cmd":"echo b"' in mapping_signature
    assert combo_signature.index('"source":"a"') < combo_signature.index('"source":"z"')
    macro_log = cast(dict[str, object], log_view["macro"])
    assert macro_log["macro_events"] == "<2 events>"
    assert log_view["raw"] == "unchanged"
