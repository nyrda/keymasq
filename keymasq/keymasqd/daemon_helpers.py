from typing import cast

type JsonObject = dict[str, object]
type JsonObjectList = list[JsonObject]


def int_like(value: object, default: int) -> int:
    return default if value in {None, ""} else int(cast(int | float | str, value))


def float_like(value: object, default: float) -> float:
    return default if value in {None, ""} else float(cast(int | float | str, value))


def str_value(value: object, default: str = "") -> str:
    return default if value is None else str(value)


def json_object(value: object) -> JsonObject:
    return cast(JsonObject, value)


def json_object_list(value: object) -> JsonObjectList:
    return cast(JsonObjectList, value)


def str_list(value: object) -> list[str]:
    return cast(list[str], value)


def str_dict(value: object) -> dict[str, str]:
    return cast(dict[str, str], value)


def int_dict(value: object) -> dict[str, int]:
    return cast(dict[str, int], value)
