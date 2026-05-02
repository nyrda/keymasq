# ruff: noqa: F403, F405, I001
from typing import cast

from tests.session.command_support import *


def _manager_with_superkeys(*configs):
    from keymasq.session.manager.state import ExecRuntimeState, ProfileRuntimeState

    by_name = {config.name: config for config in configs}
    return SimpleNamespace(
        exec_state=ExecRuntimeState(),
        profile_state=ProfileRuntimeState(),
        superkeys=SimpleNamespace(get_superkey=lambda name: by_name.get(name)),
    )


def test_profile_to_mapping_serializes_high_value_action_payloads() -> None:
    from keymasq.common.models import ActionType, MappingAction, SuperkeyConfig, SuperkeyMode
    from keymasq.session.manager import payloads
    from keymasq.session.profiles import ResolvedDeviceProfile

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
            "super": MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_name="launcher",
            ),
        },
    )

    mapping = payloads.profile_to_mapping(manager, resolved, "kbd")

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
    assert manager.exec_state.exec_refs == {1: "echo hi"}
    assert manager.exec_state.device_exec_refs == {"kbd": {1}}
    assert mapping["dispatch"] == {
        "action": "compositor_dispatch",
        "compositor": "hyprland",
        "dispatcher": "workspace",
        "args": "2",
    }
    macro_mapping = cast(dict[str, object], mapping["macro"])
    assert macro_mapping["macro_name"] == "paste"
    assert macro_mapping["macro_loop_count"] == 3
    assert macro_mapping["macro_block_mouse_movement"] is True
    assert mapping["profile"] == {"action": "profile_toggle", "profile_name": "Gaming"}
    super_mapping = cast(dict[str, object], mapping["super"])
    superkey_payload = cast(dict[str, object], super_mapping["superkey"])
    overload_actions = cast(list[dict[str, object]], superkey_payload["overload_actions"])
    assert overload_actions[1] == {
        "action": "exec",
        "exec_ref": 10000,
    }
    assert manager.exec_state.superkey_exec_refs == {10000: ("kbd", "notify-send launcher")}


def test_combo_payloads_filter_invalid_actions_and_track_exec_refs() -> None:
    from keymasq.common.models import (
        ActionType,
        ComboEvent,
        ComboStep,
        MappingAction,
        SuperkeyAction,
        SuperkeyConfig,
        SuperkeyMode,
    )
    from keymasq.session.manager import payloads
    from keymasq.session.profiles import ResolvedCombo

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

    combo_payload = payloads.resolved_combos_payload(manager, combos)

    assert [combo["id"] for combo in combo_payload] == ["dispatch", "super"]
    assert combo_payload[0] == {
        "id": "dispatch",
        "name": "dispatch",
        "profile_name": "Default",
        "steps": [
            {
                "events": [{"hardware_id": "kbd", "evdev": "key_a", "source": "main"}],
                "timeout_ms": 250,
            }
        ],
        "action": {
            "action": "compositor_dispatch",
            "dispatcher": "workspace",
            "args": "3",
        },
        "recall_trigger_keys": True,
        "restore_trigger_keys": ["key_a"],
    }
    combo_action = cast(dict[str, object], combo_payload[1]["action"])
    super_action = cast(dict[str, object], combo_action["superkey"])
    assert super_action["tap_actions"] == [{"action": "exec", "exec_ref": 10000}]
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
    assert manager.exec_state.combo_superkey_exec_refs == {10000}


def test_payload_signatures_and_log_view_are_stable() -> None:
    from keymasq.common.models import ActionType, ComboEvent, ComboStep, MappingAction
    from keymasq.session.manager import payloads
    from keymasq.session.profiles import ResolvedCombo, ResolvedDeviceProfile

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

    mapping_signature = payloads.resolved_mapping_signature(manager, resolved, "kbd")
    combo_signature = payloads.resolved_combos_signature(manager, [combo])
    log_view = payloads.mapping_log_view(
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
