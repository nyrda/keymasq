"""Pure serialization helpers for macro mapping actions."""

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
    data["move_to_start"] = bool(action.macro_move_to_start)
    data["start_x"] = int(action.macro_start_x)
    data["start_y"] = int(action.macro_start_y)
    data["block_mouse_movement"] = bool(action.macro_block_mouse_movement)


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
    data["macro_move_to_start"] = bool(action.macro_move_to_start)
    data["macro_start_x"] = int(action.macro_start_x)
    data["macro_start_y"] = int(action.macro_start_y)
    data["macro_block_mouse_movement"] = bool(action.macro_block_mouse_movement)
    return True
