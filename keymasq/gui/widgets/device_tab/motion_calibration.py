"""Stationary gyro calibration inference."""

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from keymasq.common.model.motion import MotionAxisDefinition

GYRO_CALIBRATION_WARMUP_SECONDS = 0.35
MIN_GYRO_CALIBRATION_FRAMES = 30
MIN_GYRO_CALIBRATION_DURATION_SECONDS = 2.5
MAX_STATIONARY_RANGE_DPS = 8.0
_MOVEMENT_OUTLIER_FRACTION = 0.01


@dataclass(frozen=True)
class GyroCalibrationFrame:
    timestamp_ns: int
    values: Mapping[str, float]


@dataclass(frozen=True)
class GyroAxisCalibration:
    role: str
    offset: float
    noise: float


@dataclass(frozen=True)
class GyroCalibrationResult:
    axes: tuple[GyroAxisCalibration, ...]
    sample_count: int
    sample_duration_seconds: float
    maximum_noise_dps: float


def infer_stationary_gyro_calibration(
    axes: Sequence[MotionAxisDefinition],
    frames: Sequence[GyroCalibrationFrame],
) -> GyroCalibrationResult:
    """Infer raw bias and a canonical noise floor from complete stationary frames."""
    if not axes:
        raise ValueError("This sensor has no gyroscope axes.")

    roles = {axis.role for axis in axes}
    complete_frames = sorted(
        (frame for frame in frames if roles.issubset(frame.values)),
        key=lambda frame: frame.timestamp_ns,
    )
    if not complete_frames:
        raise ValueError(
            "No complete gyroscope frames were captured. Check the motion sensor and try again."
        )

    measurement_start_ns = complete_frames[0].timestamp_ns + int(
        GYRO_CALIBRATION_WARMUP_SECONDS * 1_000_000_000
    )
    measured_frames = [
        frame for frame in complete_frames if frame.timestamp_ns >= measurement_start_ns
    ]
    if len(measured_frames) < MIN_GYRO_CALIBRATION_FRAMES:
        raise ValueError(
            f"Only {len(measured_frames)} complete gyro frames were captured after settling. "
            "Check the motion sensor connection and try again."
        )

    sample_duration_seconds = (
        measured_frames[-1].timestamp_ns - measured_frames[0].timestamp_ns
    ) / 1_000_000_000.0
    if sample_duration_seconds < MIN_GYRO_CALIBRATION_DURATION_SECONDS:
        raise ValueError(
            f"Gyro frames covered only {sample_duration_seconds:.2f} seconds. "
            "Check the motion sensor connection and try again."
        )

    results: list[GyroAxisCalibration] = []
    maximum_noise_dps = 0.0
    for axis in axes:
        values = [float(frame.values[axis.role]) for frame in measured_frames]

        offset = float(statistics.median(values))
        range_dps = math.degrees(_trimmed_range(values) * axis.scale)
        if range_dps > MAX_STATIONARY_RANGE_DPS:
            raise ValueError(
                "The controller moved during calibration. Place it down and try again."
            )

        residuals = sorted(abs(value - offset) for value in values)
        percentile_index = min(len(residuals) - 1, math.ceil(len(residuals) * 0.95) - 1)
        noise = residuals[percentile_index] * axis.scale * 1.5
        maximum_noise_dps = max(maximum_noise_dps, math.degrees(noise))
        results.append(GyroAxisCalibration(role=axis.role, offset=offset, noise=noise))

    return GyroCalibrationResult(
        axes=tuple(results),
        sample_count=len(measured_frames),
        sample_duration_seconds=sample_duration_seconds,
        maximum_noise_dps=maximum_noise_dps,
    )


def _trimmed_range(values: Sequence[float]) -> float:
    ordered = sorted(values)
    trim_count = max(1, math.floor(len(ordered) * _MOVEMENT_OUTLIER_FRACTION))
    if trim_count * 2 >= len(ordered):
        return max(ordered) - min(ordered)
    return ordered[-trim_count - 1] - ordered[trim_count]
