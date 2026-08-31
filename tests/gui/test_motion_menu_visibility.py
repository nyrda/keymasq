import pytest

pytest.importorskip("gi")

from keymasq.common.model.core import DeviceType
from keymasq.common.model.hardware import EvdevDevice, HardwareConfig
from keymasq.common.model.motion import MotionSensorDefinition
from keymasq.gui.window import chrome, device_tabs


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
