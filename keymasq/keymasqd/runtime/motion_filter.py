"""Timestamp-based One Euro filtering for normalized motion axes."""

import math
from dataclasses import dataclass


@dataclass
class OneEuroFilter:
    timestamp_ns: int | None = None
    raw_value: float = 0.0
    value: float = 0.0
    derivative: float = 0.0

    def update(self, value: float, timestamp_ns: int, min_cutoff_hz: float, beta: float) -> float:
        previous_ns = self.timestamp_ns
        # Reinitialize on a clock discontinuity or a pause; stale history adds lag.
        if (
            previous_ns is None
            or timestamp_ns < previous_ns
            or timestamp_ns - previous_ns > 500_000_000
        ):
            self.timestamp_ns = timestamp_ns
            self.raw_value = self.value = value
            self.derivative = 0.0
            return value
        if timestamp_ns == previous_ns:
            return self.value

        dt = (timestamp_ns - previous_ns) / 1_000_000_000.0
        derivative = (value - self.raw_value) / dt
        self.derivative += _alpha(1.0, dt) * (derivative - self.derivative)
        cutoff = min_cutoff_hz + beta * abs(self.derivative)
        self.value += _alpha(cutoff, dt) * (value - self.value)
        self.raw_value = value
        self.timestamp_ns = timestamp_ns
        return self.value


def _alpha(cutoff_hz: float, dt: float) -> float:
    return 1.0 / (1.0 + 1.0 / (2.0 * math.pi * cutoff_hz * dt))
