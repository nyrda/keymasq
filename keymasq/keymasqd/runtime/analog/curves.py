"""Pure normalization, denormalization, and response-curve helpers."""

import math

from keymasq.common.model.analog import analog_gamepad_output_distance

DEFAULT_STICK_MIN = -32768
DEFAULT_STICK_MAX = 32767
DEFAULT_TRIGGER_MIN = 0
DEFAULT_TRIGGER_MAX = 255


def normalize_axis_value(
    raw_value: int,
    minimum: int,
    maximum: int,
    *,
    center: int | None = None,
    invert: bool = False,
) -> float:
    if minimum >= maximum:
        minimum = DEFAULT_STICK_MIN
        maximum = DEFAULT_STICK_MAX

    midpoint = float(center) if center is not None else (float(minimum) + float(maximum)) / 2.0
    raw = float(raw_value)
    if raw < midpoint:
        span = max(1.0, midpoint - float(minimum))
        normalized = (raw - midpoint) / span
    else:
        span = max(1.0, float(maximum) - midpoint)
        normalized = (raw - midpoint) / span
    if invert:
        normalized = -normalized
    return max(-1.0, min(1.0, normalized))


def normalize_control_axis_value(
    raw_value: int,
    minimum: int,
    maximum: int,
    *,
    rest: int | None = None,
) -> float:
    if minimum >= maximum:
        minimum = DEFAULT_TRIGGER_MIN
        maximum = DEFAULT_TRIGGER_MAX
    if rest is None:
        rest = minimum if minimum >= 0 else 0
    positive_span = float(maximum) - float(rest)
    negative_span = float(minimum) - float(rest)
    active_span = positive_span if abs(positive_span) >= abs(negative_span) else negative_span
    if abs(active_span) < 1.0:
        active_span = float(maximum) - float(minimum)
    if abs(active_span) < 1.0:
        return 0.0
    normalized = (float(raw_value) - float(rest)) / active_span
    return max(0.0, min(1.0, normalized))


def denormalize_axis_value(
    value: float,
    minimum: int,
    maximum: int,
    *,
    center: int | None = None,
    invert: bool = False,
) -> int:
    if minimum >= maximum:
        minimum = DEFAULT_STICK_MIN
        maximum = DEFAULT_STICK_MAX
    normalized = max(-1.0, min(1.0, float(value)))
    if invert:
        normalized = -normalized
    midpoint = int(center) if center is not None else int(round((minimum + maximum) / 2.0))
    if normalized >= 0.0:
        return min(maximum, int(round(midpoint + normalized * (maximum - midpoint))))
    return max(minimum, int(round(midpoint + normalized * (midpoint - minimum))))


def denormalize_control_axis_value(
    value: float,
    minimum: int,
    maximum: int,
    *,
    rest: int | None = None,
    invert: bool = False,
) -> int:
    if minimum >= maximum:
        minimum = DEFAULT_TRIGGER_MIN
        maximum = DEFAULT_TRIGGER_MAX
    if rest is None:
        rest = minimum if minimum >= 0 else 0
    normalized = max(0.0, min(1.0, float(value)))
    endpoint = minimum if invert else maximum
    return max(minimum, min(maximum, int(round(float(rest) + normalized * (endpoint - rest)))))


def apply_stick_output_curve(
    x: float,
    y: float,
    *,
    deadzone: float,
    sensitivity: float,
    response_curve: float,
) -> tuple[float, float]:
    magnitude = math.sqrt(x * x + y * y)
    scaled = analog_gamepad_output_distance(
        magnitude,
        deadzone=deadzone,
        sensitivity=sensitivity,
        response_curve=response_curve,
    )
    if scaled <= 0.0 or magnitude <= 0.0:
        return 0.0, 0.0
    direction_x = x / magnitude
    direction_y = y / magnitude
    return direction_x * scaled, direction_y * scaled


def apply_control_axis_output_curve(
    value: float,
    *,
    deadzone: float,
    sensitivity: float,
    response_curve: float,
) -> float:
    return analog_gamepad_output_distance(
        max(0.0, min(1.0, value)),
        deadzone=deadzone,
        sensitivity=sensitivity,
        response_curve=response_curve,
    )


def apply_signed_axis_output_curve(
    value: float,
    *,
    deadzone: float,
    sensitivity: float,
    response_curve: float,
) -> float:
    value = max(-1.0, min(1.0, value))
    scaled = apply_control_axis_output_curve(
        abs(value),
        deadzone=deadzone,
        sensitivity=sensitivity,
        response_curve=response_curve,
    )
    return math.copysign(scaled, value)
