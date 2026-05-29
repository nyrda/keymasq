from typing import cast

type JsonObject = dict[str, object]
type JsonObjectList = list[JsonObject]


def int_like(value: object, default: int = 0) -> int:
    return default if value in {None, ""} else int(cast(int | float | str, value))


def float_like(value: object, default: float = 0.0) -> float:
    return default if value in {None, ""} else float(cast(int | float | str, value))


def str_value(value: object, default: str = "") -> str:
    return default if value is None else str(value)
