import math
from dataclasses import replace

import pytest

from keymasq.common.controller_capabilities import hardware_controller_template
from keymasq.common.model.core import DeviceType
from keymasq.common.model.hardware import EvdevDevice, HardwareConfig
from keymasq.common.model.motion import MotionSensorDefinition
from keymasq.gui.widgets.device_tab.hardware_settings_dialog import append_evdev_device_selection
from keymasq.gui.wizards.hardware_setup import discovery, templates
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
    assert payload[0]["type"] == "motion"
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


def deck_interfaces():
    return [
        {
            "id": "if02_joystick",
            "path": "/dev/input/event9",
            "config_path": "/dev/input/by-id/usb-Valve_Steam_Deck-if02-event-joystick",
            "device_types": ["gamepad"],
            "phys": "usb-0000:04:00.4-3/input2",
        },
        {
            "id": "event_if02",
            "path": "/dev/input/event10",
            "config_path": "/dev/input/by-id/usb-Valve_Steam_Deck-event-if02",
            "device_types": ["motion"],
            "phys": "usb-0000:04:00.4-3/input2",
        },
        {
            "id": "input",
            "path": "/dev/keymasq-sources/steam-deck-touch/0003:28DE:1205.0019/hid-bpf",
            "backend": "hid-bpf",
            "driver": "steam-deck-touch",
            "device_types": ["other"],
            "name": "Steam Deck stick touch",
            "phys": "usb-0000:04:00.4-3/input2",
            "companion_paths": ["/dev/input/event10", "/dev/input/event9"],
            "native_buttons": [
                {"evdev": "btn_touch_ls", "evdev_code": 768},
                {"evdev": "btn_touch_rs", "evdev_code": 769},
                {"evdev": "btn_touch_lp", "evdev_code": 770},
                {"evdev": "btn_touch_rp", "evdev_code": 771},
            ],
        },
    ]


def test_deck_setup_saves_touch_as_native_buttons(temp_config_dir):
    detected = deck_interfaces()
    hardware = HardwareConfig(
        "28de",
        "1205",
        "Steam Deck",
        templates.build_evdev_devices(detected),
        templates.build_native_buttons(detected),
        motion_sensors=[MotionSensorDefinition("motion_1", "Steam Motion Sensor", "event_if02")],
        input_sources=templates.build_input_sources(detected),
    )
    HardwareManager().save_hardware(hardware)
    reloaded = HardwareManager().get_hardware(hardware.hardware_id)
    assert reloaded is not None
    assert [(item.id, item.backend, item.companion_of) for item in reloaded.input_sources] == [
        ("input", "hid-bpf", "if02_joystick")
    ]
    assert [(item.id, item.label, item.evdev_code, item.source) for item in reloaded.buttons] == [
        ("btn_touch_ls", "LS Touch", 768, "input"),
        ("btn_touch_rs", "RS Touch", 769, "input"),
        ("btn_touch_lp", "LP Touch", 770, "input"),
        ("btn_touch_rp", "RP Touch", 771, "input"),
    ]
    (touch,) = configured_interface_descriptors(reloaded, {"input"})
    assert (touch["type"], touch["backend"]) == ("other", "hid-bpf")
    assert hardware_controller_template(reloaded).buttons == ()


def test_add_touch_to_configured_deck_pairs_with_its_gamepad():
    hardware = HardwareConfig(
        "28de",
        "1205",
        "Steam Deck",
        templates.build_evdev_devices(deck_interfaces()[:2]),
        [],
    )
    detected = deck_interfaces()[2:]
    selection = EvdevDeviceSelection(
        [],
        templates.build_motion_sensors(detected),
        [replace(source, companion_of=None) for source in templates.build_input_sources(detected)],
        templates.build_native_buttons(detected),
    )
    assert append_evdev_device_selection(hardware, selection) == (1, 0)
    assert hardware.input_sources[0].companion_of == "if02_joystick"
    assert [button.id for button in hardware.buttons] == [
        "btn_touch_ls",
        "btn_touch_rs",
        "btn_touch_lp",
        "btn_touch_rp",
    ]
    hardware.buttons = hardware.buttons[:2]
    assert append_evdev_device_selection(hardware, selection) == (0, 0)
    assert [(button.id, button.source) for button in hardware.buttons[2:]] == [
        ("btn_touch_lp", "input"),
        ("btn_touch_rp", "input"),
    ]
    assert len(hardware.input_sources) == 1


