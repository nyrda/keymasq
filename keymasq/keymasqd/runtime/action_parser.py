"""Runtime action payload parsing and normalization."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from keymasq.common.coercion import coerce_float, coerce_int, coerce_str
from keymasq.common.gamepad_axes import gamepad_axis_max_value
from keymasq.common.model import superkeys
from keymasq.common.model.actions import (
    DEFAULT_NATURAL_MOUSE_MOVE_JITTER,
    DEFAULT_NATURAL_MOUSE_MOVE_MAX_DURATION_MS,
    DEFAULT_NATURAL_MOUSE_MOVE_SPEED,
    DEFAULT_NATURAL_MOUSE_MOVE_TOLERANCE,
    MappingAction,
    ProfileDeactivationPolicy,
    normalize_macro_loop_stop_behavior,
    normalize_macro_recording_slot,
    normalize_mpris_command,
    normalize_natural_mouse_move_curve,
    normalize_profile_deactivation_policy,
    parse_profile_deactivation_policy,
    parse_rapidfire_fields,
)
from keymasq.common.model.analog import (
    SAME_DEVICE_OUTPUT_ID,
    AnalogActionThreshold,
    AnalogControlConfig,
    AnalogGamepadOutputConfig,
    AnalogMouseMotionConfig,
    normalize_analog_control_features,
)
from keymasq.common.model.core import ActionType, SuperkeyMode
from keymasq.common.model.motion import (
    MotionAxisRoutingConfig,
    MotionControlConfig,
    MotionGamepadConfig,
    MotionMouseConfig,
    MotionTiltConfig,
)
from keymasq.common.types import JsonObject
from keymasq.keymasqd import superkey_state

log = logging.getLogger("keymasqd.runtime.action_parser")


@dataclass(frozen=True)
class _ParsedActionFields:
    target: str | None
    output_id: str | None
    cmd: str | None
    exec_ref: int | None
    macro_name: str | None
    macro_replay_mouse_movement: bool
    macro_replay_mouse_clicks: bool
    macro_speed: float
    macro_loop_mode: str
    macro_loop_count: int
    macro_loop_stop_behavior: str
    macro_move_to_start: bool
    macro_start_x: int
    macro_start_y: int
    macro_block_mouse_movement: bool
    macro_recording_slot: int
    profile_name: str | None
    profile_deactivation: ProfileDeactivationPolicy | None
    compositor_id: str | None
    compositor_dispatcher: str | None
    compositor_args: str | None
    mpris_command: str | None
    move_x: int
    move_y: int
    move_speed: float
    move_jitter: float
    move_curve: str
    move_tolerance: int
    move_max_duration_ms: int
    axis_value: int
    rapidfire_enabled: bool
    rapidfire_hold_ms: int
    rapidfire_wait_ms: int


def _parse_shared_action_fields(
    action_data: JsonObject,
    action_type: ActionType,
    *,
    rapidfire_context: str,
) -> _ParsedActionFields:
    target = action_data.get("target")
    axis_value = 0
    if action_type == ActionType.GAMEPAD_AXIS:
        axis_value = coerce_int(action_data.get("value"), gamepad_axis_max_value(target))
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
    )
    if unsupported_rapidfire:
        log.warning(
            "Ignoring rapidfire for unsupported %s action in %s",
            action_type.value,
            rapidfire_context,
        )

    mpris_command = (
        normalize_mpris_command(action_data.get("command"))
        if action_type == ActionType.MPRIS
        else None
    )

    return _ParsedActionFields(
        target=coerce_str(target, None),
        output_id=coerce_str(action_data.get("output_id"), None),
        cmd=coerce_str(action_data.get("cmd"), None),
        exec_ref=coerce_int(action_data.get("exec_ref"), None),
        macro_name=coerce_str(action_data.get("macro_name"), None),
        macro_replay_mouse_movement=bool(action_data.get("macro_replay_mouse_movement", True)),
        macro_replay_mouse_clicks=bool(action_data.get("macro_replay_mouse_clicks", True)),
        macro_speed=coerce_float(action_data.get("macro_speed"), 1.0),
        macro_loop_mode=coerce_str(action_data.get("macro_loop_mode"), "none") or "none",
        macro_loop_count=coerce_int(action_data.get("macro_loop_count"), 1),
        macro_loop_stop_behavior=normalize_macro_loop_stop_behavior(
            action_data.get("macro_loop_stop_behavior")
        ),
        macro_move_to_start=bool(action_data.get("macro_move_to_start", False)),
        macro_start_x=coerce_int(action_data.get("macro_start_x"), 0),
        macro_start_y=coerce_int(action_data.get("macro_start_y"), 0),
        macro_block_mouse_movement=bool(action_data.get("macro_block_mouse_movement", False)),
        macro_recording_slot=normalize_macro_recording_slot(
            action_data.get("recording_slot", action_data.get("macro_recording_slot"))
        ),
        profile_name=coerce_str(action_data.get("profile_name"), None),
        profile_deactivation=normalize_profile_deactivation_policy(
            action_type,
            parse_profile_deactivation_policy(action_data.get("deactivation")),
        ),
        compositor_id=coerce_str(action_data.get("compositor"), None),
        compositor_dispatcher=coerce_str(action_data.get("dispatcher"), None),
        compositor_args=coerce_str(action_data.get("args"), None),
        mpris_command=mpris_command,
        move_x=coerce_int(action_data.get("x"), 0),
        move_y=coerce_int(action_data.get("y"), 0),
        move_speed=coerce_float(action_data.get("speed"), DEFAULT_NATURAL_MOUSE_MOVE_SPEED),
        move_jitter=coerce_float(action_data.get("jitter"), DEFAULT_NATURAL_MOUSE_MOVE_JITTER),
        move_curve=normalize_natural_mouse_move_curve(action_data.get("curve")),
        move_tolerance=coerce_int(
            action_data.get("tolerance"),
            DEFAULT_NATURAL_MOUSE_MOVE_TOLERANCE,
        ),
        move_max_duration_ms=coerce_int(
            action_data.get("max_duration_ms"),
            DEFAULT_NATURAL_MOUSE_MOVE_MAX_DURATION_MS,
        ),
        axis_value=axis_value,
        rapidfire_enabled=rapidfire_enabled,
        rapidfire_hold_ms=rapidfire_hold_ms,
        rapidfire_wait_ms=rapidfire_wait_ms,
    )


def parse_action(
    manager: object,
    action_data: JsonObject | str,
) -> MappingAction:
    if isinstance(action_data, str):
        return MappingAction(action_type=ActionType.KEYBOARD, target=action_data)

    action_type_str = coerce_str(action_data.get("action"), "passthrough")
    action_type = ActionType(action_type_str)

    superkey_config = None
    if action_type == ActionType.SUPERKEY and "superkey" in action_data:
        superkey_config = parse_superkey_config(
            manager,
            action_data["superkey"],
            json_object=getattr(manager, "_json_object", None),
        )
    analog_control_config = None
    analog_control_configs: list[AnalogControlConfig] = []
    if action_type == ActionType.ANALOG_CONTROL and "analog_control" in action_data:
        analog_control_config = parse_analog_control_config(
            manager,
            action_data["analog_control"],
            json_object=getattr(manager, "_json_object", None),
        )
        analog_control_configs = [analog_control_config]
    elif action_type == ActionType.ANALOG_CONTROL and isinstance(
        action_data.get("analog_controls"),
        list,
    ):
        for raw_config in cast(list[object], action_data["analog_controls"]):
            analog_control_configs.append(
                parse_analog_control_config(
                    manager,
                    raw_config,
                    json_object=getattr(manager, "_json_object", None),
                )
            )
        analog_control_config = analog_control_configs[0] if analog_control_configs else None

    motion_control_config = None
    if action_type == ActionType.MOTION_CONTROL and "motion_control" in action_data:
        motion_control_config = parse_motion_control_config(action_data["motion_control"])

    shared = _parse_shared_action_fields(
        action_data,
        action_type,
        rapidfire_context="runtime payload",
    )

    return MappingAction(
        action_type=action_type,
        source_profile_name=coerce_str(action_data.get("source_profile_name"), None),
        target=shared.target,
        output_id=shared.output_id,
        keys=cast(list[str] | None, action_data.get("keys")),
        cmd=shared.cmd,
        exec_ref=shared.exec_ref,
        superkey_config=cast(superkeys.SuperkeyConfig | None, superkey_config),
        analog_control_name=coerce_str(action_data.get("analog_control_name"), None),
        analog_control_names=cast(list[str], action_data.get("analog_control_names") or []),
        analog_control_config=analog_control_config,
        analog_control_configs=analog_control_configs,
        motion_control_name=coerce_str(action_data.get("motion_control_name"), None),
        motion_control_config=motion_control_config,
        macro_name=shared.macro_name,
        macro_events=cast(list[JsonObject] | None, action_data.get("macro_events")),
        macro_replay_mouse_movement=shared.macro_replay_mouse_movement,
        macro_replay_mouse_clicks=shared.macro_replay_mouse_clicks,
        macro_speed=shared.macro_speed,
        macro_loop_mode=shared.macro_loop_mode,
        macro_loop_count=shared.macro_loop_count,
        macro_loop_stop_behavior=shared.macro_loop_stop_behavior,
        macro_move_to_start=shared.macro_move_to_start,
        macro_start_x=shared.macro_start_x,
        macro_start_y=shared.macro_start_y,
        macro_block_mouse_movement=shared.macro_block_mouse_movement,
        macro_recording_slot=shared.macro_recording_slot,
        profile_name=shared.profile_name,
        profile_deactivation=shared.profile_deactivation,
        compositor_id=shared.compositor_id,
        compositor_dispatcher=shared.compositor_dispatcher,
        compositor_args=shared.compositor_args,
        mpris_command=shared.mpris_command,
        move_x=shared.move_x,
        move_y=shared.move_y,
        axis_value=shared.axis_value,
        move_speed=shared.move_speed,
        move_jitter=shared.move_jitter,
        move_curve=shared.move_curve,
        move_tolerance=shared.move_tolerance,
        move_max_duration_ms=shared.move_max_duration_ms,
        rapidfire_enabled=shared.rapidfire_enabled,
        rapidfire_hold_ms=shared.rapidfire_hold_ms,
        rapidfire_wait_ms=shared.rapidfire_wait_ms,
        tap_enabled=bool(action_data.get("tap_enabled", False)),
        tap_hold_ms=coerce_int(action_data.get("tap_hold_ms"), 10),
        repeat_categories=cast(list[str] | None, action_data.get("repeat_categories")),
    )


def parse_motion_control_config(data: object) -> MotionControlConfig:
    if not isinstance(data, dict):
        raise TypeError("motion control config must be an object")
    config = cast(JsonObject, data)
    raw_mouse = config.get("mouse", {})
    raw_gamepad = config.get("gamepad", {})
    raw_tilt = config.get("tilt", {})
    raw_axis_routing = config.get("axis_routing", {})
    mouse = cast(JsonObject, raw_mouse) if isinstance(raw_mouse, dict) else {}
    gamepad = cast(JsonObject, raw_gamepad) if isinstance(raw_gamepad, dict) else {}
    tilt = cast(JsonObject, raw_tilt) if isinstance(raw_tilt, dict) else {}
    axis_routing = cast(JsonObject, raw_axis_routing) if isinstance(raw_axis_routing, dict) else {}
    return MotionControlConfig(
        name=coerce_str(config.get("name"), "Motion Control"),
        mode=coerce_str(config.get("mode"), "mouse"),
        axis_routing=MotionAxisRoutingConfig(
            yaw=coerce_str(axis_routing.get("yaw"), "horizontal"),
            pitch=coerce_str(axis_routing.get("pitch"), "vertical"),
            roll=coerce_str(axis_routing.get("roll"), "horizontal"),
        ),
        mouse=MotionMouseConfig(
            sensitivity_x=coerce_float(mouse.get("sensitivity_x"), 8.0),
            sensitivity_y=coerce_float(mouse.get("sensitivity_y"), 8.0),
            deadzone_dps=coerce_float(mouse.get("deadzone_dps"), 0.5),
            smoothing=coerce_float(mouse.get("smoothing"), 0.15),
            response_curve=coerce_float(mouse.get("response_curve"), 1.0),
            invert_x=bool(mouse.get("invert_x", False)),
            invert_y=bool(mouse.get("invert_y", False)),
        ),
        gamepad=MotionGamepadConfig(
            output_id=coerce_str(gamepad.get("output_id"), SAME_DEVICE_OUTPUT_ID),
            target=coerce_str(gamepad.get("target"), "right"),
            target_analog_id=coerce_str(gamepad.get("target_analog_id"), None),
            max_rate_dps=coerce_float(gamepad.get("max_rate_dps"), 360.0),
            deadzone_dps=coerce_float(gamepad.get("deadzone_dps"), 1.0),
            smoothing=coerce_float(gamepad.get("smoothing"), 0.15),
            response_curve=coerce_float(gamepad.get("response_curve"), 1.0),
            invert_x=bool(gamepad.get("invert_x", False)),
            invert_y=bool(gamepad.get("invert_y", False)),
        ),
        tilt=MotionTiltConfig(
            reference=coerce_str(tilt.get("reference"), "activation"),
            pitch=coerce_str(tilt.get("pitch"), "vertical"),
            roll=coerce_str(tilt.get("roll"), "horizontal"),
            deadzone_deg=coerce_float(tilt.get("deadzone_deg"), 2.0),
            full_scale_deg=coerce_float(tilt.get("full_scale_deg"), 30.0),
            smoothing=coerce_float(tilt.get("smoothing"), 0.8),
            response_curve=coerce_float(tilt.get("response_curve"), 1.0),
            invert_x=bool(tilt.get("invert_x", False)),
            invert_y=bool(tilt.get("invert_y", False)),
            speed_x=coerce_float(tilt.get("speed_x"), 900.0),
            speed_y=coerce_float(tilt.get("speed_y"), 900.0),
            area_radius_x=coerce_float(tilt.get("area_radius_x"), 400.0),
            area_radius_y=coerce_float(tilt.get("area_radius_y"), 400.0),
            drag_center=bool(tilt.get("drag_center", True)),
        ),
    )


def parse_analog_control_config(
    manager: object,
    data: object,
    *,
    json_object: Callable[[object], JsonObject | None] | None,
) -> AnalogControlConfig:
    if json_object is not None:
        config = json_object(data)
    else:
        config = cast(JsonObject | None, data if isinstance(data, dict) else None)
    if config is None:
        raise TypeError("analog control config must be an object")

    mouse_data = config.get("mouse_motion")
    mouse_config = json_object(mouse_data) if json_object is not None else None
    if mouse_config is None and isinstance(mouse_data, dict):
        mouse_config = cast(JsonObject, mouse_data)
    mouse_config_data = mouse_config or {}
    mouse = AnalogMouseMotionConfig(
        enabled=bool(mouse_config_data.get("enabled", False)),
        mode=coerce_str(mouse_config_data.get("mode"), "velocity") or "velocity",
        speed=coerce_float(mouse_config_data.get("speed"), 900.0),
        speed_x=(
            coerce_float(mouse_config_data.get("speed_x"), 900.0)
            if "speed_x" in mouse_config_data
            else None
        ),
        speed_y=(
            coerce_float(mouse_config_data.get("speed_y"), 900.0)
            if "speed_y" in mouse_config_data
            else None
        ),
        area_radius_x=coerce_float(mouse_config_data.get("area_radius_x"), 400.0),
        area_radius_y=coerce_float(mouse_config_data.get("area_radius_y"), 400.0),
        area_start_enabled=bool(mouse_config_data.get("area_start_enabled", False)),
        area_start_x=coerce_int(mouse_config_data.get("area_start_x"), 0),
        area_start_y=coerce_int(mouse_config_data.get("area_start_y"), 0),
        deadzone=coerce_float(mouse_config_data.get("deadzone"), 0.15),
        sensitivity=coerce_float(mouse_config_data.get("sensitivity"), 1.0),
        response_curve=coerce_float(mouse_config_data.get("response_curve"), 1.0),
        direction=coerce_str(mouse_config_data.get("direction"), "right") or "right",
        invert_x=bool(mouse_config_data.get("invert_x", False)),
        invert_y=bool(mouse_config_data.get("invert_y", False)),
        tick_ms=coerce_int(mouse_config_data.get("tick_ms"), 8),
    )

    gamepad_data = config.get("gamepad_output")
    gamepad_config = json_object(gamepad_data) if json_object is not None else None
    if gamepad_config is None and isinstance(gamepad_data, dict):
        gamepad_config = cast(JsonObject, gamepad_data)
    gamepad_output = AnalogGamepadOutputConfig(
        enabled=bool((gamepad_config or {}).get("enabled", False)),
        output_id=coerce_str((gamepad_config or {}).get("output_id"), None),
        deadzone=coerce_float((gamepad_config or {}).get("deadzone"), 0.0),
        target=coerce_str((gamepad_config or {}).get("target"), "same") or "same",
        target_analog_id=coerce_str((gamepad_config or {}).get("target_analog_id"), None),
        output_rest=coerce_int(
            (gamepad_config or {}).get("output_rest"),
            None,
        ),
        output_direction=coerce_str((gamepad_config or {}).get("output_direction"), ""),
        output_invert=bool((gamepad_config or {}).get("output_invert", False)),
        output_invert_x=bool((gamepad_config or {}).get("output_invert_x", False)),
        output_invert_y=bool((gamepad_config or {}).get("output_invert_y", False)),
        sensitivity=coerce_float((gamepad_config or {}).get("sensitivity"), 1.0),
        response_curve=coerce_float((gamepad_config or {}).get("response_curve"), 1.0),
    )

    thresholds: list[AnalogActionThreshold] = []
    raw_thresholds = config.get("thresholds")
    if isinstance(raw_thresholds, list):
        for raw_threshold in cast(list[object], raw_thresholds):
            threshold = json_object(raw_threshold) if json_object is not None else None
            if threshold is None and isinstance(raw_threshold, dict):
                threshold = cast(JsonObject, raw_threshold)
            if threshold is None:
                continue
            actions: list[MappingAction] = []
            raw_actions = threshold.get("actions")
            if isinstance(raw_actions, list):
                for raw_action in cast(list[object], raw_actions):
                    child = json_object(raw_action) if json_object is not None else None
                    if child is None and isinstance(raw_action, dict):
                        child = cast(JsonObject, raw_action)
                    if child is None:
                        continue
                    if _is_nested_analog_control_action(child):
                        continue
                    parsed = parse_action(
                        manager,
                        child,
                    )
                    if parsed.action_type == ActionType.ANALOG_CONTROL:
                        continue
                    actions.append(parsed)
            thresholds.append(
                AnalogActionThreshold(
                    axis=coerce_str(threshold.get("axis"), ""),
                    trigger_min=coerce_float(threshold.get("trigger_min"), 0.0),
                    trigger_max=coerce_float(threshold.get("trigger_max"), 0.0),
                    release_min=coerce_float(threshold.get("release_min"), 0.0),
                    release_max=coerce_float(threshold.get("release_max"), 0.0),
                    actions=actions,
                )
            )

    return normalize_analog_control_features(
        AnalogControlConfig(
            name=coerce_str(config.get("name"), ""),
            description=coerce_str(config.get("description"), None),
            input_type=coerce_str(config.get("input_type"), "stick") or "stick",
            mouse_motion=mouse,
            gamepad_output=gamepad_output,
            thresholds=thresholds,
        )
    )


def _is_nested_analog_control_action(action_data: JsonObject) -> bool:
    return (
        action_data.get("action") == ActionType.ANALOG_CONTROL.value
        or action_data.get("action_type") == ActionType.ANALOG_CONTROL.value
    )


def parse_superkey_config(
    manager: object,
    data: object,
    *,
    json_object: Callable[[object], JsonObject | None] | None,
) -> superkey_state.SuperkeyConfig:
    if json_object is not None:
        config = json_object(data)
    else:
        config = cast(JsonObject | None, data if isinstance(data, dict) else None)
    if config is None:
        raise TypeError("superkey config must be an object")

    overload_actions = parse_overload_action_bundle(
        manager,
        config.get("overload_actions"),
        json_object=json_object,
    )
    overload_down_actions = parse_overload_action_bundle(
        manager,
        config.get("overload_down_actions"),
        json_object=json_object,
    )
    overload_up_actions = parse_overload_action_bundle(
        manager,
        config.get("overload_up_actions"),
        json_object=json_object,
    )
    mode_value = config.get("mode")
    if not isinstance(mode_value, str):
        raise TypeError("superkey config must include a mode")
    mode = SuperkeyMode(mode_value)
    if mode == SuperkeyMode.OVERLOAD:
        if any(
            config.get(key)
            for key in ("tap_actions", "double_tap_actions", "hold_actions", "tap_hold_actions")
        ):
            raise ValueError("overload superkeys cannot define pattern slots")
        tap_actions = []
        double_tap_actions = []
        hold_actions = []
        tap_hold_actions = []
    else:
        if overload_actions or overload_down_actions or overload_up_actions:
            raise ValueError("pattern superkeys cannot define overload actions")
        tap_actions = parse_superkey_action_bundle(
            manager,
            config.get("tap_actions"),
            json_object=json_object,
        )
        double_tap_actions = parse_superkey_action_bundle(
            manager,
            config.get("double_tap_actions"),
            json_object=json_object,
        )
        hold_actions = parse_superkey_action_bundle(
            manager,
            config.get("hold_actions"),
            json_object=json_object,
        )
        tap_hold_actions = parse_superkey_action_bundle(
            manager,
            config.get("tap_hold_actions"),
            json_object=json_object,
        )
    return superkey_state.SuperkeyConfig(
        name=coerce_str(config.get("name"), ""),
        mode=mode,
        tap_timeout_ms=coerce_int(config.get("tap_timeout_ms"), 200),
        double_tap_window_ms=coerce_int(config.get("double_tap_window_ms"), 300),
        hold_threshold_ms=coerce_int(config.get("hold_threshold_ms"), 300),
        tap_actions=tap_actions,
        double_tap_actions=double_tap_actions,
        hold_actions=hold_actions,
        tap_hold_actions=tap_hold_actions,
        overload_actions=overload_actions,
        overload_down_actions=overload_down_actions,
        overload_up_actions=overload_up_actions,
    )


def parse_superkey_action_bundle(
    manager: object,
    data: object | None,
    *,
    json_object: Callable[[object], JsonObject | None] | None,
) -> list[superkey_state.SuperkeyActionData]:
    if data is None:
        return []
    if not isinstance(data, list):
        raise TypeError("superkey action bundle must be a list")

    actions: list[superkey_state.SuperkeyActionData] = []
    for item in cast(list[object], data):
        parsed = parse_superkey_action(
            manager,
            item,
            json_object=json_object,
        )
        if parsed is not None:
            actions.append(parsed)
    return actions


def parse_overload_action_bundle(
    manager: object,
    data: object | None,
    *,
    json_object: Callable[[object], JsonObject | None] | None,
) -> list[MappingAction]:
    if data is None:
        return []

    actions: list[MappingAction] = []
    if not isinstance(data, list):
        raise TypeError("overload action bundle must be a list")
    for item in cast(list[object], data):
        payload = json_object(item) if json_object is not None else cast(JsonObject | None, item)
        if payload is None:
            raise TypeError("overload action must be an object")
        action_type = coerce_str(payload.get("action"), "passthrough")
        if action_type == "superkey":
            raise ValueError("nested superkeys are not allowed inside superkeys")
        if action_type == "repeat":
            raise ValueError("repeat is not allowed inside overload superkeys")
        actions.append(
            parse_action(
                manager,
                payload,
            )
        )
    return actions


def parse_superkey_action(
    _manager: object,
    data: object | None,
    *,
    json_object: Callable[[object], JsonObject | None] | None,
) -> superkey_state.SuperkeyActionData | None:
    if data is None:
        return None
    if json_object is not None:
        action = json_object(data)
    else:
        action = cast(JsonObject | None, data if isinstance(data, dict) else None)
    if action is None:
        raise TypeError("superkey action must be an object")

    action_type = ActionType(coerce_str(action.get("action"), "keyboard"))
    if action_type == ActionType.SUPERKEY:
        raise ValueError("nested superkeys are not allowed inside superkeys")
    shared = _parse_shared_action_fields(
        action,
        action_type,
        rapidfire_context="superkey runtime payload",
    )

    return superkey_state.SuperkeyActionData(
        action_type=action_type.value,
        target=shared.target,
        output_id=shared.output_id,
        cmd=shared.cmd,
        exec_ref=shared.exec_ref,
        macro_name=shared.macro_name,
        macro_replay_mouse_movement=shared.macro_replay_mouse_movement,
        macro_replay_mouse_clicks=shared.macro_replay_mouse_clicks,
        macro_speed=shared.macro_speed,
        macro_loop_mode=shared.macro_loop_mode,
        macro_loop_count=shared.macro_loop_count,
        macro_loop_stop_behavior=shared.macro_loop_stop_behavior,
        macro_move_to_start=shared.macro_move_to_start,
        macro_start_x=shared.macro_start_x,
        macro_start_y=shared.macro_start_y,
        macro_block_mouse_movement=shared.macro_block_mouse_movement,
        macro_recording_slot=shared.macro_recording_slot,
        profile_name=shared.profile_name,
        profile_deactivation=shared.profile_deactivation,
        compositor_id=shared.compositor_id,
        compositor_dispatcher=shared.compositor_dispatcher,
        compositor_args=shared.compositor_args,
        mpris_command=shared.mpris_command,
        move_x=shared.move_x,
        move_y=shared.move_y,
        move_speed=shared.move_speed,
        move_jitter=shared.move_jitter,
        move_curve=shared.move_curve,
        move_tolerance=shared.move_tolerance,
        move_max_duration_ms=shared.move_max_duration_ms,
        axis_value=shared.axis_value,
        rapidfire_enabled=shared.rapidfire_enabled,
        rapidfire_hold_ms=shared.rapidfire_hold_ms,
        rapidfire_wait_ms=shared.rapidfire_wait_ms,
    )
