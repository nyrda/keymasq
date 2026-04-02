import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_resolve_keyforge_record_helper_path_uses_build_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import keyforge.common.paths as paths

    helper = tmp_path / "keyforge-record"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)

    monkeypatch.setitem(
        sys.modules,
        "keyforge.common.build_paths",
        SimpleNamespace(KEYFORGE_RECORD_HELPER_PATH=str(helper)),
    )

    reloaded = importlib.reload(paths)
    try:
        assert reloaded.KEYFORGE_RECORD_HELPER_PATH == helper
        assert reloaded.resolve_keyforge_record_helper_path() == str(helper)
    finally:
        monkeypatch.delitem(sys.modules, "keyforge.common.build_paths", raising=False)
        importlib.reload(paths)


def test_resolve_slurp_path_uses_build_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import keyforge.common.paths as paths

    slurp = tmp_path / "slurp"
    slurp.write_text("#!/bin/sh\n", encoding="utf-8")
    slurp.chmod(0o755)

    monkeypatch.setitem(
        sys.modules,
        "keyforge.common.build_paths",
        SimpleNamespace(
            KEYFORGE_RECORD_HELPER_PATH=str(tmp_path / "keyforge-record"),
            SLURP_PATH=str(slurp),
        ),
    )

    reloaded = importlib.reload(paths)
    try:
        assert reloaded.SLURP_PATH == slurp
        assert reloaded.resolve_slurp_path() == str(slurp)
    finally:
        monkeypatch.delitem(sys.modules, "keyforge.common.build_paths", raising=False)
        importlib.reload(paths)


def test_ensure_config_dirs_creates_expected_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import keyforge.common.paths as paths

    config_dir = tmp_path / "config"
    monkeypatch.setattr(paths, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(paths, "HARDWARE_DIR", config_dir / "hardware")
    monkeypatch.setattr(paths, "PROFILES_DIR", config_dir / "profiles")
    monkeypatch.setattr(paths, "SUPERKEYS_DIR", config_dir / "superkeys")
    monkeypatch.setattr(paths, "MACROS_DIR", config_dir / "macros")

    paths.ensure_config_dirs()

    assert (config_dir / "hardware").is_dir()
    assert (config_dir / "profiles").is_dir()
    assert (config_dir / "superkeys").is_dir()
    assert (config_dir / "macros").is_dir()


def test_ensure_session_socket_dir_logs_permission_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import keyforge.common.paths as paths

    session_socket = tmp_path / "runtime" / "keyforge" / "session.sock"
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


def test_resolve_keyforge_record_helper_path_returns_none_for_non_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import keyforge.common.paths as paths

    helper = tmp_path / "keyforge-record"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o644)

    monkeypatch.setattr(paths, "KEYFORGE_RECORD_HELPER_PATH", helper)

    assert paths.resolve_keyforge_record_helper_path() is None


def test_resolve_slurp_path_returns_none_for_non_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import keyforge.common.paths as paths

    slurp = tmp_path / "slurp"
    slurp.write_text("#!/bin/sh\n", encoding="utf-8")
    slurp.chmod(0o644)

    monkeypatch.setattr(paths, "SLURP_PATH", slurp)

    assert paths.resolve_slurp_path() is None
