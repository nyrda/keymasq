from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GamepadAxisRange:
    target: str
    label: str
    evdev_name: str
    minimum: int
    maximum: int
    neutral: int = 0


GAMEPAD_AXIS_RANGES: dict[str, GamepadAxisRange] = {
    "abs_x": GamepadAxisRange("abs_x", "Left Stick X", "ABS_X", -32768, 32767),
    "abs_y": GamepadAxisRange("abs_y", "Left Stick Y", "ABS_Y", -32768, 32767),
    "abs_rx": GamepadAxisRange("abs_rx", "Right Stick X", "ABS_RX", -32768, 32767),
    "abs_ry": GamepadAxisRange("abs_ry", "Right Stick Y", "ABS_RY", -32768, 32767),
    "abs_z": GamepadAxisRange("abs_z", "Left Trigger", "ABS_Z", 0, 255),
    "abs_rz": GamepadAxisRange("abs_rz", "Right Trigger", "ABS_RZ", 0, 255),
}

GAMEPAD_AXIS_ALIASES = {
    "x": "abs_x",
    "y": "abs_y",
    "rx": "abs_rx",
    "ry": "abs_ry",
    "z": "abs_z",
    "rz": "abs_rz",
    "lt": "abs_z",
    "rt": "abs_rz",
    "left_trigger": "abs_z",
    "right_trigger": "abs_rz",
}


def normalize_gamepad_axis_target(value: object) -> str | None:
    target = str(value or "").strip().lower()
    if not target:
        return None
    target = target.removeprefix("ev_")
    if target.startswith("abs_") and target in GAMEPAD_AXIS_RANGES:
        return target
    if target.startswith("abs"):
        target = f"abs_{target.removeprefix('abs').lstrip('_')}"
    return GAMEPAD_AXIS_ALIASES.get(target, target if target in GAMEPAD_AXIS_RANGES else None)


def gamepad_axis_range(target: object) -> GamepadAxisRange | None:
    normalized = normalize_gamepad_axis_target(target)
    if normalized is None:
        return None
    return GAMEPAD_AXIS_RANGES.get(normalized)


def gamepad_axis_max_value(target: object) -> int:
    axis_range = gamepad_axis_range(target)
    return axis_range.maximum if axis_range is not None else 0


def clamp_gamepad_axis_value(target: object, value: object) -> int:
    axis_range = gamepad_axis_range(target)
    if axis_range is None:
        return 0
    try:
        raw_value = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raw_value = axis_range.maximum
    return max(axis_range.minimum, min(axis_range.maximum, raw_value))


def gamepad_axis_value_from_percent(target: object, percent: object) -> int:
    axis_range = gamepad_axis_range(target)
    if axis_range is None:
        return 0
    try:
        pct = float(percent)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        pct = 100.0
    if axis_range.minimum < 0:
        pct = max(-100.0, min(100.0, pct))
        endpoint = axis_range.maximum if pct >= 0 else abs(axis_range.minimum)
        return int(round((pct / 100.0) * endpoint))
    pct = max(0.0, min(100.0, pct))
    return int(round((pct / 100.0) * axis_range.maximum))


def gamepad_axis_percent_from_value(target: object, value: object) -> float:
    axis_range = gamepad_axis_range(target)
    if axis_range is None:
        return 0.0
    try:
        raw_value = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raw_value = float(axis_range.maximum)
    raw_value = max(float(axis_range.minimum), min(float(axis_range.maximum), raw_value))
    if axis_range.minimum < 0 and raw_value < 0:
        return (raw_value / abs(float(axis_range.minimum))) * 100.0
    if axis_range.maximum == 0:
        return 0.0
    return (raw_value / float(axis_range.maximum)) * 100.0
