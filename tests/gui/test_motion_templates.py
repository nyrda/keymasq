import math

import evdev
import pytest

from keymasq.gui.wizards.hardware_setup.flow import merge_inventory_abs_info
from keymasq.gui.wizards.hardware_setup.templates import (
    build_gamepad_analog_inputs,
    build_motion_sensors,
)


@pytest.mark.parametrize(
    ("driver", "expected_gyro", "expected_accelerometer"),
    [
        (
            "hid-playstation",
            {
                "abs_rx": ("pitch", False),
                "abs_ry": ("yaw", False),
                "abs_rz": ("roll", False),
            },
            {"abs_x": ("x", False), "abs_y": ("y", False), "abs_z": ("z", False)},
        ),
        (
            "hid-nintendo",
            {
                "abs_rx": ("roll", True),
                "abs_ry": ("pitch", True),
                "abs_rz": ("yaw", False),
            },
            {"abs_x": ("y", False), "abs_y": ("x", True), "abs_z": ("z", False)},
        ),
        (
            "hid-steam",
            {
                "abs_rx": ("pitch", False),
                "abs_ry": ("yaw", False),
                "abs_rz": ("roll", False),
            },
            {"abs_x": ("x", False), "abs_y": ("y", False), "abs_z": ("z", False)},
        ),
    ],
)
def test_motion_templates_normalize_driver_axes(
    driver: str,
    expected_gyro: dict[str, tuple[str, bool]],
    expected_accelerometer: dict[str, tuple[str, bool]],
) -> None:
    interface = {
        "id": "motion",
        "device_types": ["motion"],
        "driver": driver,
        "raw_capabilities": {
            evdev.ecodes.EV_ABS: [
                (code, evdev.AbsInfo(0, -32768, 32768, 0, 0, 16))
                for code in (
                    evdev.ecodes.ABS_X,
                    evdev.ecodes.ABS_Y,
                    evdev.ecodes.ABS_Z,
                    evdev.ecodes.ABS_RX,
                    evdev.ecodes.ABS_RY,
                    evdev.ecodes.ABS_RZ,
                )
            ]
        },
    }

    sensor = build_motion_sensors([interface])[0]

    assert {axis.evdev: (axis.role, axis.invert) for axis in sensor.gyro_axes} == expected_gyro
    assert {
        axis.evdev: (axis.role, axis.invert) for axis in sensor.accelerometer_axes
    } == expected_accelerometer


@pytest.mark.parametrize("driver", ["steam", "hid-steam"])
def test_motion_template_uses_kernel_resolution_and_is_not_a_gamepad_stick(
    driver: str,
) -> None:
    interface = {
        "id": "motion",
        "device_types": ["motion"],
        "driver": driver,
        "raw_capabilities": {
            evdev.ecodes.EV_ABS: [
                (evdev.ecodes.ABS_X, evdev.AbsInfo(0, -32768, 32768, 0, 0, 16384)),
                (evdev.ecodes.ABS_Y, evdev.AbsInfo(0, -32768, 32768, 0, 0, 16384)),
                (evdev.ecodes.ABS_Z, evdev.AbsInfo(0, -32768, 32768, 0, 0, 16384)),
                (evdev.ecodes.ABS_RX, evdev.AbsInfo(0, -32768, 32768, 0, 0, 16)),
                (evdev.ecodes.ABS_RY, evdev.AbsInfo(0, -32768, 32768, 0, 0, 16)),
                (evdev.ecodes.ABS_RZ, evdev.AbsInfo(0, -32768, 32768, 0, 0, 16)),
            ]
        },
    }

    sensors = build_motion_sensors([interface])

    assert build_gamepad_analog_inputs([interface]) == []
    assert len(sensors) == 1
    assert sensors[0].driver == driver
    assert sensors[0].label == "Steam Motion Sensor"
    assert math.isclose(sensors[0].gyro_axes[0].scale, math.radians(1 / 16))
    assert math.isclose(sensors[0].accelerometer_axes[0].scale, 9.80665 / 16384)


def test_motion_template_recognizes_nintendo_sysfs_driver_name() -> None:
    interface = {
        "id": "imu",
        "device_types": ["motion"],
        "driver": "nintendo",
        "raw_capabilities": {
            evdev.ecodes.EV_ABS: [
                (evdev.ecodes.ABS_X, evdev.AbsInfo(0, -32767, 32767, 10, 0, 4096)),
                (evdev.ecodes.ABS_RX, evdev.AbsInfo(0, -32767000, 32767000, 10, 0, 14247)),
            ]
        },
    }

    sensor = build_motion_sensors([interface])[0]

    assert sensor.driver == "nintendo"
    assert sensor.label == "Nintendo Motion Sensor"
    assert math.isclose(sensor.gyro_axes[0].scale, math.radians(1 / 14247))
    assert math.isclose(sensor.accelerometer_axes[0].scale, 9.80665 / 4096)


def test_daemon_abs_info_recovers_unreadable_nintendo_imu_metadata() -> None:
    raw_capabilities = merge_inventory_abs_info(
        {},
        {
            str(evdev.ecodes.ABS_X): {
                "value": 0,
                "minimum": -32767,
                "maximum": 32767,
                "fuzz": 10,
                "flat": 0,
                "resolution": 4096,
            },
            str(evdev.ecodes.ABS_RX): {
                "value": 0,
                "minimum": -32767000,
                "maximum": 32767000,
                "fuzz": 10,
                "flat": 0,
                "resolution": 14247,
            },
        },
    )

    sensor = build_motion_sensors(
        [
            {
                "id": "imu",
                "device_types": ["motion"],
                "driver": "nintendo",
                "raw_capabilities": raw_capabilities,
            }
        ]
    )[0]

    assert math.isclose(sensor.gyro_axes[0].scale, math.radians(1 / 14247))
    assert math.isclose(sensor.accelerometer_axes[0].scale, 9.80665 / 4096)
