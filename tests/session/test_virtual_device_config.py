import logging

from keymasq.common.virtual_device_templates import VirtualDeviceConfig


def test_virtual_device_config_persists(temp_config_dir) -> None:
    from keymasq.common.virtual_device_templates import virtual_device_config_from_toml
    from keymasq.session.virtual_devices import (
        load_virtual_device_config,
        save_virtual_device_config,
    )

    config = virtual_device_config_from_toml(
        {
            "devices": [
                {
                    "output_id": "flight-stick",
                    "template": "logitech-extreme-3d-pro",
                }
            ]
        }
    )

    assert save_virtual_device_config(config) == config
    assert load_virtual_device_config() == config
    text = (temp_config_dir / "virtual_devices.toml").read_text(encoding="utf-8")
    assert 'output_id = "flight-stick"' in text
    assert 'template = "logitech-extreme-3d-pro"' in text


def test_virtual_device_config_invalid_file_falls_back(
    temp_config_dir,
    caplog,
) -> None:
    from keymasq.session.virtual_devices import load_virtual_device_config

    (temp_config_dir / "virtual_devices.toml").write_text("devices = nope", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="keymasq-session.virtual-devices"):
        assert load_virtual_device_config() == VirtualDeviceConfig()
    assert "Failed to load virtual devices" in caplog.text
