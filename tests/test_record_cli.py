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

    def _chown(path: Path, uid: int, gid: int) -> None:
        calls.append((uid, gid))

    monkeypatch.setattr(record.os, "chown", _chown)

    record._write_lease(lease, 42)
    assert lease.read_text(encoding="utf-8") == "42\n"
    assert calls == [(1000, 1000)]

    record._remove_lease(lease)
    assert lease.exists() is False


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


def test_main_unlock_runtime_extends_previous_expiry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime_path = tmp_path / "runtime-lease"
    writes: list[tuple[Path, int]] = []

    monkeypatch.setattr(
        sys,
        "argv",
        ["keymasq-record", "unlock-runtime", "--uid", "1000", "--ttl", "5"],
    )
    monkeypatch.setattr(record, "_require_privileged_caller", lambda: None)
    monkeypatch.setattr(record, "runtime_unlock_path", lambda uid: runtime_path)
    monkeypatch.setattr(record.time, "time", lambda: 10)
    monkeypatch.setattr(record, "parse_unlock_expires_at", lambda _: 20)

    def _write(path: Path, expires_at: int) -> None:
        writes.append((path, expires_at))

    monkeypatch.setattr(record, "_write_lease", _write)

    record.main()

    assert writes == [(runtime_path, 21)]
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["expires_at"] == 21


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
