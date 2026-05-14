import importlib
import sys
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_config_dir_defaults_to_home_config(monkeypatch: pytest.MonkeyPatch) -> None:
    import keymasq.common.paths as paths

    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    reloaded = importlib.reload(paths)
    try:
        assert reloaded.CONFIG_DIR == Path.home() / ".config" / "keymasq"
        assert reloaded.HARDWARE_DIR == reloaded.CONFIG_DIR / "hardware"
        assert reloaded.PROFILES_DIR == reloaded.CONFIG_DIR / "profiles"
        assert reloaded.SUPERKEYS_DIR == reloaded.CONFIG_DIR / "superkeys"
        assert reloaded.SETTINGS_PATH == reloaded.CONFIG_DIR / "settings.toml"
        assert reloaded.ANALOG_CONTROLS_DIR == reloaded.CONFIG_DIR / "analog_controls"
        assert reloaded.VIRTUAL_DEVICES_PATH == reloaded.CONFIG_DIR / "virtual_devices.toml"
    finally:
        importlib.reload(paths)


def test_config_dir_honors_xdg_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import keymasq.common.paths as paths

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))

    reloaded = importlib.reload(paths)
    try:
        assert reloaded.XDG_CONFIG_HOME == tmp_path / "xdg-config"
        assert reloaded.CONFIG_DIR == tmp_path / "xdg-config" / "keymasq"
        assert reloaded.HARDWARE_DIR == reloaded.CONFIG_DIR / "hardware"
        assert reloaded.PROFILES_DIR == reloaded.CONFIG_DIR / "profiles"
        assert reloaded.SUPERKEYS_DIR == reloaded.CONFIG_DIR / "superkeys"
        assert reloaded.SETTINGS_PATH == reloaded.CONFIG_DIR / "settings.toml"
        assert reloaded.ANALOG_CONTROLS_DIR == reloaded.CONFIG_DIR / "analog_controls"
        assert reloaded.VIRTUAL_DEVICES_PATH == reloaded.CONFIG_DIR / "virtual_devices.toml"
    finally:
        importlib.reload(paths)


def test_resolve_keymasq_record_helper_path_uses_build_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import keymasq.common.paths as paths

    helper = tmp_path / "keymasq-record"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)

    monkeypatch.setitem(
        sys.modules,
        "keymasq.common.build_paths",
        SimpleNamespace(KEYMASQ_RECORD_HELPER_PATH=str(helper)),
    )

    reloaded = importlib.reload(paths)
    try:
        assert reloaded.KEYMASQ_RECORD_HELPER_PATH == helper
        assert reloaded.resolve_keymasq_record_helper_path() == str(helper)
    finally:
        monkeypatch.delitem(sys.modules, "keymasq.common.build_paths", raising=False)
        importlib.reload(paths)


def test_resolve_slurp_path_uses_build_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import keymasq.common.paths as paths

    slurp = tmp_path / "slurp"
    slurp.write_text("#!/bin/sh\n", encoding="utf-8")
    slurp.chmod(0o755)

    monkeypatch.setitem(
        sys.modules,
        "keymasq.common.build_paths",
        SimpleNamespace(
            KEYMASQ_RECORD_HELPER_PATH=str(tmp_path / "keymasq-record"),
            SLURP_PATH=str(slurp),
        ),
    )

    reloaded = importlib.reload(paths)
    try:
        assert reloaded.SLURP_PATH == slurp
        assert reloaded.resolve_slurp_path() == str(slurp)
    finally:
        monkeypatch.delitem(sys.modules, "keymasq.common.build_paths", raising=False)
        importlib.reload(paths)


def test_resolve_slurp_path_honors_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import keymasq.common.paths as paths

    slurp = tmp_path / "slurp"
    slurp.write_text("#!/bin/sh\n", encoding="utf-8")
    slurp.chmod(0o755)

    monkeypatch.setenv("SLURP_PATH", str(slurp))
    monkeypatch.setattr(paths, "SLURP_PATH", tmp_path / "missing-build-slurp")
    monkeypatch.setattr(paths, "SLURP_FALLBACK_PATHS", ())
    monkeypatch.setattr(paths.shutil, "which", lambda _name: None)

    assert paths.resolve_slurp_path() == str(slurp)


def test_resolve_slurp_path_empty_environment_override_disables_slurp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keymasq.common.paths as paths

    monkeypatch.setenv("SLURP_PATH", "")

    assert paths.resolve_slurp_path() is None


def test_resolve_slurp_path_uses_nixos_system_profile_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import keymasq.common.paths as paths

    nixos_slurp = tmp_path / "run-current-system-sw-bin-slurp"
    nixos_slurp.write_text("#!/bin/sh\n", encoding="utf-8")
    nixos_slurp.chmod(0o755)

    monkeypatch.delenv("SLURP_PATH", raising=False)
    monkeypatch.setattr(paths, "SLURP_PATH", tmp_path / "missing-build-slurp")
    monkeypatch.setattr(
        paths,
        "SLURP_FALLBACK_PATHS",
        (tmp_path / "missing-usr-bin-slurp", nixos_slurp),
    )
    monkeypatch.setattr(paths.shutil, "which", lambda _name: None)

    assert paths.resolve_slurp_path() == str(nixos_slurp)


