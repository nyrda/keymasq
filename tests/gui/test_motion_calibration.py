import math
from typing import Any, cast

import evdev
import pytest

from keymasq.common.model.hardware import HardwareConfig
from keymasq.common.model.motion import MotionAxisDefinition, MotionSensorDefinition
from keymasq.gui.widgets.device_tab.motion_calibration import (
    MAX_STATIONARY_RANGE_DPS,
    infer_stationary_gyro_calibration,
)


def _gyro_axes() -> list[MotionAxisDefinition]:
    scale = math.radians(1 / 16)
    return [
        MotionAxisDefinition("pitch", "abs_rx", evdev.ecodes.ABS_RX, scale=scale),
        MotionAxisDefinition("yaw", "abs_ry", evdev.ecodes.ABS_RY, scale=scale),
        MotionAxisDefinition("roll", "abs_rz", evdev.ecodes.ABS_RZ, scale=scale),
    ]


def test_stationary_gyro_calibration_infers_bias_and_noise() -> None:
    axes = _gyro_axes()
    samples = {
        "pitch": [100 + value for value in (-1, 0, 1, 0) * 10],
        "yaw": [-50 + value for value in (0, 1, 0, -1) * 10],
        "roll": [25 + value for value in (1, 0, -1, 0) * 10],
    }

    result = infer_stationary_gyro_calibration(axes, samples)

    assert result.sample_count == 40
    assert [axis.offset for axis in result.axes] == [100.0, -50.0, 25.0]
    assert [math.degrees(axis.noise) for axis in result.axes] == pytest.approx(
        [0.09375, 0.09375, 0.09375]
    )
    assert result.maximum_noise_dps == pytest.approx(0.09375)


def test_stationary_gyro_calibration_rejects_movement() -> None:
    axes = _gyro_axes()
    moving_range = math.ceil(MAX_STATIONARY_RANGE_DPS * 16) + 1
    samples = {
        "pitch": [0] * 20 + [moving_range],
        "yaw": [0] * 21,
        "roll": [0] * 21,
    }

    with pytest.raises(ValueError, match="moved during calibration"):
        infer_stationary_gyro_calibration(axes, samples)


def test_stationary_gyro_calibration_requires_each_axis() -> None:
    with pytest.raises(ValueError, match="Not enough Yaw samples"):
        infer_stationary_gyro_calibration(
            _gyro_axes(),
            {"pitch": [0] * 20, "yaw": [0] * 19, "roll": [0] * 20},
        )


def test_guided_dialog_applies_only_matching_gyro_samples() -> None:
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from keymasq.gui.widgets.device_tab.motion_calibration_dialog import (
        MotionCalibrationDialog,
    )

    class _HardwareManager:
        def __init__(self) -> None:
            self.saved: list[HardwareConfig] = []

        def save_hardware(self, hardware: HardwareConfig) -> None:
            self.saved.append(hardware)

    axes = _gyro_axes()
    sensor = MotionSensorDefinition(
        id="imu",
        label="Nintendo Motion Sensor",
        source="imu",
        gyro_axes=axes,
    )
    hardware = HardwareConfig("057e", "2009", "Pro Controller", [], [], motion_sensors=[sensor])
    manager = _HardwareManager()
    dialog = MotionCalibrationDialog(
        Gtk.Window(),
        hardware,
        sensor,
        cast(Any, manager),
    )
    dialog._samples = {axis.role: [] for axis in axes}

    for _index in range(25):
        dialog._record_sample(
            {
                "source": "gamepad",
                "code": evdev.ecodes.ABS_RX,
                "value": 999,
            }
        )
        for axis, bias in zip(axes, (120, -35, 8), strict=True):
            dialog._record_sample(
                {
                    "source": "imu",
                    "code": axis.evdev_code,
                    "value": bias,
                }
            )

    dialog._apply_capture()

    assert [axis.offset for axis in axes] == [120.0, -35.0, 8.0]
    assert sensor.calibration_samples == 25
    assert sensor.calibrated_at is not None
    assert manager.saved == [hardware]
