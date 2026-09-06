import pytest

pytest.importorskip("gi")

from keymasq.common.model.core import DeviceType
from keymasq.common.model.hardware import EvdevDevice, HardwareConfig
from keymasq.common.model.motion import MotionAxisDefinition, MotionSensorDefinition
from keymasq.gui.window import chrome, device_tabs
from keymasq.gui.wizards.hardware_setup.types import EvdevDeviceSelection


def _hardware(*, gamepad: bool, motion: bool) -> HardwareConfig:
    return HardwareConfig(
        vendor_id="057e",
        product_id="2009",
        name="Pro Controller",
        evdev_devices=[
            EvdevDevice(
                path="keymasq:057e:2009",
                device_type=DeviceType.GAMEPAD if gamepad else DeviceType.MOTION,
                id="gamepad" if gamepad else "imu",
            )
        ],
        buttons=[],
        motion_sensors=(
            [MotionSensorDefinition(id="imu", label="Nintendo Motion Sensor")] if motion else []
        ),
    )


def test_motion_controls_require_controller_and_motion_hardware() -> None:
    assert chrome._motion_controls_available([]) is False
    assert chrome._motion_controls_available([_hardware(gamepad=True, motion=False)]) is False
    assert chrome._motion_controls_available([_hardware(gamepad=False, motion=True)]) is False
    assert chrome._motion_controls_available([_hardware(gamepad=True, motion=True)]) is True


def test_motion_controls_menu_tracks_added_and_removed_hardware() -> None:
    from keymasq.gui.window.core import MainWindow

    window = MainWindow(demo_mode=True)
    button = window._menu_motion_controls_btn
    assert button is not None
    assert button.get_visible() is False

    hardware = _hardware(gamepad=True, motion=True)
    device_tabs._add_device_tab(window, hardware)
    assert button.get_visible() is True

    device_tabs.remove_device_tab(window, hardware.hardware_id)
    assert button.get_visible() is False


def test_attaching_motion_refreshes_controller_tab_and_menu(
    monkeypatch,
    temp_config_dir,
) -> None:
    from keymasq.gui.widgets.device_tab import tab as device_tab_module
    from keymasq.gui.widgets.device_tab.tab import DeviceTab
    from keymasq.gui.window.core import MainWindow

    window = MainWindow(demo_mode=True)
    button = window._menu_motion_controls_btn
    assert button is not None

    hardware = _hardware(gamepad=True, motion=False)
    device_tabs._add_device_tab(window, hardware)
    page = window._device_pages[hardware.hardware_id]
    tab = page.get_child()
    assert isinstance(tab, DeviceTab)
    assert "motion_1" not in tab._button_widgets
    assert button.get_visible() is False

    monkeypatch.setattr(
        device_tab_module,
        "session_request_async",
        lambda _payload, _callback: None,
    )
    selection = EvdevDeviceSelection(
        [
            EvdevDevice(
                path="/dev/input/by-id/usb-Pro_Controller-event-if03",
                device_type=DeviceType.MOTION,
                id="imu",
            )
        ],
        [
            MotionSensorDefinition(
                id="motion_1",
                label="Nintendo Motion Sensor",
                source="imu",
                driver="hid-nintendo",
                gyro_axes=[MotionAxisDefinition(role="yaw", evdev="abs_rz")],
            )
        ],
    )

    added, _message, error = tab._add_hardware_evdev_devices(selection)

    assert added == 1
    assert error is False
    assert "motion_1" in tab._button_widgets
    assert button.get_visible() is True

    motion_evdev = next(
        interface
        for interface in hardware.evdev_devices
        if interface.device_type == DeviceType.MOTION
    )
    assert tab._delete_hardware_evdev_device(motion_evdev, False) is True
    assert "motion_1" not in tab._button_widgets
    assert button.get_visible() is False
