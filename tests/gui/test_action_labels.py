# ruff: noqa: F403, F405, I001
from tests.gui.support import *


def test_action_labels_describe_all_mapping_action_types() -> None:
    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.action_labels import (
        describe_mapping_action_compact,
        describe_mapping_action_verbose,
    )

    actions = [
        MappingAction(action_type=ActionType.KEYBOARD, target="key_a"),
        MappingAction(action_type=ActionType.MOUSE, target="btn_left"),
        MappingAction(action_type=ActionType.MOUSE_MOVE_REL, move_x=4, move_y=-3),
        MappingAction(action_type=ActionType.MOUSE_MOVE_ABS, move_x=100, move_y=200),
        MappingAction(action_type=ActionType.GAMEPAD, target="btn_south"),
        MappingAction(action_type=ActionType.EXEC, cmd="notify-send hi"),
        MappingAction(
            action_type=ActionType.COMPOSITOR_DISPATCH,
            compositor_dispatcher="workspace",
            compositor_args="2",
        ),
        MappingAction(action_type=ActionType.SUPERKEY, superkey_name="Nav"),
        MappingAction(action_type=ActionType.ANALOG_CONTROL, analog_control_name="FPS Mouse"),
        MappingAction(action_type=ActionType.MACRO, macro_name="paste"),
        MappingAction(action_type=ActionType.START_MACRO_RECORDING),
        MappingAction(action_type=ActionType.STOP_MACRO_RECORDING),
        MappingAction(action_type=ActionType.CANCEL_MACRO_PLAYBACK),
        MappingAction(action_type=ActionType.EMERGENCY_RESET),
        MappingAction(action_type=ActionType.PROFILE_ENABLE, profile_name="Gaming"),
        MappingAction(action_type=ActionType.PROFILE_DISABLE, profile_name="Work"),
        MappingAction(action_type=ActionType.PROFILE_TOGGLE, profile_name="Streaming"),
        MappingAction(action_type=ActionType.SUPPRESS),
        MappingAction(action_type=ActionType.PASSTHROUGH),
    ]

    compact = [describe_mapping_action_compact(action) for action in actions]
    verbose = [
        describe_mapping_action_verbose(
            action,
            keyboard_label=lambda value: f"keyboard:{value}",
            gamepad_label=lambda value: f"gamepad:{value}",
        )
        for action in actions
    ]

    assert compact == [
        "→ key_a",
        "→ btn_left",
        "⇢ 4,-3",
        "⌖ 100,200",
        "🎮 btn_south",
        "▶ notify-send hi",
        "🪟 workspace 2",
        "🌟S: Nav",
        "🕹️ FPS Mouse",
        "🎬 paste",
        "⏺ toggle recording",
        "⏹ stop recording",
        "⏹ cancel playback",
        "⏹ emergency reset",
        "🗂 enable Gaming",
        "🗂 disable Work",
        "🗂 toggle Streaming",
        "× suppress",
        "→ legacy passthrough",
    ]
    assert verbose == [
        "Keyboard → keyboard:key_a",
        "Mouse → btn_left",
        "Mouse Move (rel) → 4, -3",
        "Mouse Move (abs) → 100, 200",
        "Gamepad → gamepad:btn_south",
        "Exec → notify-send hi",
        "Compositor → workspace 2",
        "Super Key → Nav",
        "Analog Control -> FPS Mouse",
        "Macro → paste",
        "Toggle Macro Recording",
        "Stop Macro Recording",
        "Cancel Macro Playback",
        "Emergency Runtime Reset",
        "Enable Profile → Gaming",
        "Disable Profile → Work",
        "Toggle Profile → Streaming",
        "Suppress",
        "Legacy Passthrough",
    ]


def test_action_labels_include_state_flags_and_fallbacks() -> None:
    from keymasq.common.models import ActionType, MappingAction
    from keymasq.gui.widgets.action_labels import (
        describe_mapping_action_compact,
        describe_mapping_action_verbose,
    )

    action = MappingAction(
        action_type=ActionType.KEYBOARD,
        target="key_b",
        rapidfire_enabled=True,
        tap_enabled=True,
    )

    assert describe_mapping_action_compact(None) == "No action selected"
    assert describe_mapping_action_verbose(None) == "No action selected"
    assert describe_mapping_action_compact(action, include_state=True) == "→ key_b ⚡ ↓"
    assert describe_mapping_action_verbose(
        MappingAction(action_type=ActionType.KEYBOARD),
        keyboard_label=lambda value: f"resolved:{value}",
    ) == "Keyboard → resolved:?"
    assert describe_mapping_action_verbose(
        MappingAction(action_type=ActionType.EXEC)
    ) == "Exec → ?"
    assert (
        describe_mapping_action_compact(
            MappingAction(
                action_type=ActionType.ANALOG_CONTROL,
                analog_control_names=["Mouse", "WASD"],
            )
        )
        == "🕹️ 2 controls"
    )
    assert (
        describe_mapping_action_verbose(
            MappingAction(
                action_type=ActionType.ANALOG_CONTROL,
                analog_control_names=["Mouse", "WASD"],
            )
        )
        == "Analog Control -> 2 controls"
    )
