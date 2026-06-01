import logging
from typing import Literal, cast

from keymasq.common.gamepad_axes import gamepad_axis_max_value
from keymasq.common.models import (
    ActionType,
    MappingAction,
    normalize_macro_loop_stop_behavior,
    normalize_macro_recording_slot,
    normalize_profile_deactivation_policy,
    parse_profile_deactivation_policy,
    parse_rapidfire_fields,
    profile_deactivation_policy_to_dict,
    resolve_rapidfire_fields,
)

type TomlDict = dict[str, object]
type _IntLike = int | float | str | bytes
type _FloatLike = int | float | str | bytes
type UnknownActionPolicy = Literal["raise", "passthrough"]

MACRO_RECORDING_SLOT_ACTION_TYPES: tuple[ActionType, ...] = (
    ActionType.START_MACRO_RECORDING,
    ActionType.STOP_MACRO_RECORDING,
    ActionType.PLAY_MACRO_SLOT,
)
MACRO_CONTROL_ACTION_TYPES: tuple[ActionType, ...] = (
    *MACRO_RECORDING_SLOT_ACTION_TYPES,
    ActionType.CANCEL_MACRO_PLAYBACK,
    ActionType.EMERGENCY_RESET,
)
PROFILE_REF_ACTION_TYPES: tuple[ActionType, ...] = (
    ActionType.PROFILE_ENABLE,
    ActionType.PROFILE_DISABLE,
    ActionType.PROFILE_TOGGLE,
)


class UnknownActionTypeError(ValueError):
    pass


def _int_value(value: object, default: int) -> int:
    return default if value is None else int(cast(_IntLike, value))


def _float_value(value: object, default: float) -> float:
    return default if value is None else float(cast(_FloatLike, value))


def mapping_action_type_from_toml(
    action_data: TomlDict,
    *,
    unknown_action: UnknownActionPolicy,
    logger: logging.Logger | None = None,
) -> tuple[ActionType, TomlDict]:
    action_type_str = str(action_data.get("action", "passthrough"))
    normalized_action_data = action_data
    if action_type_str == "rapidfire":
        action_type_str = "keyboard"
        normalized_action_data = dict(action_data)
        normalized_action_data["rapidfire_enabled"] = True
        normalized_action_data["action"] = "keyboard"

    try:
        return ActionType(action_type_str), normalized_action_data
    except ValueError as exc:
        if unknown_action == "raise":
            raise UnknownActionTypeError(f"unknown action type '{action_type_str}'") from exc
        if logger is not None:
            logger.warning(
                "Unknown action type '%s', defaulting to passthrough",
                action_type_str,
            )
        return ActionType.PASSTHROUGH, normalized_action_data


def _rapidfire_from_toml(
    action_data: TomlDict,
    action_type: ActionType,
    *,
    logger: logging.Logger | None,
    warning_context: str,
) -> tuple[bool, int, int]:
    (
        rapidfire_enabled,
        rapidfire_hold_ms,
        rapidfire_wait_ms,
        unsupported_rapidfire,
    ) = parse_rapidfire_fields(
        action_type,
        rapidfire_enabled=action_data.get("rapidfire_enabled", False),
        rapidfire_hold_ms=action_data.get("rapidfire_hold_ms"),
        rapidfire_wait_ms=action_data.get("rapidfire_wait_ms"),
        int_value=_int_value,
    )
    if unsupported_rapidfire and logger is not None:
        logger.warning(
            "Ignoring rapidfire for unsupported %s action in %s",
            action_type.value,
            warning_context,
        )
    return rapidfire_enabled, rapidfire_hold_ms, rapidfire_wait_ms


