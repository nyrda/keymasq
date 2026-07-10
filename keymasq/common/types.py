from dataclasses import dataclass
from typing import Any

type JsonObject = dict[str, Any]
type JsonObjectList = list[JsonObject]


@dataclass(init=False)
class SyntheticInputEvent:
    type: int
    code: int
    value: int

    def __init__(self, event_type: int, code: int, value: int) -> None:
        self.type = int(event_type)
        self.code = int(code)
        self.value = int(value)
