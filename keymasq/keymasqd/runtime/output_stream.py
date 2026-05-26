from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

type JsonObject = dict[str, object]


def _ecodes(evdev_mod: Any) -> object:
    return cast(object, getattr(evdev_mod, "ecodes", None))


def _ecode_int(evdev_mod: Any, name: str, default: int) -> int:
    value = getattr(_ecodes(evdev_mod), name, default)
    try:
        if isinstance(value, int | float | str | bytes | bytearray):
            return int(value)
    except (TypeError, ValueError):
        pass
    return int(default)


def _bytype(evdev_mod: Any) -> Mapping[int, object]:
    raw = getattr(_ecodes(evdev_mod), "bytype", {})
    if isinstance(raw, Mapping):
        return cast(Mapping[int, object], raw)
    return {}


def event_type_name(evdev_mod: Any, event_type: int) -> str:
    value = _bytype(evdev_mod).get(int(event_type))
    if isinstance(value, tuple | list) and value:
        return str(cast(object, value[0]))
    if isinstance(value, str):
        return value
    names = {
        _ecode_int(evdev_mod, "EV_KEY", 1): "EV_KEY",
        _ecode_int(evdev_mod, "EV_REL", 2): "EV_REL",
        _ecode_int(evdev_mod, "EV_ABS", 3): "EV_ABS",
        _ecode_int(evdev_mod, "EV_SYN", 0): "EV_SYN",
    }
    return names.get(int(event_type), str(int(event_type)))


def event_code_name(evdev_mod: Any, event_type: int, code: int) -> str:
    type_codes = _bytype(evdev_mod).get(int(event_type), {})
    if isinstance(type_codes, dict):
        value = cast(Mapping[int, object], type_codes).get(int(code))
        if isinstance(value, tuple | list) and value:
            return str(cast(object, value[0]))
        if isinstance(value, str):
            return value
    return str(int(code))


def event_filter_category(evdev_mod: Any, event_type: int, code: int) -> str:
    ecodes = _ecodes(evdev_mod)
    event_type_int = int(event_type)
    if event_type_int == _ecode_int(evdev_mod, "EV_KEY", 1):
        return "button"
    if event_type_int == _ecode_int(evdev_mod, "EV_ABS", 3):
        return "axis"
    if event_type_int == _ecode_int(evdev_mod, "EV_REL", 2):
        rel_x = int(getattr(ecodes, "REL_X", 0))
        rel_y = int(getattr(ecodes, "REL_Y", 1))
        if int(code) in {rel_x, rel_y}:
            return "mousemove"
        return "axis"
    if event_type_int == _ecode_int(evdev_mod, "EV_SYN", 0):
        return "syn"
    return "other"


def build_output_event(
    *,
    output_target: JsonObject,
    event_type: int,
    code: int,
    value: int,
    evdev_mod: Any,
) -> JsonObject:
    return {
        "kind": "output",
        **output_target,
        "filter_category": event_filter_category(evdev_mod, event_type, code),
        "type": int(event_type),
        "type_name": event_type_name(evdev_mod, event_type),
        "code": int(code),
        "code_name": event_code_name(evdev_mod, event_type, code),
        "value": int(value),
    }
