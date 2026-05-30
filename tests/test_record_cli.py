import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from keymasq import record


def test_require_privileged_caller_allows_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(record.os, "geteuid", lambda: 0)
    record._require_privileged_caller()


def test_require_privileged_caller_rejects_other_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(record.os, "geteuid", lambda: 1234)
    monkeypatch.setattr(record.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=999))

    with pytest.raises(PermissionError):
        record._require_privileged_caller()


def test_require_privileged_caller_allows_keymasq_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(record.os, "geteuid", lambda: 777)
    monkeypatch.setattr(record.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=777))
    record._require_privileged_caller()


def test_write_lease_and_remove_lease(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lease = tmp_path / "lease"
    monkeypatch.setattr(
        record.pwd,
        "getpwnam",
        lambda _: SimpleNamespace(pw_uid=1000, pw_gid=1000),
    )

    calls: list[tuple[int, int]] = []

    def _chown(fd: int, uid: int, gid: int) -> None:
        calls.append((uid, gid))

    monkeypatch.setattr(record.os, "fchown", _chown)

    record._write_lease(lease, 42)
    assert lease.read_text(encoding="utf-8") == "42\n"
    assert calls == [(1000, 1000)]

    record._remove_lease(lease)
    assert lease.exists() is False


def test_write_lease_rejects_preexisting_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "target"
    target.write_text("keep\n", encoding="utf-8")
    lease = tmp_path / "lease"
    lease.symlink_to(target)
    monkeypatch.setattr(
        record.pwd,
        "getpwnam",
        lambda _: SimpleNamespace(pw_uid=1000, pw_gid=1000),
    )

    with pytest.raises(PermissionError, match="symlink"):
        record._write_lease(lease, 42)

    assert target.read_text(encoding="utf-8") == "keep\n"
    assert lease.is_symlink()


def test_write_lease_keeps_existing_lease_when_replace_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lease = tmp_path / "lease"
    lease.write_text("41\n", encoding="utf-8")
    monkeypatch.setattr(
        record.pwd,
        "getpwnam",
        lambda _: SimpleNamespace(pw_uid=1000, pw_gid=1000),
    )

    def _replace(_src: Path, _dst: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(record.os, "replace", _replace)

    with pytest.raises(OSError, match="replace failed"):
        record._write_lease(lease, 42)

    assert lease.read_text(encoding="utf-8") == "41\n"
    assert list(tmp_path.glob(".lease.tmp-*")) == []


def test_main_status_prints_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["keymasq-record", "status", "--uid", "1000"])
    monkeypatch.setattr(record, "_require_privileged_caller", lambda: None)
    monkeypatch.setattr(
        record,
        "resolve_unlock_status",
        lambda uid: {"unlocked": True, "source": "runtime", "expires_at": 1, "path": "/tmp/x"},
    )

    record.main()

    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["status"] == "ok"
    assert payload["source"] == "runtime"


def test_main_unlock_runtime_replaces_previous_expiry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime_path = tmp_path / "runtime-lease"
    runtime_path.write_text("20\n", encoding="utf-8")
    writes: list[tuple[Path, int]] = []

    monkeypatch.setattr(
        sys,
        "argv",
        ["keymasq-record", "unlock-runtime", "--uid", "1000", "--ttl", "5"],
    )
    monkeypatch.setattr(record, "_require_privileged_caller", lambda: None)
    monkeypatch.setattr(record, "runtime_unlock_path", lambda uid: runtime_path)
    monkeypatch.setattr(record.time, "time", lambda: 10)

    def _write(path: Path, expires_at: int) -> None:
        writes.append((path, expires_at))

    monkeypatch.setattr(record, "_write_lease", _write)

    record.main()

    assert writes == [(runtime_path, 15)]
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["expires_at"] == 15


def test_main_unlock_runtime_extends_with_requested_ttl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime_path = tmp_path / "runtime-lease"
    runtime_path.write_text("12\n", encoding="utf-8")
    writes: list[tuple[Path, int]] = []

    monkeypatch.setattr(
        sys,
        "argv",
        ["keymasq-record", "unlock-runtime", "--uid", "1000", "--ttl", "20"],
    )
    monkeypatch.setattr(record, "_require_privileged_caller", lambda: None)
    monkeypatch.setattr(record, "runtime_unlock_path", lambda uid: runtime_path)
    monkeypatch.setattr(record.time, "time", lambda: 10)

    def _write(path: Path, expires_at: int) -> None:
        writes.append((path, expires_at))

    monkeypatch.setattr(record, "_write_lease", _write)

    record.main()

    assert writes == [(runtime_path, 30)]
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["expires_at"] == 30


def test_main_unlock_runtime_ttl_zero_writes_non_expiring_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime_path = tmp_path / "runtime-lease"
    writes: list[tuple[Path, int]] = []

    monkeypatch.setattr(
        sys,
        "argv",
        ["keymasq-record", "unlock-runtime", "--uid", "1000", "--ttl", "0"],
    )
    monkeypatch.setattr(record, "_require_privileged_caller", lambda: None)
    monkeypatch.setattr(record, "runtime_unlock_path", lambda uid: runtime_path)

    def _write(path: Path, expires_at: int) -> None:
        writes.append((path, expires_at))

    monkeypatch.setattr(record, "_write_lease", _write)

    record.main()

    assert writes == [(runtime_path, 0)]
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["expires_at"] == 0


def test_main_lock_runtime_removes_runtime_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime_path = tmp_path / "runtime-lease"
    runtime_path.write_text("20\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["keymasq-record", "lock-runtime", "--uid", "1000"])
    monkeypatch.setattr(record, "_require_privileged_caller", lambda: None)
    monkeypatch.setattr(record, "runtime_unlock_path", lambda uid: runtime_path)

    record.main()

    assert runtime_path.exists() is False
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {"status": "ok", "scope": "runtime", "locked": True}


def test_main_rejects_persistent_unlock_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["keymasq-record", "unlock-persistent", "--uid", "1000", "--ttl", "86400"],
    )

    with pytest.raises(SystemExit) as excinfo:
        record.main()

    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_main_error_emits_json_and_exits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["keymasq-record", "status", "--uid", "1000"])

    def _raise() -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(record, "_require_privileged_caller", _raise)

    with pytest.raises(SystemExit) as excinfo:
        record.main()

    assert excinfo.value.code == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"
    assert "denied" in payload["message"]
