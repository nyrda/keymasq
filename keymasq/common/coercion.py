from typing import cast

from keymasq.common.types import JsonObject

type IntLike = int | float | str | bytes
type FloatLike = int | float | str | bytes


def json_object(value: object) -> JsonObject | None:
    return cast(JsonObject, value) if isinstance(value, dict) else None


def json_object_or_empty(value: object) -> JsonObject:
    return cast(JsonObject, value) if isinstance(value, dict) else {}


def require_json_object(value: object) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    return cast(JsonObject, value)


def json_list(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def str_value(value: object, default: str = "") -> str:
    return default if value is None else str(value)


def int_value(value: object, default: int = 0) -> int:
    return default if value is None else int(cast(IntLike, value))


def float_value(value: object, default: float = 0.0) -> float:
    return default if value is None else float(cast(FloatLike, value))


def optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def str_or_none(value: object) -> str | None:
    return None if value is None else str(value)


def int_or_none(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(cast(IntLike, value))
    except (TypeError, ValueError):
        return None


def int_value_or_default(value: object, default: int = 0) -> int:
    try:
        return int(cast(IntLike, value))
    except (TypeError, ValueError):
        return default


def int_or_none_value(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(cast(IntLike, value))
    except (TypeError, ValueError):
        return None


def float_value_or_default(value: object, default: float = 0.0) -> float:
    try:
        return float(cast(FloatLike, value))
    except (TypeError, ValueError):
        return default


def int_like(value: object, default: int = 0) -> int:
    return default if value in {None, ""} else int(cast(int | float | str, value))


def float_like(value: object, default: float = 0.0) -> float:
    return default if value in {None, ""} else float(cast(int | float | str, value))


def bool_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
