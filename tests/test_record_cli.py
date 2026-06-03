import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from keymasq import record


def test_require_privileged_caller_allows_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(record.os, "geteuid", lambda: 0)
    assert record._require_privileged_caller() == 0


def test_require_privileged_caller_rejects_other_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(record.os, "geteuid", lambda: 1234)
    monkeypatch.setattr(record.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=999))

    with pytest.raises(PermissionError):
        record._require_privileged_caller()


def test_require_privileged_caller_allows_keymasq_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(record.os, "geteuid", lambda: 777)
    monkeypatch.setattr(record.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=777))
    assert record._require_privileged_caller() == 777


def test_authorize_target_uid_rejects_pkexec_uid_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PKEXEC_UID", "1000")

    with pytest.raises(PermissionError, match="Target uid"):
        record._authorize_target_uid(1001, 0)


def test_authorize_target_uid_rejects_non_root_caller_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PKEXEC_UID", raising=False)

    with pytest.raises(PermissionError, match="Target uid"):
        record._authorize_target_uid(1001, 777)


def test_runtime_expires_at_rounds_up_fractional_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.999
    monkeypatch.setattr(record.time, "time", lambda: now)

    expires_at = record._runtime_expires_at(1)

    assert expires_at == 102
    assert expires_at >= now + 1


def test_write_lease_and_remove_lease(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lease = tmp_path / "lease"
    monkeypatch.setattr(
        record.pwd,
        "getpwnam",
        lambda _: SimpleNamespace(pw_uid=1000, pw_gid=1000),
    )
    monkeypatch.setattr(record.pwd, "getpwuid", lambda _: SimpleNamespace(pw_gid=1235))

    calls: list[tuple[int, int]] = []

    def _chown(fd: int, uid: int, gid: int) -> None:
        calls.append((uid, gid))

    monkeypatch.setattr(record.os, "fchown", _chown)

    record._write_lease(lease, 42, 1234)
    assert lease.read_text(encoding="utf-8") == "42\n"
    assert stat.S_IMODE(lease.stat().st_mode) == 0o440
    assert calls == [(1000, 1235)]

    record._remove_lease(lease)
    assert lease.exists() is False


def test_write_lease_propagates_unexpected_user_lookup_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lease = tmp_path / "lease"

    def _raise(_name: str) -> None:
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(record.pwd, "getpwnam", _raise)

    with pytest.raises(RuntimeError, match="lookup failed"):
        record._write_lease(lease, 42, 1234)

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
        record._write_lease(lease, 42, 1234)

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
        record._write_lease(lease, 42, 1234)

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


@pytest.mark.parametrize(
    ("argv", "resolver_name"),
    [
        (["keymasq-record", "status", "--uid", "1001"], "resolve_unlock_status"),
        (
            ["keymasq-record", "macro-recording-status", "--uid", "1001"],
            "resolve_macro_recording_status",
        ),
    ],
)
def test_main_status_commands_reject_pkexec_uid_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    resolver_name: str,
) -> None:
    resolved: list[int] = []

    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(record, "_require_privileged_caller", lambda: 0)
    monkeypatch.setenv("PKEXEC_UID", "1000")
    monkeypatch.setattr(
        record,
        resolver_name,
        lambda uid: resolved.append(uid) or {"unlocked": True},
    )

    with pytest.raises(SystemExit) as excinfo:
        record.main()

    assert excinfo.value.code == 1
    assert resolved == []
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"
    assert "Target uid" in payload["message"]


def test_main_unlock_runtime_replaces_previous_expiry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime_path = tmp_path / "runtime-lease"
    runtime_path.write_text("20\n", encoding="utf-8")
    writes: list[tuple[Path, int, int]] = []

    monkeypatch.setattr(
        sys,
        "argv",
        ["keymasq-record", "unlock-runtime", "--uid", "1000", "--ttl", "5"],
    )
    monkeypatch.setattr(record, "_require_privileged_caller", lambda: None)
    monkeypatch.setattr(record, "runtime_unlock_path", lambda uid: runtime_path)
    monkeypatch.setattr(record.time, "time", lambda: 10)

    def _write(path: Path, expires_at: int, target_uid: int) -> None:
        writes.append((path, expires_at, target_uid))

    monkeypatch.setattr(record, "_write_lease", _write)

    record.main()

    assert writes == [(runtime_path, 15, 1000)]
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["expires_at"] == 15


def test_main_unlock_runtime_extends_with_requested_ttl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime_path = tmp_path / "runtime-lease"
    runtime_path.write_text("12\n", encoding="utf-8")
    writes: list[tuple[Path, int, int]] = []

    monkeypatch.setattr(
        sys,
        "argv",
        ["keymasq-record", "unlock-runtime", "--uid", "1000", "--ttl", "20"],
    )
    monkeypatch.setattr(record, "_require_privileged_caller", lambda: None)
    monkeypatch.setattr(record, "runtime_unlock_path", lambda uid: runtime_path)
    monkeypatch.setattr(record.time, "time", lambda: 10)

    def _write(path: Path, expires_at: int, target_uid: int) -> None:
        writes.append((path, expires_at, target_uid))

    monkeypatch.setattr(record, "_write_lease", _write)

    record.main()

    assert writes == [(runtime_path, 30, 1000)]
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["expires_at"] == 30


