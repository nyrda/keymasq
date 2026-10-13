"""Pure serialization helpers for macro mapping actions."""

import json
from typing import cast

from keymasq.common.model.actions import MappingAction


def add_inspector_fields(data: dict[str, object], action: MappingAction) -> None:
    """Add the GUI inspector representation of a macro action."""
    data["target"] = action.macro_name or ""
    data["replay_mouse_movement"] = bool(action.macro_replay_mouse_movement)
    data["replay_mouse_clicks"] = bool(action.macro_replay_mouse_clicks)
    data["speed"] = float(action.macro_speed)
    data["loop_mode"] = action.macro_loop_mode
    data["loop_count"] = int(action.macro_loop_count)
    data["loop_stop_behavior"] = action.macro_loop_stop_behavior
    data["pause_timeout_s"] = action.macro_pause_timeout_s
    data["block_mouse_movement"] = bool(action.macro_block_mouse_movement)
    data["block_keyboard"] = bool(action.macro_block_keyboard)


def add_runtime_fields(
    data: dict[str, object],
    action: MappingAction,
    *,
    include_empty: bool,
) -> bool:
    """Add daemon-facing macro fields, returning whether a payload was emitted."""
    if not include_empty and not action.macro_name:
        return False
    data["macro_name"] = action.macro_name or ""
    data["macro_replay_mouse_movement"] = bool(action.macro_replay_mouse_movement)
    data["macro_replay_mouse_clicks"] = bool(action.macro_replay_mouse_clicks)
    data["macro_speed"] = float(action.macro_speed)
    data["macro_loop_mode"] = action.macro_loop_mode
    data["macro_loop_count"] = int(action.macro_loop_count)
    data["macro_loop_stop_behavior"] = action.macro_loop_stop_behavior
    data["macro_pause_timeout_s"] = action.macro_pause_timeout_s
    data["macro_block_mouse_movement"] = bool(action.macro_block_mouse_movement)
    data["macro_block_keyboard"] = bool(action.macro_block_keyboard)
    return True


def signature_references(signature: str, macro_names: set[str]) -> bool:
    """Return whether a sent mapping or combo signature names one of the macros."""
    if not signature:
        return False
    try:
        payload = json.loads(signature)
    except ValueError:
        return True
    return _payload_references(payload, macro_names)


def _payload_references(value: object, macro_names: set[str]) -> bool:
    if isinstance(value, dict):
        payload = cast(dict[str, object], value)
        if payload.get("macro_name") in macro_names:
            return True
        return any(_payload_references(item, macro_names) for item in payload.values())
    if isinstance(value, list):
        return any(_payload_references(item, macro_names) for item in cast(list[object], value))
    return False
