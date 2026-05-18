import logging
from collections.abc import Callable
from typing import cast

from keymasq.common.gamepad_axes import gamepad_axis_max_value
from keymasq.common.models import (
    ActionType,
    AnalogActionThreshold,
    AnalogControlConfig,
    AnalogGamepadOutputConfig,
    AnalogMouseMotionConfig,
    MappingAction,
    SuperkeyMode,
    normalize_analog_control_features,
    normalize_macro_loop_stop_behavior,
    parse_rapidfire_fields,
)
from keymasq.common.models import (
    SuperkeyConfig as CommonSuperkeyConfig,
)
from keymasq.keymasqd.superkey_state import SuperkeyActionData, SuperkeyConfig

type JsonObject = dict[str, object]

log = logging.getLogger("keymasqd.runtime.actions")


def _default_optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _default_int_or_none(value: object, *, int_value: Callable[..., int]) -> int | None:
    return None if value is None else int_value(value)


def parse_action(
    manager: object,
    action_data: JsonObject | str,
    *,
    str_value: Callable[..., str],
    optional_str: Callable[..., str | None],
    int_value: Callable[..., int],
    int_or_none: Callable[..., int | None],
    float_value: Callable[..., float],
) -> MappingAction:
    if isinstance(action_data, str):
        return MappingAction(action_type=ActionType.KEYBOARD, target=action_data)

    action_type_str = str_value(action_data.get("action"), "passthrough")
    action_type = ActionType(action_type_str)

    superkey_config = None
    if action_type == ActionType.SUPERKEY and "superkey" in action_data:
        superkey_config = parse_superkey_config(
            manager,
            action_data["superkey"],
            json_object=getattr(manager, "_json_object", None),
            str_value=str_value,
            optional_str=optional_str,
            int_value=int_value,
            int_or_none=int_or_none,
            float_value=float_value,
            parse_superkey_action=parse_superkey_action,
        )
    analog_control_config = None
    analog_control_configs: list[AnalogControlConfig] = []
    if action_type == ActionType.ANALOG_CONTROL and "analog_control" in action_data:
        analog_control_config = parse_analog_control_config(
            manager,
            action_data["analog_control"],
            json_object=getattr(manager, "_json_object", None),
            str_value=str_value,
            optional_str=optional_str,
            int_value=int_value,
            int_or_none=int_or_none,
            float_value=float_value,
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
                    str_value=str_value,
                    optional_str=optional_str,
                    int_value=int_value,
                    int_or_none=int_or_none,
                    float_value=float_value,
                )
            )
        analog_control_config = analog_control_configs[0] if analog_control_configs else None

    target = action_data.get("target")
    axis_value = 0
    if action_type == ActionType.GAMEPAD_AXIS:
        axis_value = int_value(action_data.get("value"), gamepad_axis_max_value(target))
    cmd = action_data.get("cmd")
    macro_name = action_data.get("macro_name")
    profile_name = action_data.get("profile_name")
    compositor_id = action_data.get("compositor")
    compositor_dispatcher = action_data.get("dispatcher")
    compositor_args = action_data.get("args")
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
        int_value=int_value,
    )
    if unsupported_rapidfire:
        log.warning(
            "Ignoring rapidfire for unsupported %s action in runtime payload",
            action_type.value,
        )

    return MappingAction(
        action_type=action_type,
        target=optional_str(target),
        output_id=optional_str(action_data.get("output_id")),
        keys=cast(list[str] | None, action_data.get("keys")),
        cmd=optional_str(cmd),
        exec_ref=int_or_none(action_data.get("exec_ref")),
        superkey_config=cast(CommonSuperkeyConfig | None, superkey_config),
        analog_control_name=optional_str(action_data.get("analog_control_name")),
        analog_control_names=cast(list[str], action_data.get("analog_control_names") or []),
        analog_control_config=analog_control_config,
        analog_control_configs=analog_control_configs,
        macro_name=optional_str(macro_name),
        macro_events=cast(list[JsonObject] | None, action_data.get("macro_events")),
        macro_replay_mouse_movement=bool(action_data.get("macro_replay_mouse_movement", True)),
        macro_replay_mouse_clicks=bool(action_data.get("macro_replay_mouse_clicks", True)),
        macro_speed=float_value(action_data.get("macro_speed"), 1.0),
        macro_loop_mode=str_value(action_data.get("macro_loop_mode"), "none") or "none",
        macro_loop_count=int_value(action_data.get("macro_loop_count"), 1),
        macro_loop_stop_behavior=normalize_macro_loop_stop_behavior(
            action_data.get("macro_loop_stop_behavior")
        ),
        macro_move_to_start=bool(action_data.get("macro_move_to_start", False)),
        macro_start_x=int_value(action_data.get("macro_start_x"), 0),
        macro_start_y=int_value(action_data.get("macro_start_y"), 0),
        macro_block_mouse_movement=bool(action_data.get("macro_block_mouse_movement", False)),
        profile_name=optional_str(profile_name),
        compositor_id=optional_str(compositor_id),
        compositor_dispatcher=optional_str(compositor_dispatcher),
        compositor_args=optional_str(compositor_args),
        move_x=int_value(action_data.get("x"), 0),
        move_y=int_value(action_data.get("y"), 0),
        axis_value=axis_value,
        move_speed=float_value(action_data.get("speed"), 1.0),
        move_jitter=float_value(action_data.get("jitter"), 0.3),
        rapidfire_enabled=rapidfire_enabled,
        rapidfire_hold_ms=rapidfire_hold_ms,
        rapidfire_wait_ms=rapidfire_wait_ms,
        tap_enabled=bool(action_data.get("tap_enabled", False)),
        tap_hold_ms=int_value(action_data.get("tap_hold_ms"), 10),
    )


