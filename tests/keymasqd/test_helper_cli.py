import json
import sys

import pytest

from keymasq import helper
from keymasq.masking import operations


def _record_operations(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        operations, "main", lambda operation, token="": calls.append((operation, token))
    )
    return calls


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["hardware-operation", "abc123"], ("hardware-operation", "abc123")),
        (["recover-hardware"], ("recover-hardware", "")),
        (["prepare-removal"], ("prepare-removal", "")),
    ],
)
def test_main_dispatches_hardware_commands(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: tuple[str, str]
) -> None:
    calls = _record_operations(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["keymasq-helper", *argv])

    helper.main()

    assert calls == [expected]


@pytest.mark.parametrize(
    "command",
    [
        "status",
        "unlock-runtime",
        "lock-runtime",
        "unlock-persistent",
        "macro-recording-status",
        "enable-macro-recording-runtime",
        "enable-macro-recording-persistent",
        "disable-macro-recording",
    ],
)
def test_main_rejects_removed_capture_commands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    calls = _record_operations(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["keymasq-helper", command, "--uid", "1000"])

    with pytest.raises(SystemExit) as excinfo:
        helper.main()

    assert excinfo.value.code == 2
    assert calls == []
    assert "invalid choice" in capsys.readouterr().err


def test_main_permission_error_emits_json_and_exits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raise(_operation: str, _token: str = "") -> None:
        raise PermissionError("Hardware operations require root")

    monkeypatch.setattr(operations, "main", _raise)
    monkeypatch.setattr(sys, "argv", ["keymasq-helper", "recover-hardware"])

    with pytest.raises(SystemExit) as excinfo:
        helper.main()

    assert excinfo.value.code == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {"status": "error", "message": "Hardware operations require root"}


def test_main_unexpected_error_logs_exception(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _raise(_operation: str, _token: str = "") -> None:
        raise RuntimeError("recovery broke")

    monkeypatch.setattr(operations, "main", _raise)
    monkeypatch.setattr(sys, "argv", ["keymasq-helper", "recover-hardware"])

    with caplog.at_level("ERROR", logger="keymasq.helper"):
        with pytest.raises(SystemExit) as excinfo:
            helper.main()

    assert excinfo.value.code == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"
    assert "recovery broke" in payload["message"]
    assert "Unexpected keymasq-helper failure" in caplog.text
    assert "RuntimeError: recovery broke" in caplog.text
