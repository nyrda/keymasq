from __future__ import annotations

from collections.abc import Callable

from keymasq.common.models import ActionType, MappingAction
from keymasq.gui.widgets.compositor_actions import describe_compositor_action


def _resolved_label(
    value: str | None,
    resolver: Callable[[str], str] | None,
) -> str:
    label = value or "?"
    if resolver is None:
        return label
    return resolver(label)


def describe_mapping_action_compact(
    action: MappingAction | None,
    *,
    include_state: bool = False,
) -> str:
    if action is None:
        return "No action selected"

    parts: list[str] = []

    if action.action_type == ActionType.KEYBOARD:
        parts.append(f"→ {action.target or '?'}")
    elif action.action_type == ActionType.MOUSE:
        parts.append(f"→ {action.target or '?'}")
    elif action.action_type == ActionType.MOUSE_MOVE_REL:
        parts.append(f"⇢ {action.move_x},{action.move_y}")
    elif action.action_type == ActionType.MOUSE_MOVE_ABS:
        parts.append(f"⌖ {action.move_x},{action.move_y}")
    elif action.action_type == ActionType.GAMEPAD:
        parts.append(f"🎮 {action.target or '?'}")
    elif action.action_type == ActionType.EXEC:
        cmd = action.cmd or "exec"
        parts.append(f"▶ {cmd}")
    elif action.action_type == ActionType.COMPOSITOR_DISPATCH:
        dispatcher = action.compositor_dispatcher or "dispatch"
        args = str(action.compositor_args or "").strip()
        parts.append(f"🪟 {dispatcher}{f' {args}' if args else ''}")
    elif action.action_type == ActionType.SUPERKEY:
        parts.append(f"🌟S: {action.superkey_name or '?'}")
    elif action.action_type == ActionType.MACRO:
        parts.append(f"🎬 {action.macro_name or '?'}")
    elif action.action_type == ActionType.START_MACRO_RECORDING:
        parts.append("⏺ toggle recording")
    elif action.action_type == ActionType.STOP_MACRO_RECORDING:
        parts.append("⏹ stop recording")
    elif action.action_type == ActionType.CANCEL_MACRO_PLAYBACK:
        parts.append("⏹ cancel playback")
    elif action.action_type == ActionType.PROFILE_ENABLE:
        parts.append(f"🗂 enable {action.profile_name or '?'}")
    elif action.action_type == ActionType.PROFILE_DISABLE:
        parts.append(f"🗂 disable {action.profile_name or '?'}")
    elif action.action_type == ActionType.PROFILE_TOGGLE:
        parts.append(f"🗂 toggle {action.profile_name or '?'}")
    elif action.action_type == ActionType.SUPPRESS:
        parts.append("× suppress")
    elif action.action_type == ActionType.PASSTHROUGH:
        parts.append("→ legacy passthrough")
    else:
        parts.append("→ passthrough")

    if include_state and action.action_type in {
        ActionType.KEYBOARD,
        ActionType.MOUSE,
        ActionType.MOUSE_MOVE_REL,
        ActionType.MOUSE_MOVE_ABS,
        ActionType.GAMEPAD,
    }:
        if action.rapidfire_enabled:
            parts.append("⚡")
        if action.tap_enabled:
            parts.append("↓")

    return " ".join(parts)


def describe_mapping_action_verbose(
    action: MappingAction | None,
    *,
    keyboard_label: Callable[[str], str] | None = None,
    gamepad_label: Callable[[str], str] | None = None,
) -> str:
    if action is None:
        return "No action selected"
    if action.action_type == ActionType.PASSTHROUGH:
        return "Legacy Passthrough"
    if action.action_type == ActionType.SUPPRESS:
        return "Suppress"
    if action.action_type == ActionType.SUPERKEY:
        return f"Super Key → {action.superkey_name or '?'}"
    if action.action_type == ActionType.KEYBOARD:
        return f"Keyboard → {_resolved_label(action.target, keyboard_label)}"
    if action.action_type == ActionType.MOUSE:
        return f"Mouse → {action.target or '?'}"
    if action.action_type == ActionType.MOUSE_MOVE_REL:
        return f"Mouse Move (rel) → {action.move_x}, {action.move_y}"
    if action.action_type == ActionType.MOUSE_MOVE_ABS:
        return f"Mouse Move (abs) → {action.move_x}, {action.move_y}"
    if action.action_type == ActionType.GAMEPAD:
        return f"Gamepad → {_resolved_label(action.target, gamepad_label)}"
    if action.action_type == ActionType.MACRO:
        return f"Macro → {action.macro_name or '?'}"
    if action.action_type == ActionType.EXEC:
        return f"Exec → {action.cmd or '?'}"

    compositor_action = describe_compositor_action(action)
    if compositor_action is not None:
        return compositor_action
    if action.action_type == ActionType.COMPOSITOR_DISPATCH:
        dispatcher = action.compositor_dispatcher or "dispatch"
        args = str(action.compositor_args or "").strip()
        suffix = f" {args}" if args else ""
        return f"Compositor → {dispatcher}{suffix}"

    if action.action_type == ActionType.START_MACRO_RECORDING:
        return "Toggle Macro Recording"
    if action.action_type == ActionType.STOP_MACRO_RECORDING:
        return "Stop Macro Recording"
    if action.action_type == ActionType.CANCEL_MACRO_PLAYBACK:
        return "Cancel Macro Playback"
    if action.action_type == ActionType.PROFILE_ENABLE:
        return f"Enable Profile → {action.profile_name or '?'}"
    if action.action_type == ActionType.PROFILE_DISABLE:
        return f"Disable Profile → {action.profile_name or '?'}"
    if action.action_type == ActionType.PROFILE_TOGGLE:
        return f"Toggle Profile → {action.profile_name or '?'}"
    return action.action_type.value
