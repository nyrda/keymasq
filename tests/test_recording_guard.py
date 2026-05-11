import logging
from pathlib import Path

import pytest

from keymasq.common import recording_guard


def test_unlock_path_helpers() -> None:
    runtime = recording_guard.runtime_unlock_path(1000)
    persistent = recording_guard.persistent_unlock_path(1000)

    assert runtime.name == "recording-unlock-1000"
    assert persistent.name == "recording-unlock-1000"


def test_parse_unlock_expires_at_invalid_or_missing(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    assert recording_guard.parse_unlock_expires_at(missing) is None

    invalid = tmp_path / "invalid"
    invalid.write_text("abc\n", encoding="utf-8")
    assert recording_guard.parse_unlock_expires_at(invalid) is None


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


def test_write_unlock_expires_at_handles_chown_failure(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lease = tmp_path / "nested" / "lease"

    def _raise_chown(path: str, uid: int, gid: int) -> None:
        raise OSError("nope")

    monkeypatch.setattr(recording_guard.os, "chown", _raise_chown)

    caplog.set_level(logging.WARNING, logger=recording_guard.__name__)
    recording_guard.write_unlock_expires_at(lease, 123, owner_uid=1, owner_gid=1)

    assert lease.read_text(encoding="utf-8") == "123\n"
    assert "Failed to set recording unlock file owner" in caplog.text
    tmp_candidates = list(lease.parent.glob(f".{lease.name}.tmp-*"))
    assert tmp_candidates == []
