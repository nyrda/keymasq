"""Stationary gyro calibration inference."""

import math
import statistics
from dataclasses import dataclass

from keymasq.common.model.motion import MotionAxisDefinition

MIN_GYRO_CALIBRATION_SAMPLES = 20
MAX_STATIONARY_RANGE_DPS = 8.0


@dataclass(frozen=True)
class GyroAxisCalibration:
    role: str
    offset: float
    noise: float


@dataclass(frozen=True)
class GyroCalibrationResult:
    axes: tuple[GyroAxisCalibration, ...]
    sample_count: int
    maximum_noise_dps: float


def infer_stationary_gyro_calibration(
    axes: list[MotionAxisDefinition],
    samples: dict[str, list[float]],
) -> GyroCalibrationResult:
    """Infer raw bias and a canonical noise floor from stationary samples."""
    if not axes:
        raise ValueError("This sensor has no gyroscope axes.")

    results: list[GyroAxisCalibration] = []
    counts: list[int] = []
    maximum_noise_dps = 0.0
    for axis in axes:
        values = [float(value) for value in samples.get(axis.role, [])]
        if len(values) < MIN_GYRO_CALIBRATION_SAMPLES:
            raise ValueError(
                f"Not enough {axis.role.capitalize()} samples. "
                "Keep the controller still and try again."
            )

        offset = float(statistics.median(values))
        range_dps = math.degrees((max(values) - min(values)) * axis.scale)
        if range_dps > MAX_STATIONARY_RANGE_DPS:
            raise ValueError(
                "The controller moved during calibration. Place it down and try again."
            )

        residuals = sorted(abs(value - offset) for value in values)
        percentile_index = min(len(residuals) - 1, math.ceil(len(residuals) * 0.95) - 1)
        noise = residuals[percentile_index] * axis.scale * 1.5
        maximum_noise_dps = max(maximum_noise_dps, math.degrees(noise))
        results.append(GyroAxisCalibration(role=axis.role, offset=offset, noise=noise))
        counts.append(len(values))

    return GyroCalibrationResult(
        axes=tuple(results),
        sample_count=min(counts),
        maximum_noise_dps=maximum_noise_dps,
    )