def mapping_action_from_toml(
    action_data: TomlDict,
    action_type: ActionType,
    *,
    logger: logging.Logger | None = None,
    rapidfire_warning_context: str,
    preparse_rapidfire_for_special_actions: bool = False,
) -> MappingAction:
    rapidfire_fields: tuple[bool, int, int] | None = None
    if preparse_rapidfire_for_special_actions:
        rapidfire_fields = _rapidfire_from_toml(
            action_data,
            action_type,
            logger=logger,
            warning_context=rapidfire_warning_context,
        )

    if action_type == ActionType.MACRO:
        return MappingAction(
            action_type=ActionType.MACRO,
            macro_name=str(action_data.get("target", "") or "")
            or str(action_data.get("macro_name", "") or ""),
            macro_replay_mouse_movement=bool(action_data.get("replay_mouse_movement", True)),
            macro_replay_mouse_clicks=bool(action_data.get("replay_mouse_clicks", True)),
            macro_speed=_float_value(action_data.get("speed"), 1.0),
            macro_loop_mode=str(action_data.get("loop_mode", "none") or "none"),
            macro_loop_count=_int_value(action_data.get("loop_count"), 1),
            macro_loop_stop_behavior=normalize_macro_loop_stop_behavior(
                action_data.get("loop_stop_behavior")
            ),
            macro_move_to_start=bool(action_data.get("move_to_start", False)),
            macro_start_x=_int_value(action_data.get("start_x"), 0),
            macro_start_y=_int_value(action_data.get("start_y"), 0),
            macro_block_mouse_movement=bool(action_data.get("block_mouse_movement", False)),
        )

    if action_type in MACRO_CONTROL_ACTION_TYPES:
        if action_type in MACRO_RECORDING_SLOT_ACTION_TYPES:
            return MappingAction(
                action_type=action_type,
                macro_recording_slot=normalize_macro_recording_slot(
                    action_data.get("recording_slot", action_data.get("slot"))
                ),
            )
        return MappingAction(action_type=action_type)

    if action_type in PROFILE_REF_ACTION_TYPES:
        deactivation = normalize_profile_deactivation_policy(
            action_type,
            parse_profile_deactivation_policy(action_data.get("deactivation")),
        )
        return MappingAction(
            action_type=action_type,
            profile_name=str(
                action_data.get("profile_name", "") or action_data.get("target", "") or ""
            ),
            profile_deactivation=deactivation,
        )

    if action_type == ActionType.COMPOSITOR_DISPATCH:
        return MappingAction(
            action_type=action_type,
            compositor_id=str(action_data.get("compositor", "") or "") or None,
            compositor_dispatcher=str(action_data.get("dispatcher", "") or ""),
            compositor_args=str(action_data.get("args", "") or ""),
        )

    if rapidfire_fields is None:
        rapidfire_fields = _rapidfire_from_toml(
            action_data,
            action_type,
            logger=logger,
            warning_context=rapidfire_warning_context,
        )
    rapidfire_enabled, rapidfire_hold_ms, rapidfire_wait_ms = rapidfire_fields

    if action_type == ActionType.REPEAT:
        return MappingAction(
            action_type=action_type,
            repeat_categories=cast(list[str] | None, action_data.get("repeat_categories")),
            rapidfire_enabled=rapidfire_enabled,
            rapidfire_hold_ms=rapidfire_hold_ms,
            rapidfire_wait_ms=rapidfire_wait_ms,
        )

    if action_type in (ActionType.MOUSE_MOVE_REL, ActionType.MOUSE_MOVE_ABS):
        return MappingAction(
            action_type=action_type,
            move_x=_int_value(action_data.get("x"), 0),
            move_y=_int_value(action_data.get("y"), 0),
            rapidfire_enabled=rapidfire_enabled,
            rapidfire_hold_ms=rapidfire_hold_ms,
            rapidfire_wait_ms=rapidfire_wait_ms,
            tap_enabled=bool(action_data.get("tap_enabled", False)),
            tap_hold_ms=_int_value(action_data.get("tap_hold_ms"), 10),
        )

    target = action_data.get("target")
    cmd = action_data.get("cmd")
    axis_value = 0
    if action_type == ActionType.GAMEPAD_AXIS:
        axis_value = _int_value(
            action_data.get("value"),
            gamepad_axis_max_value(target),
        )
    return MappingAction(
        action_type=action_type,
        target=str(target) if target is not None else None,
        output_id=str(action_data.get("output_id", "") or "") or None,
        keys=cast(list[str] | None, action_data.get("keys")),
        cmd=str(cmd) if cmd is not None else None,
        axis_value=axis_value,
        rapidfire_enabled=rapidfire_enabled,
        rapidfire_hold_ms=rapidfire_hold_ms,
        rapidfire_wait_ms=rapidfire_wait_ms,
        tap_enabled=bool(action_data.get("tap_enabled", False)),
        tap_hold_ms=_int_value(action_data.get("tap_hold_ms"), 10),
    )