def parse_analog_control_config(
    manager: object,
    data: object,
    *,
    json_object: Callable[[object], JsonObject | None] | None,
    str_value: Callable[..., str],
    optional_str: Callable[..., str | None],
    int_value: Callable[..., int],
    int_or_none: Callable[..., int | None],
    float_value: Callable[..., float],
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
        mode=str_value(mouse_config_data.get("mode"), "velocity") or "velocity",
        speed=float_value(mouse_config_data.get("speed"), 900.0),
        speed_x=(
            float_value(mouse_config_data.get("speed_x"), 900.0)
            if "speed_x" in mouse_config_data
            else None
        ),
        speed_y=(
            float_value(mouse_config_data.get("speed_y"), 900.0)
            if "speed_y" in mouse_config_data
            else None
        ),
        area_radius_x=float_value(mouse_config_data.get("area_radius_x"), 400.0),
        area_radius_y=float_value(mouse_config_data.get("area_radius_y"), 400.0),
        area_start_enabled=bool(mouse_config_data.get("area_start_enabled", False)),
        area_start_x=int_value(mouse_config_data.get("area_start_x"), 0),
        area_start_y=int_value(mouse_config_data.get("area_start_y"), 0),
        deadzone=float_value(mouse_config_data.get("deadzone"), 0.15),
        sensitivity=float_value(mouse_config_data.get("sensitivity"), 1.0),
        response_curve=float_value(mouse_config_data.get("response_curve"), 1.0),
        direction=str_value(mouse_config_data.get("direction"), "right") or "right",
        invert_x=bool(mouse_config_data.get("invert_x", False)),
        invert_y=bool(mouse_config_data.get("invert_y", False)),
        tick_ms=int_value(mouse_config_data.get("tick_ms"), 8),
    )

    gamepad_data = config.get("gamepad_output")
    gamepad_config = json_object(gamepad_data) if json_object is not None else None
    if gamepad_config is None and isinstance(gamepad_data, dict):
        gamepad_config = cast(JsonObject, gamepad_data)
    gamepad_output = AnalogGamepadOutputConfig(
        enabled=bool((gamepad_config or {}).get("enabled", False)),
        output_id=optional_str((gamepad_config or {}).get("output_id")),
        deadzone=float_value((gamepad_config or {}).get("deadzone"), 0.0),
        target=str_value((gamepad_config or {}).get("target"), "same") or "same",
        target_analog_id=optional_str((gamepad_config or {}).get("target_analog_id")),
        output_rest=int_or_none((gamepad_config or {}).get("output_rest")),
        output_direction=str_value((gamepad_config or {}).get("output_direction"), ""),
        output_invert=bool((gamepad_config or {}).get("output_invert", False)),
        sensitivity=float_value((gamepad_config or {}).get("sensitivity"), 1.0),
        response_curve=float_value((gamepad_config or {}).get("response_curve"), 1.0),
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
                        str_value=str_value,
                        optional_str=optional_str,
                        int_value=int_value,
                        int_or_none=int_or_none,
                        float_value=float_value,
                    )
                    if parsed.action_type == ActionType.ANALOG_CONTROL:
                        continue
                    actions.append(parsed)
            thresholds.append(
                AnalogActionThreshold(
                    axis=str_value(threshold.get("axis"), ""),
                    trigger_min=float_value(threshold.get("trigger_min"), 0.0),
                    trigger_max=float_value(threshold.get("trigger_max"), 0.0),
                    release_min=float_value(threshold.get("release_min"), 0.0),
                    release_max=float_value(threshold.get("release_max"), 0.0),
                    actions=actions,
                )
            )

    return normalize_analog_control_features(
        AnalogControlConfig(
            name=str_value(config.get("name"), ""),
            description=optional_str(config.get("description")),
            input_type=str_value(config.get("input_type"), "stick") or "stick",
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
    str_value: Callable[..., str],
    optional_str: Callable[..., str | None] | None,
    int_value: Callable[..., int],
    int_or_none: Callable[..., int | None] | None,
    float_value: Callable[..., float] | None,
    parse_superkey_action: Callable[..., SuperkeyActionData | None],
) -> SuperkeyConfig:
    if json_object is not None:
        config = json_object(data)
    else:
        config = cast(JsonObject | None, data if isinstance(data, dict) else None)
    if config is None:
        raise TypeError("superkey config must be an object")
    if optional_str is None:
        optional_str = _default_optional_str
    if int_or_none is None:
        def fallback_int_or_none(value: object) -> int | None:
            return _default_int_or_none(value, int_value=int_value)

        int_or_none = fallback_int_or_none
    if float_value is None:
        def fallback_float_value(value: object, default: float) -> float:
            return default if value is None else float(cast(int | float | str | bytes, value))

        float_value = fallback_float_value

    overload_actions = parse_overload_action_bundle(
        manager,
        config.get("overload_actions"),
        json_object=json_object,
        str_value=str_value,
        optional_str=optional_str,
        int_value=int_value,
        int_or_none=int_or_none,
        float_value=float_value,
    )
    overload_down_actions = parse_overload_action_bundle(
        manager,
        config.get("overload_down_actions"),
        json_object=json_object,
        str_value=str_value,
        optional_str=optional_str,
        int_value=int_value,
        int_or_none=int_or_none,
        float_value=float_value,
    )
    overload_up_actions = parse_overload_action_bundle(
        manager,
        config.get("overload_up_actions"),
        json_object=json_object,
        str_value=str_value,
        optional_str=optional_str,
        int_value=int_value,
        int_or_none=int_or_none,
        float_value=float_value,
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
        if (
            overload_actions
            or overload_down_actions
            or overload_up_actions
        ):
            raise ValueError("pattern superkeys cannot define overload actions")
        tap_actions = parse_superkey_action_bundle(
            manager,
            config.get("tap_actions"),
            json_object=json_object,
            str_value=str_value,
            optional_str=optional_str,
            int_or_none=int_or_none,
            int_value=int_value,
        )
        double_tap_actions = parse_superkey_action_bundle(
            manager,
            config.get("double_tap_actions"),
            json_object=json_object,
            str_value=str_value,
            optional_str=optional_str,
            int_or_none=int_or_none,
            int_value=int_value,
        )
        hold_actions = parse_superkey_action_bundle(
            manager,
            config.get("hold_actions"),
            json_object=json_object,
            str_value=str_value,
            optional_str=optional_str,
            int_or_none=int_or_none,
            int_value=int_value,
        )
        tap_hold_actions = parse_superkey_action_bundle(
            manager,
            config.get("tap_hold_actions"),
            json_object=json_object,
            str_value=str_value,
            optional_str=optional_str,
            int_or_none=int_or_none,
            int_value=int_value,
        )
    return SuperkeyConfig(
        name=str_value(config.get("name"), ""),
        mode=mode,
        tap_timeout_ms=int_value(config.get("tap_timeout_ms"), 200),
        double_tap_window_ms=int_value(config.get("double_tap_window_ms"), 300),
        hold_threshold_ms=int_value(config.get("hold_threshold_ms"), 300),
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
    str_value: Callable[..., str],
    optional_str: Callable[..., str | None] | None,
    int_or_none: Callable[..., int | None] | None,
    int_value: Callable[..., int],
) -> list[SuperkeyActionData]:
    if data is None:
        return []
    if not isinstance(data, list):
        raise TypeError("superkey action bundle must be a list")

    actions: list[SuperkeyActionData] = []
    for item in cast(list[object], data):
        parsed = parse_superkey_action(
            manager,
            item,
            json_object=json_object,
            str_value=str_value,
            optional_str=optional_str,
            int_or_none=int_or_none,
            int_value=int_value,
        )
        if parsed is not None:
            actions.append(parsed)
    return actions


def parse_overload_action_bundle(
    manager: object,
    data: object | None,
    *,
    json_object: Callable[[object], JsonObject | None] | None,
    str_value: Callable[..., str],
    optional_str: Callable[..., str | None] | None,
    int_value: Callable[..., int],
    int_or_none: Callable[..., int | None] | None,
    float_value: Callable[..., float] | None,
) -> list[MappingAction]:
    if data is None:
        return []
    if optional_str is None:
        optional_str = _default_optional_str
    if int_or_none is None:
        def fallback_int_or_none(value: object) -> int | None:
            return _default_int_or_none(value, int_value=int_value)

        int_or_none = fallback_int_or_none
    if float_value is None:
        def fallback_float_value(value: object, default: float) -> float:
            return default if value is None else float(cast(int | float | str | bytes, value))

        float_value = fallback_float_value

    actions: list[MappingAction] = []
    if not isinstance(data, list):
        raise TypeError("overload action bundle must be a list")
    for item in cast(list[object], data):
        payload = json_object(item) if json_object is not None else cast(JsonObject | None, item)
        if payload is None:
            raise TypeError("overload action must be an object")
        if str_value(payload.get("action"), "passthrough") == "superkey":
            raise ValueError("nested superkeys are not allowed inside superkeys")
        actions.append(
            parse_action(
                manager,
                payload,
                str_value=str_value,
                optional_str=optional_str,
                int_value=int_value,
                int_or_none=int_or_none,
                float_value=float_value,
            )
        )
    return actions


def parse_superkey_action(
    _manager: object,
    data: object | None,
    *,
    json_object: Callable[[object], JsonObject | None] | None,
    str_value: Callable[..., str],
    optional_str: Callable[..., str | None] | None,
    int_or_none: Callable[..., int | None] | None,
    int_value: Callable[..., int],
) -> SuperkeyActionData | None:
    if data is None:
        return None
    if json_object is not None:
        action = json_object(data)
    else:
        action = cast(JsonObject | None, data if isinstance(data, dict) else None)
    if action is None:
        raise TypeError("superkey action must be an object")

    if optional_str is None:
        optional_str = _default_optional_str
    if int_or_none is None:
        def fallback_int_or_none(value: object) -> int | None:
            return _default_int_or_none(value, int_value=int_value)

        int_or_none = fallback_int_or_none
    macro_speed_value = action.get("macro_speed")
    action_type = ActionType(str_value(action.get("action"), "keyboard"))
    if action_type == ActionType.SUPERKEY:
        raise ValueError("nested superkeys are not allowed inside superkeys")
    (
        rapidfire_enabled,
        rapidfire_hold_ms,
        rapidfire_wait_ms,
        unsupported_rapidfire,
    ) = parse_rapidfire_fields(
        action_type,
        rapidfire_enabled=action.get("rapidfire_enabled", False),
        rapidfire_hold_ms=action.get("rapidfire_hold_ms"),
        rapidfire_wait_ms=action.get("rapidfire_wait_ms"),
        int_value=int_value,
    )
    if unsupported_rapidfire:
        log.warning(
            "Ignoring rapidfire for unsupported %s action in superkey runtime payload",
            action_type.value,
        )

    target = action.get("target")
    axis_value = 0
    if action_type == ActionType.GAMEPAD_AXIS:
        axis_value = int_value(action.get("value"), gamepad_axis_max_value(target))

    return SuperkeyActionData(
        action_type=action_type.value,
        target=optional_str(target),
        output_id=optional_str(action.get("output_id")),
        cmd=optional_str(action.get("cmd")),
        exec_ref=int_or_none(action.get("exec_ref")),
        macro_name=optional_str(action.get("macro_name")),
        macro_replay_mouse_movement=bool(action.get("macro_replay_mouse_movement", True)),
        macro_replay_mouse_clicks=bool(action.get("macro_replay_mouse_clicks", True)),
        macro_speed=1.0
        if macro_speed_value is None
        else float(cast(int | float | str | bytes, macro_speed_value)),
        macro_loop_mode=str_value(action.get("macro_loop_mode"), "none") or "none",
        macro_loop_count=int_value(action.get("macro_loop_count"), 1),
        macro_loop_stop_behavior=normalize_macro_loop_stop_behavior(
            action.get("macro_loop_stop_behavior")
        ),
        macro_move_to_start=bool(action.get("macro_move_to_start", False)),
        macro_start_x=int_value(action.get("macro_start_x"), 0),
        macro_start_y=int_value(action.get("macro_start_y"), 0),
        macro_block_mouse_movement=bool(action.get("macro_block_mouse_movement", False)),
        profile_name=optional_str(action.get("profile_name")),
        compositor_id=optional_str(action.get("compositor")),
        compositor_dispatcher=optional_str(action.get("dispatcher")),
        compositor_args=optional_str(action.get("args")),
        move_x=int_value(action.get("x"), 0),
        move_y=int_value(action.get("y"), 0),
        axis_value=axis_value,
        rapidfire_enabled=rapidfire_enabled,
        rapidfire_hold_ms=rapidfire_hold_ms,
        rapidfire_wait_ms=rapidfire_wait_ms,
    )
