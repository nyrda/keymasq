from typing import cast

from keymasq.common.types import JsonObject

type IntLike = int | float | str | bytes
type FloatLike = int | float | str | bytes


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


def str_value(value: object, default: str = "") -> str:
    """Return default only for None; all other values are converted with str()."""
    return default if value is None else str(value)


def int_value(value: object, default: int = 0) -> int:
    """Return default only for None; invalid int conversions raise TypeError or ValueError."""
    return default if value is None else int(cast(IntLike, value))


def float_value(value: object, default: float = 0.0) -> float:
    """Return default only for None; invalid float conversions raise TypeError or ValueError."""
    return default if value is None else float(cast(FloatLike, value))


def optional_str(value: object) -> str | None:
    """Convert value to stripped text, returning None for None or an empty stripped string."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def str_or_none(value: object) -> str | None:
    """Return None only for None; all other values are converted with str() unchanged."""
    return None if value is None else str(value)


def int_or_none(value: object, *, reject_bool: bool = True) -> int | None:
    """Return None for None or failed int conversion.

    By default, booleans are rejected so JSON true/false are not treated as 1/0; pass
    reject_bool=False to allow bool conversion.
    """
    if value is None or (reject_bool and isinstance(value, bool)):
        return None
    try:
        return int(cast(IntLike, value))
    except (TypeError, ValueError):
        return None


def int_value_or_default(value: object, default: int = 0) -> int:
    """Return int(value), or default for TypeError and ValueError conversion failures."""
    try:
        return int(cast(IntLike, value))
    except (TypeError, ValueError):
        return default


def int_or_none_value(value: object) -> int | None:
    """Return int_or_none(value, reject_bool=False) for callers that allow bool conversion."""
    return int_or_none(value, reject_bool=False)


def float_value_or_default(value: object, default: float = 0.0) -> float:
    """Return float(value), or default for TypeError and ValueError conversion failures."""
    try:
        return float(cast(FloatLike, value))
    except (TypeError, ValueError):
        return default


def int_like(value: object, default: int = 0) -> int:
    """Return default for None, empty string, or any failed int conversion."""
    if value in {None, ""}:
        return default
    try:
        return int(cast(int | float | str, value))
    except (TypeError, ValueError):
        return default


def float_like(value: object, default: float = 0.0) -> float:
    """Return default for None, empty string, or any failed float conversion."""
    if value in {None, ""}:
        return default
    try:
        return float(cast(int | float | str, value))
    except (TypeError, ValueError):
        return default


def bool_value(value: object) -> bool:
    """Parse booleans from bools and common truthy strings; other values use bool(value)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