def mapping_action_to_toml(
    action: MappingAction,
    *,
    include_profile_refs: bool = False,
    logger: logging.Logger | None = None,
    rapidfire_warning_context: str,
) -> TomlDict:
    action_data: dict[str, object] = {"action": action.action_type.value}
    if action.target:
        action_data["target"] = action.target
    if action.action_type in (ActionType.GAMEPAD, ActionType.GAMEPAD_AXIS) and action.output_id:
        action_data["output_id"] = action.output_id
    if action.keys:
        action_data["keys"] = action.keys
    if action.cmd:
        action_data["cmd"] = action.cmd
    if include_profile_refs and action.superkey_name:
        action_data["superkey_name"] = action.superkey_name
    if (
        include_profile_refs
        and action.action_type == ActionType.ANALOG_CONTROL
        and action.analog_control_names
    ):
        if len(action.analog_control_names) == 1:
            action_data["analog_control_name"] = action.analog_control_names[0]
        else:
            action_data["analog_control_names"] = action.analog_control_names
    if action.action_type == ActionType.MACRO:
        action_data["target"] = action.macro_name or ""
        action_data["macro_name"] = action.macro_name or ""
        action_data["replay_mouse_movement"] = action.macro_replay_mouse_movement
        action_data["replay_mouse_clicks"] = action.macro_replay_mouse_clicks
        action_data["speed"] = action.macro_speed
        action_data["loop_mode"] = action.macro_loop_mode
        action_data["loop_count"] = int(action.macro_loop_count)
        action_data["loop_stop_behavior"] = action.macro_loop_stop_behavior
        action_data["move_to_start"] = bool(action.macro_move_to_start)
        action_data["start_x"] = int(action.macro_start_x)
        action_data["start_y"] = int(action.macro_start_y)
        action_data["block_mouse_movement"] = bool(action.macro_block_mouse_movement)
    if (
        action.action_type in MACRO_RECORDING_SLOT_ACTION_TYPES
        and action.macro_recording_slot
    ):
        action_data["recording_slot"] = int(action.macro_recording_slot)
    if action.action_type in (ActionType.MOUSE_MOVE_REL, ActionType.MOUSE_MOVE_ABS):
        action_data["x"] = int(action.move_x)
        action_data["y"] = int(action.move_y)
    if action.action_type == ActionType.GAMEPAD_AXIS:
        action_data["value"] = int(action.axis_value)
    if action.action_type in PROFILE_REF_ACTION_TYPES:
        action_data["target"] = action.profile_name or ""
        action_data["profile_name"] = action.profile_name or ""
        deactivation = normalize_profile_deactivation_policy(
            action.action_type,
            action.profile_deactivation,
        )
        deactivation_data = profile_deactivation_policy_to_dict(deactivation)
        if deactivation_data is not None:
            action_data["deactivation"] = deactivation_data
    if action.action_type == ActionType.COMPOSITOR_DISPATCH:
        if action.compositor_id:
            action_data["compositor"] = action.compositor_id
        action_data["dispatcher"] = action.compositor_dispatcher or ""
        action_data["args"] = action.compositor_args or ""
    if action.action_type == ActionType.REPEAT:
        action_data["repeat_categories"] = list(action.repeat_categories or [])
    (
        rapidfire_enabled,
        rapidfire_hold_ms,
        rapidfire_wait_ms,
        unsupported_rapidfire,
    ) = resolve_rapidfire_fields(
        action.action_type,
        rapidfire_enabled=bool(action.rapidfire_enabled),
        rapidfire_hold_ms=int(action.rapidfire_hold_ms),
        rapidfire_wait_ms=int(action.rapidfire_wait_ms),
    )
    if unsupported_rapidfire and logger is not None:
        logger.warning(
            "Dropping rapidfire for unsupported %s action while saving %s",
            action.action_type.value,
            rapidfire_warning_context,
        )
    if rapidfire_enabled:
        action_data["rapidfire_enabled"] = True
        action_data["rapidfire_hold_ms"] = rapidfire_hold_ms
        action_data["rapidfire_wait_ms"] = rapidfire_wait_ms
    if action.tap_enabled:
        action_data["tap_enabled"] = True
        action_data["tap_hold_ms"] = int(action.tap_hold_ms)
    return action_data