def test_resolve_slurp_path_uses_path_lookup_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import keymasq.common.paths as paths

    path_slurp = tmp_path / "path-slurp"
    path_slurp.write_text("#!/bin/sh\n", encoding="utf-8")
    path_slurp.chmod(0o755)

    monkeypatch.delenv("SLURP_PATH", raising=False)
    monkeypatch.setattr(paths, "SLURP_PATH", tmp_path / "missing-build-slurp")
    monkeypatch.setattr(paths, "SLURP_FALLBACK_PATHS", ())
    monkeypatch.setattr(paths.shutil, "which", lambda _name: str(path_slurp))

    assert paths.resolve_slurp_path() == str(path_slurp)


def test_ensure_config_dirs_creates_expected_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import keymasq.common.paths as paths

    config_dir = tmp_path / "config"
    monkeypatch.setattr(paths, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(paths, "HARDWARE_DIR", config_dir / "hardware")
    monkeypatch.setattr(paths, "PROFILES_DIR", config_dir / "profiles")
    monkeypatch.setattr(paths, "SUPERKEYS_DIR", config_dir / "superkeys")
    monkeypatch.setattr(paths, "ANALOG_CONTROLS_DIR", config_dir / "analog_controls")

    paths.ensure_config_dirs()

    assert (config_dir / "hardware").is_dir()
    assert (config_dir / "profiles").is_dir()
    assert (config_dir / "superkeys").is_dir()
    assert (config_dir / "analog_controls").is_dir()


def test_ensure_session_socket_dir_logs_permission_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import keymasq.common.paths as paths

    session_socket = tmp_path / "runtime" / "keymasq" / "session.sock"
    monkeypatch.setattr(paths, "SESSION_SOCKET_PATH", session_socket)

    real_chmod = Path.chmod

    def _chmod(self: Path, mode: int) -> None:
        if self == session_socket.parent:
            raise OSError("no chmod")
        real_chmod(self, mode)

    monkeypatch.setattr(Path, "chmod", _chmod)

    with caplog.at_level("WARNING"):
        paths.ensure_session_socket_dir()

    assert session_socket.parent.is_dir()
    assert "Failed to set session socket directory permissions" in caplog.text


def test_resolve_keymasq_record_helper_path_returns_none_for_non_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import keymasq.common.paths as paths

    helper = tmp_path / "keymasq-record"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o644)

    monkeypatch.setattr(paths, "KEYMASQ_RECORD_HELPER_PATH", helper)
    monkeypatch.setattr(paths, "KEYMASQ_RECORD_HELPER_FALLBACK_PATHS", ())

    assert paths.resolve_keymasq_record_helper_path() is None


def test_resolve_keymasq_record_helper_path_uses_nixos_system_profile_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import keymasq.common.paths as paths

    nixos_helper = tmp_path / "run-current-system-sw-bin-keymasq-record"
    nixos_helper.write_text("#!/bin/sh\n", encoding="utf-8")
    nixos_helper.chmod(0o755)

    monkeypatch.setattr(paths, "KEYMASQ_RECORD_HELPER_PATH", tmp_path / "missing-build-helper")
    monkeypatch.setattr(
        paths,
        "KEYMASQ_RECORD_HELPER_FALLBACK_PATHS",
        (tmp_path / "missing-usr-bin-helper", nixos_helper),
    )

    assert paths.resolve_keymasq_record_helper_path() == str(nixos_helper)


def test_resolve_slurp_path_returns_none_for_non_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import keymasq.common.paths as paths

    slurp = tmp_path / "slurp"
    slurp.write_text("#!/bin/sh\n", encoding="utf-8")
    slurp.chmod(0o644)

    monkeypatch.delenv("SLURP_PATH", raising=False)
    monkeypatch.setattr(paths, "SLURP_PATH", slurp)
    monkeypatch.setattr(paths, "SLURP_FALLBACK_PATHS", ())
    monkeypatch.setattr(paths.shutil, "which", lambda _name: None)

    assert paths.resolve_slurp_path() is None


def test_gui_package_assets_exist() -> None:
    gui_dir = resources.files("keymasq").joinpath("gui")

    assert gui_dir.joinpath("style.css").is_file()
    assert gui_dir.joinpath("assets", "gamepad.svg").is_file()
    assert gui_dir.joinpath("assets", "keymasq-keyboard-symbolic.svg").is_file()
    assert gui_dir.joinpath("assets", "keymasq-mouse-symbolic.svg").is_file()
    assert gui_dir.joinpath("assets", "keymasq-combos-symbolic.svg").is_file()
