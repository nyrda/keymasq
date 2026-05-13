import logging


def test_virtual_gamepad_config_defaults_and_clamps(tmp_path, monkeypatch) -> None:
    from keymasq.common import paths
    from keymasq.session import virtual_devices

    config_dir = tmp_path / "keymasq"
    monkeypatch.setattr(paths, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(
        virtual_devices,
        "VIRTUAL_DEVICES_PATH",
        config_dir / "virtual_devices.toml",
    )

    assert virtual_devices.load_virtual_gamepad_count() == 1
    assert virtual_devices.save_virtual_gamepad_count(-2) == 0
    assert virtual_devices.load_virtual_gamepad_count() == 0
    assert virtual_devices.save_virtual_gamepad_count(9) == 4
    assert virtual_devices.load_virtual_gamepad_count() == 4


def test_virtual_gamepad_config_malformed_defaults(tmp_path, monkeypatch, caplog) -> None:
    from keymasq.common import paths
    from keymasq.session import virtual_devices

    config_dir = tmp_path / "keymasq"
    config_dir.mkdir()
    config_path = config_dir / "virtual_devices.toml"
    config_path.write_text("[gamepads\n", encoding="utf-8")
    monkeypatch.setattr(paths, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(virtual_devices, "VIRTUAL_DEVICES_PATH", config_path)

    with caplog.at_level(logging.WARNING, logger="keymasq-session.virtual-devices"):
        assert virtual_devices.load_virtual_gamepad_count() == 1
    assert "Failed to load virtual device config" in caplog.text
