from typing import cast

from keymasq.session.listeners.base import WindowListener

type JsonObject = dict[str, object]
type IntLike = int | float | str | bytes
type FloatLike = int | float | str | bytes


def json_object(value: object) -> JsonObject | None:
    return cast(JsonObject, value) if isinstance(value, dict) else None


def json_list(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def str_value(value: object, default: str = "") -> str:
    return default if value is None else str(value)


def int_value(value: object, default: int = 0) -> int:
    return default if value is None else int(cast(IntLike, value))


def float_value(value: object, default: float = 0.0) -> float:
    return default if value is None else float(cast(FloatLike, value))


def merge_support_details(
    base: dict[str, bool | str],
    listener: WindowListener | None,
) -> dict[str, bool | str | int]:
    merged: dict[str, bool | str | int] = dict(base)
    if listener is None:
        return merged
    runtime_details_getter = getattr(listener, "runtime_support_details", None)
    if callable(runtime_details_getter):
        runtime_details = json_object(runtime_details_getter())
        if runtime_details:
            for key, value in runtime_details.items():
                if isinstance(value, (bool, int, str)):
                    merged[key] = value
    return merged
