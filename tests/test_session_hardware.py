import logging
from typing import BinaryIO

import pytest

import keymasq.session.hardware as hardware_module
from keymasq.common.models import (
    AnalogAxisDefinition,
    AnalogInputDefinition,
    ButtonDefinition,
    DeviceType,
    EvdevDevice,
    HardwareConfig,
)
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

[[hardware.evdev.devices]]
path = "/dev/input/event10"
type = "mouse"
id = "usb0"
phys = "usb-test/input0"
capabilities = ["btn_left"]

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
            phys="usb-test/input0",
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


def test_hardware_manager_reload_removes_deleted_configs(temp_config_dir) -> None:
    config_path = temp_config_dir / "hardware" / "1234_5678.toml"
    config_path.write_text(
        """
[hardware]
name = "Gaming Mouse"
vendor_id = "1234"
product_id = "5678"

[hardware.evdev]
devices = []

[hardware.layout]
buttons = []
""".strip(),
        encoding="utf-8",
    )

    manager = HardwareManager()
    assert manager.get_hardware("1234:5678") is not None

    config_path.unlink()
    manager.reload()

    assert manager.get_hardware("1234:5678") is None
    assert manager.list_hardware_ids() == []


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
                phys="usb-test/input0",
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
        analog_inputs=[
            AnalogInputDefinition(
                id="left_stick",
                label="Left Stick",
                type="stick",
                source="joystick",
                axes=[
                    AnalogAxisDefinition(role="x", evdev="abs_x", evdev_code=0),
                    AnalogAxisDefinition(role="y", evdev="abs_y", evdev_code=1),
                ],
            ),
            AnalogInputDefinition(
                id="left_trigger",
                label="Left Trigger",
                type="axis",
                source="joystick",
                axes=[
                    AnalogAxisDefinition(role="x", evdev="abs_z", evdev_code=2),
                ],
            ),
        ],
        image="mouse.png",
    )

    manager.save_hardware(config)

    saved_path = temp_config_dir / "hardware" / "1111_2222.toml"
    text = saved_path.read_text(encoding="utf-8")
    assert 'type = "mouse"' in text
    assert 'phys = "usb-test/input0"' in text
    assert 'image = "mouse.png"' in text
    assert 'source = "evdev"' in text
    assert 'id = "left_stick"' in text
    assert 'id = "left_trigger"' in text
    assert 'type = "axis"' in text
    assert 'evdev = "abs_x"' in text
    assert 'evdev = "abs_z"' in text
    assert 'zone = "main"' in text

    reloaded = HardwareManager().get_hardware("1111:2222")
    assert reloaded == config

    assert manager.delete_hardware("1111:2222") is True
    assert saved_path.exists() is False
    assert manager.delete_hardware("1111:2222") is False


def test_hardware_manager_preserves_keymasq_logical_path(temp_config_dir) -> None:
    manager = HardwareManager()
    config = HardwareConfig(
        vendor_id="2dc8",
        product_id="3106",
        name="Bluetooth Pad",
        evdev_devices=[
            EvdevDevice(
                path="keymasq:2dc8:3106",
                device_type=DeviceType.GAMEPAD,
                id="gamepad",
                capabilities=["btn_south", "abs_x"],
            ),
            EvdevDevice(
                path="/dev/input/by-path/pci-test-event-joystick",
                device_type=DeviceType.GAMEPAD,
                id="manual_path",
            ),
        ],
        buttons=[],
    )

    manager.save_hardware(config)

    text = (temp_config_dir / "hardware" / "2dc8_3106.toml").read_text(encoding="utf-8")
    assert 'path = "keymasq:2dc8:3106"' in text
    assert 'path = "/dev/input/by-path/pci-test-event-joystick"' in text
    assert HardwareManager().get_hardware("2dc8:3106") == config


def test_hardware_manager_preserves_explicit_hardware_id(temp_config_dir) -> None:
    manager = HardwareManager()
    config = HardwareConfig(
        vendor_id="045e",
        product_id="02a1",
        name="Xbox Receiver Player 2",
        evdev_devices=[
            EvdevDevice(
                path="/dev/input/by-id/xbox-if02-event-joystick",
                device_type=DeviceType.GAMEPAD,
                id="if02_joystick",
            )
        ],
        buttons=[],
        id="045e:02a1@2",
    )

    manager.save_hardware(config)

    saved_files = list((temp_config_dir / "hardware").glob("*.toml"))
    assert len(saved_files) == 1
    assert saved_files[0].name == "045e_02a1_2.toml"
    assert manager.get_hardware(config.hardware_id) == config
    assert manager.get_hardware("045e:02a1") is None

    reloaded = HardwareManager().get_hardware(config.hardware_id)
    assert reloaded == config


def test_hardware_manager_suffixes_colliding_sanitized_hardware_ids(temp_config_dir) -> None:
    manager = HardwareManager()
    first = HardwareConfig(
        vendor_id="1234",
        product_id="5678",
        name="USB Path",
        evdev_devices=[],
        buttons=[],
        id="1234:5678@usb/foo",
    )
    second = HardwareConfig(
        vendor_id="1234",
        product_id="5678",
        name="USB Underscore",
        evdev_devices=[],
        buttons=[],
        id="1234:5678@usb_foo",
    )

    manager.save_hardware(first)
    manager.save_hardware(second)

    saved_names = sorted(path.name for path in (temp_config_dir / "hardware").glob("*.toml"))
    assert saved_names == ["1234_5678_usb_foo.toml", "1234_5678_usb_foo_2.toml"]

    reloaded = HardwareManager()
    assert reloaded.get_hardware(first.hardware_id) == first
    assert reloaded.get_hardware(second.hardware_id) == second

    assert reloaded.delete_hardware(second.hardware_id) is True
    assert sorted(path.name for path in (temp_config_dir / "hardware").glob("*.toml")) == [
        "1234_5678_usb_foo.toml"
    ]
    assert HardwareManager().get_hardware(first.hardware_id) == first


def test_hardware_manager_cleans_reserved_path_when_save_fails(
    temp_config_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HardwareManager()
    config = HardwareConfig(
        vendor_id="1234",
        product_id="5678",
        name="Broken Save",
        evdev_devices=[],
        buttons=[],
    )

    def fail_dump(_data: object, _file: BinaryIO) -> None:
        raise RuntimeError("dump failed")

    monkeypatch.setattr(hardware_module.tomli_w, "dump", fail_dump)

    with pytest.raises(RuntimeError, match="dump failed"):
        manager.save_hardware(config)

    assert not (temp_config_dir / "hardware" / "1234_5678.toml").exists()


def test_hardware_manager_rejects_mismatched_explicit_hardware_id(
    temp_config_dir,
    caplog: pytest.LogCaptureFixture,
) -> None:
    (temp_config_dir / "hardware" / "bad_id.toml").write_text(
        """
[hardware]
name = "Bad Hardware"
vendor_id = "045e"
product_id = "02a1"
hardware_id = "1234:5678@2"

[hardware.evdev]
devices = []

[hardware.layout]
buttons = []
""".strip(),
        encoding="utf-8",
    )

    with caplog.at_level(logging.ERROR):
        manager = HardwareManager()

    assert manager.list_hardware() == []
    assert "hardware_id '1234:5678@2' does not match vendor/product '045e:02a1'" in caplog.text


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
