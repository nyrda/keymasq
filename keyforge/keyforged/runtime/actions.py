from collections.abc import Callable
from typing import cast

from keyforge.common.models import ActionType, MappingAction
from keyforge.common.models import (
    SuperkeyConfig as CommonSuperkeyConfig,
)
from keyforge.keyforged.superkey_state import SuperkeyActionData, SuperkeyConfig

type JsonObject = dict[str, object]


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
    if action_type_str == "hyprland_dispatch":
        action_data = dict(action_data)
        action_data.setdefault("compositor", "hyprland")
        action_type_str = "compositor_dispatch"
    action_type = ActionType(action_type_str)

    superkey_config = None
    if action_type == ActionType.SUPERKEY and "superkey" in action_data:
        superkey_config = parse_superkey_config(
            manager,
            action_data["superkey"],
            json_object=getattr(manager, "_json_object", None),
            str_value=str_value,
            int_value=int_value,
            parse_superkey_action=parse_superkey_action,
        )

    target = action_data.get("target")
    cmd = action_data.get("cmd")
    macro_name = action_data.get("macro_name")
    profile_name = action_data.get("profile_name")
    compositor_id = action_data.get("compositor")
    compositor_dispatcher = action_data.get("dispatcher")
    compositor_args = action_data.get("args")

    return MappingAction(
        action_type=action_type,
        target=optional_str(target),
        keys=cast(list[str] | None, action_data.get("keys")),
        cmd=optional_str(cmd),
        exec_ref=int_or_none(action_data.get("exec_ref")),
        superkey_config=cast(CommonSuperkeyConfig | None, superkey_config),
        macro_name=optional_str(macro_name),
        macro_events=cast(list[JsonObject] | None, action_data.get("macro_events")),
        macro_replay_mouse_movement=bool(action_data.get("macro_replay_mouse_movement", True)),
        macro_replay_mouse_clicks=bool(action_data.get("macro_replay_mouse_clicks", True)),
        macro_speed=float_value(action_data.get("macro_speed"), 1.0),
        macro_loop_mode=str_value(action_data.get("macro_loop_mode"), "none") or "none",
        macro_loop_count=int_value(action_data.get("macro_loop_count"), 1),
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
        move_speed=float_value(action_data.get("speed"), 1.0),
        move_jitter=float_value(action_data.get("jitter"), 0.3),
        rapidfire_enabled=bool(action_data.get("rapidfire_enabled", False)),
        rapidfire_hold_ms=int_value(action_data.get("rapidfire_hold_ms"), 20),
        rapidfire_wait_ms=int_value(action_data.get("rapidfire_wait_ms"), 20),
        tap_enabled=bool(action_data.get("tap_enabled", False)),
        tap_hold_ms=int_value(action_data.get("tap_hold_ms"), 10),
    )


def parse_superkey_config(
    manager: object,
    data: object,
    *,
    json_object: Callable[[object], JsonObject | None] | None,
    str_value: Callable[..., str],
    int_value: Callable[..., int],
    parse_superkey_action: Callable[..., SuperkeyActionData | None],
) -> SuperkeyConfig:
    if json_object is not None:
        config = json_object(data)
    else:
        config = cast(JsonObject | None, data if isinstance(data, dict) else None)
    if config is None:
        raise TypeError("superkey config must be an object")
    return SuperkeyConfig(
        name=str_value(config.get("name"), ""),
        tap_timeout_ms=int_value(config.get("tap_timeout_ms"), 200),
        double_tap_window_ms=int_value(config.get("double_tap_window_ms"), 300),
        hold_threshold_ms=int_value(config.get("hold_threshold_ms"), 300),
        tap_action=parse_superkey_action(
            manager,
            config.get("tap_action"),
            json_object=json_object,
            str_value=str_value,
            optional_str=getattr(manager, "_optional_str", None),
            int_or_none=getattr(manager, "_int_or_none", None),
            int_value=int_value,
        ),
        double_tap_action=parse_superkey_action(
            manager,
            config.get("double_tap_action"),
            json_object=json_object,
            str_value=str_value,
            optional_str=getattr(manager, "_optional_str", None),
            int_or_none=getattr(manager, "_int_or_none", None),
            int_value=int_value,
        ),
        hold_action=parse_superkey_action(
            manager,
            config.get("hold_action"),
            json_object=json_object,
            str_value=str_value,
            optional_str=getattr(manager, "_optional_str", None),
            int_or_none=getattr(manager, "_int_or_none", None),
            int_value=int_value,
        ),
        tap_hold_action=parse_superkey_action(
            manager,
            config.get("tap_hold_action"),
            json_object=json_object,
            str_value=str_value,
            optional_str=getattr(manager, "_optional_str", None),
            int_or_none=getattr(manager, "_int_or_none", None),
            int_value=int_value,
        ),
    )


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

    return SuperkeyActionData(
        action_type=str_value(action.get("action"), "keyboard"),
        target=optional_str(action.get("target")),
        cmd=optional_str(action.get("cmd")),
        exec_ref=int_or_none(action.get("exec_ref")),
        macro_name=optional_str(action.get("macro_name")),
        rapidfire_enabled=bool(action.get("rapidfire_enabled", False)),
        rapidfire_hold_ms=int_value(action.get("rapidfire_hold_ms"), 20),
        rapidfire_wait_ms=int_value(action.get("rapidfire_wait_ms"), 20),
    )
