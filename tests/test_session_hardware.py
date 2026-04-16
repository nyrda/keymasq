import logging

import pytest

from keymasq.common.models import ButtonDefinition, DeviceType, EvdevDevice, HardwareConfig
from keymasq.session.hardware import HardwareManager


def test_hardware_manager_loads_existing_configs(temp_config_dir) -> None:
    config_path = temp_config_dir / "hardware" / "1234_5678.toml"
    config_path.write_text(
        """
[hardware]
name = "Gaming Mouse"
vendor_id = "1234"
product_id = "5678"
image = "mouse.svg"

[hardware.evdev]
devices = [
  { path = "/dev/input/event10", type = "mouse", id = "usb0", capabilities = ["btn_left"] },
]

[hardware.layout]

[[hardware.layout.buttons]]
id = "btn_left"
label = "Left"
evdev = "btn_left"
source = "evdev"
zone = "main"
row = 1
col = 2
type = "button"
""".strip(),
        encoding="utf-8",
    )

    manager = HardwareManager()
    config = manager.get_hardware("1234:5678")

    assert config is not None
    assert config.name == "Gaming Mouse"
    assert config.image == "mouse.svg"
    assert config.evdev_devices == [
        EvdevDevice(
            path="/dev/input/event10",
            device_type=DeviceType.MOUSE,
            id="usb0",
            capabilities=["btn_left"],
        )
    ]
    assert config.buttons == [
        ButtonDefinition(
            id="btn_left",
            label="Left",
            evdev="btn_left",
            source="evdev",
            zone="main",
            row=1,
            col=2,
            type="button",
        )
    ]
    assert manager.list_hardware_ids() == ["1234:5678"]


def test_hardware_manager_skips_invalid_files(
    temp_config_dir, caplog: pytest.LogCaptureFixture
) -> None:
    (temp_config_dir / "hardware" / "broken.toml").write_text("not = [valid", encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        manager = HardwareManager()

    assert manager.list_hardware() == []
    assert "Failed to load" in caplog.text


def test_hardware_manager_save_load_and_delete_round_trip(temp_config_dir) -> None:
    manager = HardwareManager()
    config = HardwareConfig(
        vendor_id="1111",
        product_id="2222",
        name="Test Mouse",
        evdev_devices=[
            EvdevDevice(
                path="/dev/input/event99",
                device_type=DeviceType.MOUSE,
                id="mouse0",
                capabilities=["btn_left", "rel_x"],
            )
        ],
        buttons=[
            ButtonDefinition(
                id="btn_left",
                label="Left Click",
                evdev="btn_left",
                source="evdev",
                zone="main",
                row=0,
                col=0,
                type="button",
            )
        ],
        image="mouse.png",
    )

    manager.save_hardware(config)

    saved_path = temp_config_dir / "hardware" / "1111_2222.toml"
    text = saved_path.read_text(encoding="utf-8")
    assert 'type = "mouse"' in text
    assert 'image = "mouse.png"' in text
    assert 'source = "evdev"' in text
    assert 'zone = "main"' in text

    reloaded = HardwareManager().get_hardware("1111:2222")
    assert reloaded == config

    assert manager.delete_hardware("1111:2222") is True
    assert saved_path.exists() is False
    assert manager.delete_hardware("1111:2222") is False


def test_hardware_manager_save_keyboard_layout_appends_helper_comments(temp_config_dir) -> None:
    manager = HardwareManager()
    config = HardwareConfig(
        vendor_id="aaaa",
        product_id="bbbb",
        name="Compact Keyboard",
        evdev_devices=[EvdevDevice(path="/dev/input/event42", device_type=DeviceType.KEYBOARD)],
        buttons=[
            ButtonDefinition(id=f"key_{index}", label=f"Key {index}", evdev=f"key_{index}")
            for index in range(40)
        ],
    )

    manager.save_hardware(config)

    text = (temp_config_dir / "hardware" / "aaaa_bbbb.toml").read_text(encoding="utf-8")
    assert 'type = "keyboard"' in text
    assert "# Optional special keys" in text
    assert 'id = "key_volumeup"' in text


def test_hardware_manager_saves_gamepad_layout_type(temp_config_dir) -> None:
    manager = HardwareManager()
    config = HardwareConfig(
        vendor_id="9999",
        product_id="0001",
        name="Test Gamepad",
        evdev_devices=[EvdevDevice(path="/dev/input/event50", device_type=DeviceType.GAMEPAD)],
        buttons=[
            ButtonDefinition(
                id="btn_south",
                label="A",
                evdev="btn_south",
                evdev_code=304,
                source="joystick",
            ),
            ButtonDefinition(
                id="btn_east",
                label="B",
                evdev="btn_east",
                evdev_code=305,
                source="joystick",
            ),
        ],
    )

    manager.save_hardware(config)

    text = (temp_config_dir / "hardware" / "9999_0001.toml").read_text(encoding="utf-8")
    assert 'type = "gamepad"' in text
    assert "evdev_code = 304" in text
    assert HardwareManager().get_hardware("9999:0001") == config
