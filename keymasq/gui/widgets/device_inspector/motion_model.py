"""Inspector-only motion estimation from calibrated, timestamped sensor frames."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from keymasq.common.coercion import coerce_float

from .model import Payload, int_or_none, list_of_dicts, text

Vector = tuple[float, float, float]
Quaternion = tuple[float, float, float, float]
IDENTITY: Quaternion = (1.0, 0.0, 0.0, 0.0)
GRAVITY = 9.80665


def multiply(a: Quaternion, b: Quaternion) -> Quaternion:
    w, x, y, z = a
    v, i, j, k = b
    return (
        w * v - x * i - y * j - z * k,
        w * i + x * v + y * k - z * j,
        w * j - x * k + y * v + z * i,
        w * k + x * j - y * i + z * v,
    )


def conjugate(q: Quaternion) -> Quaternion:
    return q[0], -q[1], -q[2], -q[3]


def rotate(q: Quaternion, point: Vector) -> Vector:
    result = multiply(multiply(q, (0.0, *point)), conjugate(q))
    return result[1], result[2], result[3]


def unit(q: Quaternion) -> Quaternion:
    length = math.sqrt(sum(value * value for value in q))
    return (q[0] / length, q[1] / length, q[2] / length, q[3] / length)


@dataclass
class MotionState:
    sensor: Payload
    raw: dict[tuple[str, str], int] = field(default_factory=dict)
    values: dict[tuple[str, str], float] = field(default_factory=dict)
    orientation: Quaternion = IDENTITY
    reference: Quaternion = IDENTITY
    timestamp_us: int = 0
    ready: bool = False
    gravity_initialized: bool = False
    resyncing: bool = False
    bound_source: str | None = None

    @property
    def axes(self) -> list[tuple[str, Payload]]:
        return [
            (kind, axis)
            for kind, key in (("gyro", "gyro_axes"), ("accelerometer", "accelerometer_axes"))
            for axis in list_of_dicts(self.sensor.get(key))
        ]

    @property
    def display_orientation(self) -> Quaternion:
        return multiply(conjugate(self.reference), self.orientation)

    def recenter(self) -> None:
        self.reference = self.orientation

    def reset(self) -> None:
        self.raw.clear()
        self.values.clear()
        self.orientation = IDENTITY
        self.reference = IDENTITY
        self.timestamp_us = 0
        self.ready = False
        self.gravity_initialized = False
        self.resyncing = False
        self.bound_source = None

    def accept(self, event: Payload) -> bool:
        """Consume matching ABS samples and integrate once per SYN_REPORT."""
        source = text(event.get("source")).strip().lower()
        configured_source = text(self.sensor.get("source")).strip().lower()
        if configured_source and source != configured_source:
            return False
        # Unscoped definitions must never combine samples from different interfaces.
        if self.bound_source is not None and source != self.bound_source:
            return False
        event_type = text(event.get("type_name"), text(event.get("type"))).lower()
        code = int_or_none(event.get("code"))
        name = text(event.get("code_name")).lower()
        if event_type in {"ev_syn", "0"}:
            if code == 3 or name == "syn_dropped":
                self.reset()
                self.bound_source = source
                self.resyncing = True
                return True
            if code != 0 and name != "syn_report":
                return False
            if self.resyncing:
                self.resyncing = False
                return False
            if not self.values:
                return False
            self._finish_frame(int_or_none(event.get("time_us")) or 0)
            return True
        if self.resyncing or event_type not in {"ev_abs", "3"}:
            return False
        for kind, axis in self.axes:
            axis_code = int_or_none(axis.get("evdev_code"))
            if not ((code is not None and code == axis_code) or name == text(axis.get("evdev"))):
                continue
            raw = int_or_none(event.get("value"))
            if raw is None:
                return False
            value = (raw - coerce_float(axis.get("offset"), 0.0)) * coerce_float(
                axis.get("scale"), 1.0
            )
            if axis.get("invert"):
                value = -value
            if not math.isfinite(value):
                return False
            if abs(value) < coerce_float(axis.get("noise"), 0.0):
                value = 0.0
            key = kind, text(axis.get("role"))
            self.raw[key] = raw
            self.values[key] = value
            self.bound_source = source
        return False

    def _finish_frame(self, timestamp_us: int) -> None:
        dt = (timestamp_us - self.timestamp_us) / 1_000_000 if self.timestamp_us else 0.0
        self.timestamp_us = timestamp_us
        self.ready = True
        gravity: Vector | None = None
        if all(("accelerometer", role) in self.values for role in ("x", "y", "z")):
            ax, ay, az = (self.values["accelerometer", role] for role in ("x", "y", "z"))
            magnitude = math.sqrt(ax * ax + ay * ay + az * az)
            # Shakes and free fall are not a reliable gravity reference.
            if 0.8 * GRAVITY <= magnitude <= 1.2 * GRAVITY:
                gravity = ax / magnitude, ay / magnitude, az / magnitude
        if gravity is not None and not self.gravity_initialized:
            ax, ay, az = gravity
            self.orientation = (
                unit((1.0 + az, ay, -ax, 0.0)) if az > -0.999999 else (0.0, 1.0, 0.0, 0.0)
            )
            self.gravity_initialized = True
        if not 0.0 < dt <= 0.1:
            return
        # Canonical pitch tips the front up, roll tips the right side down.
        wx = self.values.get(("gyro", "pitch"), 0.0)
        wy = -self.values.get(("gyro", "roll"), 0.0)
        wz = self.values.get(("gyro", "yaw"), 0.0)
        if gravity is not None:
            px, py, pz = rotate(conjugate(self.orientation), (0.0, 0.0, 1.0))
            ax, ay, az = gravity
            wx += 2.5 * (ay * pz - az * py)
            wy += 2.5 * (az * px - ax * pz)
            wz += 2.5 * (ax * py - ay * px)
        speed = math.sqrt(wx * wx + wy * wy + wz * wz)
        if speed > 0.0:
            half_angle = speed * dt / 2.0
            factor = math.sin(half_angle) / speed
            delta = (math.cos(half_angle), wx * factor, wy * factor, wz * factor)
            self.orientation = unit(multiply(self.orientation, delta))
