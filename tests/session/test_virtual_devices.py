import logging

import pytest


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


def test_virtual_gamepad_output_ids_match_validator_contract() -> None:
    from keymasq.common.virtual_devices import (
        MAX_VIRTUAL_GAMEPADS,
        is_virtual_gamepad_output_id,
        virtual_gamepad_output_id,
    )

    for index in range(1, MAX_VIRTUAL_GAMEPADS + 1):
        output_id = virtual_gamepad_output_id(index)
        assert output_id == f"virtual-gamepad-{index}"
        assert is_virtual_gamepad_output_id(output_id)

    with pytest.raises(ValueError):
        virtual_gamepad_output_id(0)
    assert not is_virtual_gamepad_output_id("virtual-gamepad-0")
    assert not is_virtual_gamepad_output_id("virtual-gamepad-01")
    assert not is_virtual_gamepad_output_id("virtual-gamepad-+1")
    assert not is_virtual_gamepad_output_id("virtual-gamepad-1 ")


def test_global_settings_round_trips_keyboard_layout(tmp_path, monkeypatch) -> None:
    from keymasq.common import paths, xkb
    from keymasq.common.settings import GlobalSettings
    from keymasq.session import settings

    if not xkb.is_available():
        pytest.skip("libxkbcommon unavailable")
    config_dir = tmp_path / "keymasq"
    monkeypatch.setattr(paths, "CONFIG_DIR", config_dir)

    assert settings.load_keyboard_layout() == "us"
    saved = settings.save_global_settings(
        GlobalSettings(virtual_gamepad_count=2, keyboard_layout=" de(nodeadkeys) ")
    )
    assert saved.keyboard_layout == "de(nodeadkeys)"
    assert (config_dir / "settings.toml").read_text().count('layout = "de(nodeadkeys)"') == 1
    assert settings.load_keyboard_layout() == "de(nodeadkeys)"

    # Saving only the gamepad count keeps the layout.
    assert settings.save_virtual_gamepad_count(3) == 3
    assert settings.load_global_settings() == GlobalSettings(
        virtual_gamepad_count=3, keyboard_layout="de(nodeadkeys)"
    )


def test_global_settings_keep_unknown_keyboard_layout(tmp_path, monkeypatch) -> None:
    """Loading never replaces the configured layout; using it reports the error."""
    from keymasq.common import paths
    from keymasq.session import settings

    config_dir = tmp_path / "keymasq"
    config_dir.mkdir()
    (config_dir / "settings.toml").write_text('[keyboard]\nlayout = "nonsense"\n', encoding="utf-8")
    monkeypatch.setattr(paths, "CONFIG_DIR", config_dir)

    assert settings.load_global_settings().keyboard_layout == "nonsense"
    # Saving another setting does not overwrite the user's value with the default.
    assert settings.save_virtual_gamepad_count(2) == 2
    assert (config_dir / "settings.toml").read_text().count('layout = "nonsense"') == 1
