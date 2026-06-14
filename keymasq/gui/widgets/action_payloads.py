from typing import cast

from keymasq.common.coercion import bool_value, coerce_float, coerce_int
from keymasq.common.models import (
    DEFAULT_NATURAL_MOUSE_MOVE_SPEED,
    DEFAULT_RAPIDFIRE_HOLD_MS,
    DEFAULT_RAPIDFIRE_WAIT_MS,
    ActionType,
    MappingAction,
    normalize_macro_loop_stop_behavior,
    normalize_macro_recording_slot,
    normalize_natural_mouse_move_curve,
    parse_profile_deactivation_policy,
)

_PROFILE_ACTION_TYPES = {
    ActionType.PROFILE_ENABLE,
    ActionType.PROFILE_DISABLE,
    ActionType.PROFILE_TOGGLE,
}


def mapping_action_from_payload(value: object) -> MappingAction | None:
    if not isinstance(value, dict):
        return None

    action_data = cast(dict[str, object], value)
    action_type = _action_type(action_data.get("action"))
    macro_name = _macro_name(action_data, action_type)
    profile_name = _profile_name(action_data, action_type)
    analog_control_names = _string_list(action_data.get("analog_control_names"))
    analog_control_name = _optional_text(action_data.get("analog_control_name"))
    if not analog_control_names and analog_control_name is not None:
        analog_control_names = [analog_control_name]

    return MappingAction(
        action_type=action_type,
        target=_optional_text(action_data.get("target")),
        output_id=_optional_text(action_data.get("output_id")),
        keys=_optional_string_list(action_data.get("keys")),
        cmd=_optional_text(action_data.get("cmd")),
        superkey_name=_optional_text(action_data.get("superkey_name")),
        analog_control_names=analog_control_names,
        macro_name=macro_name,
        macro_replay_mouse_movement=bool_value(
            _first_value(
                action_data,
                "replay_mouse_movement",
                "macro_replay_mouse_movement",
                default=True,
            )
        ),
        macro_replay_mouse_clicks=bool_value(
            _first_value(
                action_data,
                "replay_mouse_clicks",
                "macro_replay_mouse_clicks",
                default=True,
            )
        ),
        macro_speed=coerce_float(_first_value(action_data, "speed", "macro_speed"), 1.0),
        macro_loop_mode=_text(_first_value(action_data, "loop_mode", "macro_loop_mode"), "none")
        or "none",
        macro_loop_count=coerce_int(
            _first_value(action_data, "loop_count", "macro_loop_count"),
            1,
        ),
        macro_loop_stop_behavior=normalize_macro_loop_stop_behavior(
            _first_value(action_data, "loop_stop_behavior", "macro_loop_stop_behavior")
        ),
        macro_move_to_start=bool_value(
            _first_value(action_data, "move_to_start", "macro_move_to_start")
        ),
        macro_start_x=coerce_int(_first_value(action_data, "start_x", "macro_start_x"), 0),
        macro_start_y=coerce_int(_first_value(action_data, "start_y", "macro_start_y"), 0),
        macro_block_mouse_movement=bool_value(
            _first_value(action_data, "block_mouse_movement", "macro_block_mouse_movement")
        ),
        macro_recording_slot=normalize_macro_recording_slot(
            _first_value(action_data, "recording_slot", "slot")
        ),
        profile_name=profile_name,
        compositor_id=_optional_text(action_data.get("compositor")),
        compositor_dispatcher=_optional_text(action_data.get("dispatcher")),
        compositor_args=_optional_text(action_data.get("args")),
        mpris_command=_optional_text(action_data.get("command")),
        move_x=coerce_int(action_data.get("x"), 0),
        move_y=coerce_int(action_data.get("y"), 0),
        move_speed=coerce_float(action_data.get("speed"), DEFAULT_NATURAL_MOUSE_MOVE_SPEED),
        move_jitter=coerce_float(action_data.get("jitter"), 0.3),
        move_curve=normalize_natural_mouse_move_curve(action_data.get("curve")),
        move_tolerance=coerce_int(action_data.get("tolerance"), 2),
        move_max_duration_ms=coerce_int(action_data.get("max_duration_ms"), 3000),
        move_stop_on_failure=bool_value(action_data.get("stop_on_failure")),
        axis_value=coerce_int(action_data.get("value"), 0),
        rapidfire_enabled=bool_value(action_data.get("rapidfire_enabled")),
        rapidfire_hold_ms=coerce_int(
            action_data.get("rapidfire_hold_ms"),
            DEFAULT_RAPIDFIRE_HOLD_MS,
        ),
        rapidfire_wait_ms=coerce_int(
            action_data.get("rapidfire_wait_ms"),
            DEFAULT_RAPIDFIRE_WAIT_MS,
        ),
        tap_enabled=bool_value(action_data.get("tap_enabled")),
        tap_hold_ms=coerce_int(action_data.get("tap_hold_ms"), 10),
        source_profile_name=_optional_text(action_data.get("source_profile_name")),
        profile_deactivation=parse_profile_deactivation_policy(action_data.get("deactivation")),
        repeat_categories=_optional_string_list(action_data.get("repeat_categories")),
    )


def _action_type(value: object) -> ActionType:
    try:
        return ActionType(_text(value, ActionType.PASSTHROUGH.value))
    except ValueError:
        return ActionType.PASSTHROUGH


def _macro_name(action_data: dict[str, object], action_type: ActionType) -> str | None:
    if action_type != ActionType.MACRO:
        return None
    return _optional_text(action_data.get("macro_name")) or _optional_text(
        action_data.get("target")
    )


def _profile_name(action_data: dict[str, object], action_type: ActionType) -> str | None:
    if action_type not in _PROFILE_ACTION_TYPES:
        return None
    return _optional_text(action_data.get("profile_name")) or _optional_text(
        action_data.get("target")
    )


def _first_value(
    action_data: dict[str, object],
    *keys: str,
    default: object = None,
) -> object:
    for key in keys:
        if key in action_data:
            return action_data[key]
    return default


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _optional_string_list(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return _string_list(value)


def _text(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _optional_text(value: object) -> str | None:
    text = _text(value).strip()
    return text or None
