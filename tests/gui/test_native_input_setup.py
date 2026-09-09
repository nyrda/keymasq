import math

import pytest

from keymasq.common.model.core import DeviceType
from keymasq.common.model.hardware import EvdevDevice, HardwareConfig
from keymasq.gui.widgets.device_tab.hardware_settings_dialog import append_evdev_device_selection
from keymasq.gui.wizards.hardware_setup import templates
from keymasq.gui.wizards.hardware_setup.types import EvdevDeviceSelection
from keymasq.session.hardware import HardwareManager
from keymasq.session.manager.profile.grab_plan import (
    all_configured_interfaces,
    configured_interface_descriptors,
)


def interfaces():
    return [
        {
            "id": "gamepad",
            "path": "/dev/input/event26",
            "config_path": "keymasq:2dc8:6012",
            "device_types": ["gamepad"],
            "phys": "usb-test/input0",
        },
        {
            "id": "imu",
            "path": "/dev/keymasq-sources/8bitdo-ultimate2/instance/hidraw7",
            "backend": "hidraw",
            "driver": "8bitdo-ultimate2",
            "device_types": ["motion"],
            "name": "Ultimate 2 motion",
            "phys": "usb-test/input0",
            "companion_paths": ["/dev/input/event26"],
            "native_motion_axes": {
                "gyro_axes": [
                    {
                        "role": "yaw",
                        "evdev": "abs_rz",
                        "evdev_code": 5,
                        "scale": math.radians(2000) / 32767,
                    }
                ],
                "accelerometer_axes": [
                    {"role": "z", "evdev": "abs_z", "evdev_code": 2, "scale": 9.80665 / 4096}
                ],
            },
        },
    ]


def test_setup_persists_native_source_and_precise_calibration(temp_config_dir):
    detected = interfaces()
    hardware = HardwareConfig(
        "2dc8",
        "6012",
        "Ultimate 2",
        templates.build_evdev_devices(detected),
        [],
        motion_sensors=templates.build_motion_sensors(detected),
        input_sources=templates.build_input_sources(detected),
    )
    assert len(hardware.evdev_devices) == 1
    assert hardware.input_sources[0].companion_of == "gamepad"
    hardware.motion_sensors[0].gyro_axes[0].offset = 23.5
    manager = HardwareManager()
    manager.save_hardware(hardware)
    reloaded = HardwareManager().get_hardware(hardware.hardware_id)
    assert reloaded is not None
    assert reloaded.input_sources == hardware.input_sources
    assert reloaded.motion_sensors == hardware.motion_sensors
    assert reloaded.motion_sensors[0].gyro_axes[0].scale == pytest.approx(
        math.radians(2000) / 32767
    )
    payload = configured_interface_descriptors(reloaded, {"imu"})
    assert len(payload) == 1
    assert payload[0]["anchor"]["id"] == "gamepad"
    assert payload[0]["anchor"]["path"] == "keymasq:2dc8:6012"
    assert payload[0]["path"] == "keymasq-source:imu"
    reloaded.input_sources[0].enabled = False
    manager.save_hardware(reloaded)
    assert configured_interface_descriptors(reloaded, {"imu"}) == []
    assert "imu" not in all_configured_interfaces(reloaded)
    assert not HardwareManager().get_hardware(hardware.hardware_id).input_sources[0].enabled


def test_add_motion_to_existing_controller_without_duplicating_evdev():
    hardware = HardwareConfig(
        "2dc8",
        "6012",
        "Ultimate 2",
        [
            EvdevDevice("keymasq:2dc8:6012", DeviceType.GAMEPAD, "controls", "usb-test/input0"),
        ],
        [],
    )
    detected = interfaces()[1:]
    selection = EvdevDeviceSelection(
        [], templates.build_motion_sensors(detected), templates.build_input_sources(detected)
    )
    assert append_evdev_device_selection(hardware, selection) == (1, 1)
    assert len(hardware.evdev_devices) == 1
    assert hardware.input_sources[0].companion_of == "controls"
    assert hardware.motion_sensors[0].source == hardware.input_sources[0].id
    assert append_evdev_device_selection(hardware, selection) == (0, 0)