@pytest.mark.parametrize("configured", [False, True])
def test_wizard_groups_touch_source_with_its_controller(monkeypatch, temp_config_dir, configured):
    manager = HardwareManager()
    if configured:
        manager.save_hardware(
            HardwareConfig(
                "28de",
                "1205",
                "Steam Deck",
                templates.build_evdev_devices(deck_interfaces()[:2]),
                [],
            )
        )
    monkeypatch.setattr(
        discovery,
        "session_request",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "devices": [
                {
                    **{key: value for key, value in iface.items() if key != "config_path"},
                    "stable_path": iface.get("config_path", iface["path"]),
                    "name": iface.get("name", "Steam Deck"),
                    "vendor_id": "28de",
                    "product_id": "1205",
                }
                for iface in deck_interfaces()
            ],
        },
    )
    detected = {}
    discovery.detect_devices_via_session(
        detected, hardware_manager=manager, show_raw_evdev_devices=False
    )
    rows = [[iface["path"] for iface in row["interfaces"]] for row in detected.values()]
    if configured:
        assert rows == []
    else:
        assert len(rows) == 1
        assert sorted(rows[0]) == sorted(iface["path"] for iface in deck_interfaces())


@pytest.mark.parametrize("default_output", ["passthrough", "virtual-gamepad-1"])
def test_deck_tab_groups_unmapped_touch_inputs_without_output(default_output):
    pytest.importorskip("gi")
    from keymasq.common.model.hardware import ButtonDefinition
    from keymasq.gui.widgets.device_tab.tab import DeviceTab

    detected = deck_interfaces()
    hardware = HardwareConfig(
        "28de",
        "1205",
        "Steam Deck",
        templates.build_evdev_devices(detected),
        [
            ButtonDefinition("btn_thumbl", "LS", "btn_thumbl", source="if02_joystick"),
            ButtonDefinition("btn_thumbr", "RS", "btn_thumbr", source="if02_joystick"),
            *templates.build_native_buttons(detected),
        ],
        input_sources=templates.build_input_sources(detected),
        default_output=default_output,
    )
    tab = DeviceTab(hardware, None, demo_mode=True)
    widgets = tab._button_widgets
    assert widgets["btn_touch_ls"].get_parent() is widgets["btn_thumbl"].get_parent()
    assert widgets["btn_touch_rs"].get_parent() is widgets["btn_thumbr"].get_parent()
    assert widgets["btn_touch_lp"].get_parent() is widgets["btn_touch_rp"].get_parent()
    assert widgets["btn_touch_lp"].get_parent() is not widgets["btn_thumbl"].get_parent()
    for button_id in ("btn_touch_ls", "btn_touch_rs", "btn_touch_lp", "btn_touch_rp"):
        tab._update_button_display(button_id)
        assert widgets[button_id]._action_label.get_text() == "No output"


def test_removing_the_controller_interface_removes_its_touch_inputs(temp_config_dir):
    pytest.importorskip("gi")
    from keymasq.gui.widgets.device_tab.tab import DeviceTab

    detected = deck_interfaces()
    hardware = HardwareConfig(
        "28de",
        "1205",
        "Steam Deck",
        templates.build_evdev_devices(detected),
        templates.build_native_buttons(detected),
        input_sources=templates.build_input_sources(detected),
    )
    manager = HardwareManager()
    manager.save_hardware(hardware)
    tab = DeviceTab(hardware, None, hardware_manager=manager, demo_mode=True)
    joystick = next(device for device in hardware.evdev_devices if device.id == "if02_joystick")
    assert tab._delete_hardware_evdev_device(joystick, False)
    reloaded = manager.get_hardware(hardware.hardware_id)
    assert reloaded is not None
    assert reloaded.input_sources == []
    assert reloaded.buttons == []
