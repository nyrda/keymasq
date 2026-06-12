import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from keymasq.common import recording_guard


def test_unlock_path_helpers() -> None:
    runtime = recording_guard.runtime_unlock_path(1000)
    persistent = recording_guard.persistent_unlock_path(1000)

    assert runtime.name == "recording-unlock-1000"
    assert persistent.name == "recording-unlock-1000"


def test_macro_recording_path_helpers() -> None:
    runtime = recording_guard.runtime_macro_recording_path(1000)
    persistent = recording_guard.persistent_macro_recording_path(1000)

    assert runtime.name == "macro-recording-enabled-1000"
    assert persistent.name == "macro-recording-enabled-1000"


def test_parse_unlock_expires_at_invalid_or_missing(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    assert recording_guard.parse_unlock_expires_at(missing) is None

    invalid = tmp_path / "invalid"
    invalid.write_text("abc\n", encoding="utf-8")
    assert recording_guard.parse_unlock_expires_at(invalid) is None


def test_parse_unlock_expires_at_logs_read_failure(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lease = tmp_path / "lease"
    lease.touch()
    original_read_text = Path.read_text

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == lease:
            raise PermissionError("permission denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    caplog.set_level(logging.WARNING, logger=recording_guard.__name__)

    assert recording_guard.parse_unlock_expires_at(lease) is None
    assert "Failed to read recording unlock file" in caplog.text


def test_parse_unlock_expires_at_valid(tmp_path: Path) -> None:
    lease = tmp_path / "lease"
    lease.write_text("1234\n", encoding="utf-8")

    assert recording_guard.parse_unlock_expires_at(lease) == 1234


def test_is_unlock_value_active() -> None:
    assert recording_guard.is_unlock_value_active(0, now=100) is True
    assert recording_guard.is_unlock_value_active(101, now=100) is True
    assert recording_guard.is_unlock_value_active(99, now=100) is False


def test_resolve_unlock_status_prefers_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_dir = tmp_path / "run"
    persistent_dir = tmp_path / "etc"
    runtime_dir.mkdir()
    persistent_dir.mkdir()

    monkeypatch.setattr(recording_guard, "RECORDING_UNLOCK_RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(recording_guard, "RECORDING_UNLOCK_PERSISTENT_DIR", persistent_dir)

    runtime_path = runtime_dir / "recording-unlock-1000"
    persistent_path = persistent_dir / "recording-unlock-1000"
    runtime_path.write_text("105\n", encoding="utf-8")
    persistent_path.write_text("999\n", encoding="utf-8")

    status = recording_guard.resolve_unlock_status(1000, now=100)
    assert status["unlocked"] is True
    assert status["source"] == "runtime"
    assert status["expires_at"] == 105
    assert status["path"] == str(runtime_path)


def test_resolve_unlock_status_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime_dir = tmp_path / "run"
    persistent_dir = tmp_path / "etc"
    runtime_dir.mkdir()
    persistent_dir.mkdir()

    monkeypatch.setattr(recording_guard, "RECORDING_UNLOCK_RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(recording_guard, "RECORDING_UNLOCK_PERSISTENT_DIR", persistent_dir)

    status = recording_guard.resolve_unlock_status(42, now=100)
    assert status == {"unlocked": False, "source": "none", "expires_at": 0, "path": ""}


def test_resolve_unlock_status_marks_existing_unreadable_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_dir = tmp_path / "run"
    persistent_dir = tmp_path / "etc"
    runtime_dir.mkdir()
    persistent_dir.mkdir()

    monkeypatch.setattr(recording_guard, "RECORDING_UNLOCK_RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(recording_guard, "RECORDING_UNLOCK_PERSISTENT_DIR", persistent_dir)

    runtime_path = runtime_dir / "recording-unlock-1000"
    runtime_path.write_text("105\n", encoding="utf-8")
    original_read_text = Path.read_text

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == runtime_path:
            raise PermissionError("permission denied")
        return original_read_text(path, *args, **kwargs)

    def access(path: object, mode: int) -> bool:
        return Path(path) != runtime_path

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(recording_guard.os, "access", access)

    status = recording_guard.resolve_unlock_status(1000, now=100)

    assert status == {
        "unlocked": False,
        "source": "none",
        "expires_at": 0,
        "path": "",
        "unreadable": True,
    }


def test_resolve_macro_recording_status_uses_macro_recording_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "run"
    persistent_dir = tmp_path / "etc"
    runtime_dir.mkdir()
    persistent_dir.mkdir()

    monkeypatch.setattr(recording_guard, "RECORDING_UNLOCK_RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(recording_guard, "RECORDING_UNLOCK_PERSISTENT_DIR", persistent_dir)

    persistent_path = persistent_dir / "macro-recording-enabled-1000"
    persistent_path.write_text("0\n", encoding="utf-8")

    status = recording_guard.resolve_macro_recording_status(1000, now=100)
    assert status["unlocked"] is True
    assert status["source"] == "persistent"
    assert status["expires_at"] == 0
    assert status["path"] == str(persistent_path)


def test_write_unlock_expires_at_handles_chown_failure(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lease = tmp_path / "nested" / "lease"

    def _raise_chown(fd: int, uid: int, gid: int) -> None:
        raise OSError("nope")

    monkeypatch.setattr(recording_guard.os, "fchown", _raise_chown)

    caplog.set_level(logging.WARNING, logger=recording_guard.__name__)
    recording_guard.write_unlock_expires_at(lease, 123, owner_uid=1, owner_gid=1)

    assert lease.read_text(encoding="utf-8") == "123\n"
    assert "Failed to set recording unlock file owner" in caplog.text
    tmp_candidates = list(lease.parent.glob(f".{lease.name}.tmp-*"))
    assert tmp_candidates == []


def test_write_unlock_expires_at_accepts_keymasq_owned_parent_when_run_as_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lease = tmp_path / "lease"
    monkeypatch.setattr(recording_guard.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        recording_guard.pwd,
        "getpwnam",
        lambda name: SimpleNamespace(pw_uid=os.getuid()) if name == "keymasq" else None,
    )

    recording_guard.write_unlock_expires_at(lease, 123)

    assert lease.read_text(encoding="utf-8") == "123\n"


def test_write_unlock_expires_at_uses_shared_parent_directory_fsync_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lease = tmp_path / "lease"
    fsynced_paths: list[Path] = []

    def _fsync_parent_dir(path: Path) -> None:
        fsynced_paths.append(path)

    monkeypatch.setattr(recording_guard, "fsync_parent_dir", _fsync_parent_dir)

    recording_guard.write_unlock_expires_at(lease, 123)

    assert lease.read_text(encoding="utf-8") == "123\n"
    assert fsynced_paths == [lease]
