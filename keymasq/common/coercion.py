import math
from collections.abc import Callable
from typing import cast, overload

from keymasq.common.types import JsonObject

__all__ = [
    "bool_value",
    "coerce_bool",
    "coerce_float",
    "coerce_int",
    "coerce_str",
    "json_list",
    "json_object",
    "json_object_or_empty",
    "require_json_object",
]

type _NumberInput = int | float | str | bytes

_CONVERSION_ERRORS = (OverflowError, TypeError, ValueError)
_TRUE_BOOL_TEXT = {"1", "true", "yes", "on"}
_FALSE_BOOL_TEXT = {"0", "false", "no", "off"}


def json_object(value: object) -> JsonObject | None:
    """Return value as a JSON object when it is a dict, otherwise None."""
    return cast(JsonObject, value) if isinstance(value, dict) else None


def json_object_or_empty(value: object) -> JsonObject:
    """Return value as a JSON object when it is a dict, otherwise an empty dict."""
    return cast(JsonObject, value) if isinstance(value, dict) else {}


def require_json_object(value: object) -> JsonObject:
    """Return value as a JSON object when it is a dict, otherwise raise ValueError."""
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    return cast(JsonObject, value)


def json_list(value: object) -> list[object]:
    """Return value as a list when it is a list, otherwise an empty list."""
    return cast(list[object], value) if isinstance(value, list) else []


@overload
def coerce_str(
    value: object,
    default: str = "",
) -> str: ...


@overload
def coerce_str(
    value: object,
    default: None,
) -> str | None: ...


def coerce_str(
    value: object,
    default: str | None = "",
) -> str | None:
    """Return default for None, otherwise return str(value)."""
    if value is None:
        return default
    return str(value)


@overload
def coerce_int(
    value: object,
    default: int = 0,
) -> int: ...


@overload
def coerce_int(
    value: object,
    default: None,
) -> int | None: ...


def coerce_int(
    value: object,
    default: int | None = 0,
) -> int | None:
    """Return int(value), or default when the value is missing or invalid."""
    return _coerce_number(value, default, _coerce_int)


@overload
def coerce_float(
    value: object,
    default: float = 0.0,
) -> float: ...


@overload
def coerce_float(
    value: object,
    default: None,
) -> float | None: ...


def coerce_float(
    value: object,
    default: float | None = 0.0,
) -> float | None:
    """Return a finite float(value), or default when missing or invalid."""
    return _coerce_number(value, default, _coerce_float)


def coerce_bool(value: object, default: bool = False, *, strict: bool = False) -> bool:
    """Return bool(value), parsing common boolean strings explicitly."""
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "":
            return bool(default)
        if normalized in _TRUE_BOOL_TEXT:
            return True
        if normalized in _FALSE_BOOL_TEXT:
            return False
        if strict:
            raise ValueError(f"Unrecognized boolean value: {value!r}")
        return bool(default)
    if strict:
        raise ValueError(f"Unrecognized boolean value: {value!r}")
    return bool(value)


def _coerce_number[T](
    value: object,
    default: T | None,
    convert: Callable[[object], T],
) -> T | None:
    if value is None or _is_empty_number_text(value) or isinstance(value, bool):
        return default
    try:
        return convert(value)
    except _CONVERSION_ERRORS:
        return default


def _coerce_int(value: object) -> int:
    return int(cast(_NumberInput, value))


def _coerce_float(value: object) -> float:
    converted = float(cast(_NumberInput, value))
    if not math.isfinite(converted):
        raise ValueError("float must be finite")
    return converted


def _is_empty_number_text(value: object) -> bool:
    return isinstance(value, str) and value == ""


def bool_value(value: object) -> bool:
    """Parse booleans from bools and common truthy strings; other values use bool(value)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
