"""Axis capabilities shared by output providers, editors, and analog routing."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import cast

import evdev

from keymasq.common.gamepad_axes import GAMEPAD_AXIS_RANGES, normalize_gamepad_axis_target


@dataclass(frozen=True, slots=True)
class OutputAxis:
    evdev: str
    label: str
    minimum: int
    maximum: int
    neutral: int = 0

    def __post_init__(self) -> None:
        name = normalize_gamepad_axis_target(self.evdev)
        if name is None or self.minimum >= self.maximum:
            raise ValueError("Output axis requires a valid ABS code and increasing range")
        if not self.minimum <= self.neutral <= self.maximum:
            raise ValueError("Output axis neutral must be within its range")
        object.__setattr__(self, "evdev", name.upper())

    @property
    def code(self) -> int:
        return int(getattr(evdev.ecodes, self.evdev))

    @property
    def discrete(self) -> bool:
        return self.evdev.startswith("ABS_HAT") and (self.minimum, self.maximum) == (-1, 1)

    def clamp(self, value: int) -> int:
        return max(self.minimum, min(self.maximum, value))


STANDARD_OUTPUT_AXES = tuple(
    OutputAxis(axis.evdev_name, axis.label, axis.minimum, axis.maximum, axis.neutral)
    for axis in GAMEPAD_AXIS_RANGES.values()
) + (
    OutputAxis("ABS_HAT0X", "Hat 0 X", -1, 1),
    OutputAxis("ABS_HAT0Y", "Hat 0 Y", -1, 1),
)


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value).get(name)
    return getattr(value, name, None)


def _integer(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            pass
    return None


def learned_output_axes(analog_inputs: Iterable[object]) -> tuple[OutputAxis, ...]:
    """Flatten learned controls, including stick components, without guessing ranges.

    Accept hardware model objects or their IPC dictionaries. Providers such as
    virtual-device templates can also construct OutputAxis directly.
    """
    result: dict[int, OutputAxis] = {}
    for analog in analog_inputs:
        axes = _field(analog, "axes")
        if not isinstance(axes, (list, tuple)):
            continue
        stick = _field(analog, "type") == "stick"
        label = str(_field(analog, "label") or _field(analog, "id") or "Axis")
        for axis in cast(Iterable[object], axes):
            minimum = _integer(_field(axis, "minimum"))
            maximum = _integer(_field(axis, "maximum"))
            if minimum is None or maximum is None or minimum >= maximum:
                continue
            neutral = _integer(_field(axis, "center" if stick else "rest"))
            if neutral is None:
                neutral = round((minimum + maximum) / 2) if stick else max(minimum, min(0, maximum))
            role = str(_field(axis, "role") or "").upper()
            try:
                spec = OutputAxis(
                    str(_field(axis, "evdev") or ""),
                    f"{label} {role}" if stick else label,
                    minimum,
                    maximum,
                    neutral,
                )
            except ValueError:
                continue
            result.setdefault(spec.code, spec)
    return tuple(result.values())


def find_output_axis(axes: Iterable[OutputAxis], name: str) -> OutputAxis | None:
    normalized = normalize_gamepad_axis_target(name)
    return next((axis for axis in axes if axis.evdev.lower() == normalized), None)
