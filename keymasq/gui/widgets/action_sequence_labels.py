"""Human-readable labels for actions edited in ordered sequences."""

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.common.model.superkeys import SuperkeyAction, superkey_action_to_mapping_action
from keymasq.gui.widgets.action_labels import describe_mapping_action_verbose


def _append_action_state_markers(
    label: str,
    action: SuperkeyAction | MappingAction,
) -> str:
    return f"{label} ⚡" if action.rapidfire_enabled else label


def _profile_lifetime_suffix(action: SuperkeyAction) -> str:
    policy = action.profile_deactivation
    if policy is None:
        return ""
    if policy.on_trigger_end and policy.after_actions is None and policy.timeout_ms is None:
        return " (while held)"
    if not policy.on_trigger_end and policy.timeout_ms is None and policy.after_actions == 1:
        return " (one-shot)"
    if not policy.on_trigger_end and policy.timeout_ms is None and policy.after_actions:
        return f" ({int(policy.after_actions)} actions)"
    if not policy.on_trigger_end and policy.after_actions is None and policy.timeout_ms:
        return f" ({int(policy.timeout_ms)} ms)"
    return " (custom)"


def describe_pattern_action(
    action: SuperkeyAction,
    *,
    exec_limit: int,
    exec_prefix: str,
    macro_prefix: str,
    target_separator: str,
    title_case_target_type: bool,
) -> str:
    def type_label(text: str) -> str:
        return text if title_case_target_type else text.lower()

    label: str
    if action.action_type.value == "exec":
        cmd = action.cmd or ""
        rendered = cmd[:exec_limit] + "..." if len(cmd) > exec_limit else cmd
        label = f"{exec_prefix}{rendered}"
    elif action.action_type.value == "macro":
        label = f"{macro_prefix}{action.macro_name or ''}"
    elif action.action_type == ActionType.KEYBOARD:
        label = f"{type_label('Keyboard')}{target_separator}{action.target or ''}"
    elif action.action_type == ActionType.MOUSE:
        label = f"{type_label('Mouse')}{target_separator}{action.target or ''}"
    elif action.action_type == ActionType.GAMEPAD:
        label = f"{type_label('Gamepad')}{target_separator}{action.target or ''}"
    elif action.action_type == ActionType.GAMEPAD_AXIS:
        label = (
            f"{type_label('Gamepad Axis')}{target_separator}"
            f"{action.target or ''}={int(action.axis_value)}"
        )
    elif action.action_type == ActionType.MOUSE_MOVE_REL:
        label = (
            f"{type_label('Mouse Move (rel)')}{target_separator}{action.move_x}, {action.move_y}"
        )
    elif action.action_type == ActionType.MOUSE_MOVE_ABS:
        label = (
            f"{type_label('Mouse Move (abs)')}{target_separator}{action.move_x}, {action.move_y}"
        )
    elif action.action_type == ActionType.MOUSE_MOVE_NATURAL_ABS:
        label = (
            f"{type_label('Mouse Move (natural)')}{target_separator}"
            f"{action.move_x}, {action.move_y}"
        )
    elif action.action_type == ActionType.COMPOSITOR_DISPATCH:
        description = describe_mapping_action_verbose(superkey_action_to_mapping_action(action))
        target = description.split("→", 1)[1].strip() if "→" in description else description
        label = f"{type_label('Compositor')}{target_separator}{target}"
    elif action.action_type == ActionType.START_MACRO_RECORDING:
        label = type_label("Toggle Macro Recording")
    elif action.action_type == ActionType.STOP_MACRO_RECORDING:
        label = type_label("Stop Macro Recording")
    elif action.action_type == ActionType.PLAY_MACRO_SLOT:
        slot = f" {action.macro_recording_slot}" if action.macro_recording_slot else ""
        label = type_label(f"Play Macro Recording Slot{slot}")
    elif action.action_type == ActionType.CANCEL_MACRO_PLAYBACK:
        label = type_label("Cancel Macro Playback")
    elif action.action_type == ActionType.EMERGENCY_RESET:
        label = type_label("Emergency Runtime Reset")
    elif action.action_type == ActionType.PROFILE_ENABLE:
        label = (
            f"{type_label('Enable Profile')}{target_separator}{action.profile_name or ''}"
            f"{_profile_lifetime_suffix(action)}"
        )
    elif action.action_type == ActionType.PROFILE_DISABLE:
        label = f"{type_label('Disable Profile')}{target_separator}{action.profile_name or ''}"
    elif action.action_type == ActionType.PROFILE_TOGGLE:
        label = (
            f"{type_label('Toggle Profile')}{target_separator}{action.profile_name or ''}"
            f"{_profile_lifetime_suffix(action)}"
        )
    else:
        label = describe_mapping_action_verbose(superkey_action_to_mapping_action(action))
    return _append_action_state_markers(label, action)


def describe_superkey_editor_action(action: SuperkeyAction) -> str:
    return describe_pattern_action(
        action,
        exec_limit=40,
        exec_prefix="Exec -> ",
        macro_prefix="Macro -> ",
        target_separator=" -> ",
        title_case_target_type=True,
    )


def describe_mapping_editor_action(action: MappingAction) -> str:
    return _append_action_state_markers(describe_mapping_action_verbose(action), action)