def test_main_unlock_runtime_ttl_zero_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime_path = tmp_path / "runtime-lease"
    writes: list[tuple[Path, int, int]] = []

    monkeypatch.setattr(
        sys,
        "argv",
        ["keymasq-record", "unlock-runtime", "--uid", "1000", "--ttl", "0"],
    )
    monkeypatch.setattr(record, "_require_privileged_caller", lambda: None)
    monkeypatch.setattr(record, "runtime_unlock_path", lambda uid: runtime_path)

    def _write(path: Path, expires_at: int, target_uid: int) -> None:
        writes.append((path, expires_at, target_uid))

    monkeypatch.setattr(record, "_write_lease", _write)

    with pytest.raises(SystemExit) as excinfo:
        record.main()

    assert excinfo.value.code == 1
    assert writes == []
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"
    assert "runtime ttl" in payload["message"]


def test_main_macro_recording_runtime_ttl_zero_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime_path = tmp_path / "macro-runtime"
    writes: list[tuple[Path, int]] = []

    monkeypatch.setattr(
        sys,
        "argv",
        ["keymasq-record", "enable-macro-recording-runtime", "--uid", "1000", "--ttl", "0"],
    )
    monkeypatch.setattr(record, "_require_privileged_caller", lambda: None)
    monkeypatch.setattr(record, "runtime_macro_recording_path", lambda uid: runtime_path)
    monkeypatch.setattr(
        record,
        "_write_lease",
        lambda path, expires_at, target_uid: writes.append((path, expires_at, target_uid)),
    )

    with pytest.raises(SystemExit) as excinfo:
        record.main()

    assert excinfo.value.code == 1
    assert writes == []
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"
    assert "runtime ttl" in payload["message"]


def test_main_mutation_rejects_pkexec_uid_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime_path = tmp_path / "runtime-lease"
    writes: list[tuple[Path, int, int]] = []

    monkeypatch.setattr(
        sys,
        "argv",
        ["keymasq-record", "unlock-runtime", "--uid", "1001", "--ttl", "5"],
    )
    monkeypatch.setattr(record, "_require_privileged_caller", lambda: 0)
    monkeypatch.setenv("PKEXEC_UID", "1000")
    monkeypatch.setattr(record, "runtime_unlock_path", lambda uid: runtime_path)
    monkeypatch.setattr(
        record,
        "_write_lease",
        lambda path, expires_at, target_uid: writes.append((path, expires_at, target_uid)),
    )

    with pytest.raises(SystemExit) as excinfo:
        record.main()

    assert excinfo.value.code == 1
    assert writes == []
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"
    assert "Target uid" in payload["message"]


def test_main_mutation_rejects_non_root_forged_pkexec_uid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime_path = tmp_path / "runtime-lease"
    writes: list[tuple[Path, int, int]] = []

    monkeypatch.setattr(
        sys,
        "argv",
        ["keymasq-record", "unlock-runtime", "--uid", "1001", "--ttl", "5"],
    )
    monkeypatch.setattr(record, "_require_privileged_caller", lambda: 777)
    monkeypatch.setenv("PKEXEC_UID", "1001")
    monkeypatch.setattr(record, "runtime_unlock_path", lambda uid: runtime_path)
    monkeypatch.setattr(
        record,
        "_write_lease",
        lambda path, expires_at, target_uid: writes.append((path, expires_at, target_uid)),
    )

    with pytest.raises(SystemExit) as excinfo:
        record.main()

    assert excinfo.value.code == 1
    assert writes == []
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"
    assert "Target uid" in payload["message"]


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


def test_main_enable_and_disable_macro_recording_persistent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    persistent_path = tmp_path / "macro-enabled"
    runtime_path = tmp_path / "macro-runtime"
    writes: list[tuple[Path, int, int]] = []

    monkeypatch.setattr(record, "_require_privileged_caller", lambda: None)
    monkeypatch.setattr(record, "persistent_macro_recording_path", lambda uid: persistent_path)
    monkeypatch.setattr(record, "runtime_macro_recording_path", lambda uid: runtime_path)
    monkeypatch.setattr(
        record,
        "_write_lease",
        lambda path, expires_at, target_uid: writes.append((path, expires_at, target_uid)),
    )

    monkeypatch.setattr(
        sys,
        "argv",
        ["keymasq-record", "enable-macro-recording-persistent", "--uid", "1000"],
    )
    record.main()

    assert writes == [(persistent_path, 0, 1000)]
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["scope"] == "macro_recording_persistent"

    runtime_path.write_text("123\n", encoding="utf-8")
    persistent_path.write_text("0\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["keymasq-record", "disable-macro-recording-persistent", "--uid", "1000"],
    )

    record.main()

    assert runtime_path.exists() is True
    assert persistent_path.exists() is False
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {
        "status": "ok",
        "scope": "macro_recording_persistent",
        "enabled": False,
    }

    persistent_path.write_text("0\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["keymasq-record", "disable-macro-recording", "--uid", "1000"],
    )

    record.main()

    assert runtime_path.exists() is False
    assert persistent_path.exists() is False
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {"status": "ok", "scope": "macro_recording", "enabled": False}


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
