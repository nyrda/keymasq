import logging


def test_global_settings_defaults_clamps_and_persists(tmp_path, monkeypatch) -> None:
    from keymasq.common import paths
    from keymasq.session import settings

    config_dir = tmp_path / "keymasq"
    monkeypatch.setattr(paths, "CONFIG_DIR", config_dir)

    loaded = settings.load_global_settings()
    assert loaded.virtual_gamepad_count == 1
    assert settings.save_virtual_gamepad_count(-2) == 0
    assert settings.load_global_settings().virtual_gamepad_count == 0
    assert settings.save_virtual_gamepad_count(9) == 4
    assert settings.load_global_settings().virtual_gamepad_count == 4


def test_global_settings_saves_virtual_gamepad_count(tmp_path, monkeypatch) -> None:
    from keymasq.common import paths
    from keymasq.common.settings import GlobalSettings
    from keymasq.session import settings

    config_dir = tmp_path / "keymasq"
    config_dir.mkdir()
    monkeypatch.setattr(paths, "CONFIG_DIR", config_dir)

    settings.save_global_settings(GlobalSettings(virtual_gamepad_count=2))

    loaded = settings.load_global_settings()
    assert loaded.virtual_gamepad_count == 2


def test_global_settings_malformed_defaults(tmp_path, monkeypatch, caplog) -> None:
    from keymasq.common import paths
    from keymasq.session import settings

    config_dir = tmp_path / "keymasq"
    config_dir.mkdir()
    config_path = config_dir / "settings.toml"
    config_path.write_text("[gamepads\n", encoding="utf-8")
    monkeypatch.setattr(paths, "CONFIG_DIR", config_dir)

    with caplog.at_level(logging.WARNING, logger="keymasq-session.settings"):
        assert settings.load_global_settings().virtual_gamepad_count == 1
    assert "Failed to load settings" in caplog.text
