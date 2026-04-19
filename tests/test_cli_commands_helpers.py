import json

import pytest

from keymasq.cli import commands


def test_profile_kind_variants() -> None:
    assert commands._profile_kind({"is_permanent": True, "window_rule_count": 99}) == "permanent"
    assert commands._profile_kind({"window_rule_count": 2}) == "conditional"
    assert commands._profile_kind({}) == "standard"


def test_list_macros_cli_prints_macros(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        commands,
        "_session_request",
        lambda payload: {
            "status": "ok",
            "macros": [{"name": "combo", "duration_ms": 150, "event_count": 4}],
        },
    )

    commands.list_macros_cli()

    out = capsys.readouterr().out
    assert "combo" in out
    assert "150ms" in out
    assert "4 events" in out


def test_list_macros_cli_json_prints_response(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        commands,
        "_session_request",
        lambda payload: {"status": "ok", "macros": [{"name": "combo"}]},
    )

    commands.list_macros_cli(json_output=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"status": "ok", "macros": [{"name": "combo"}]}


def test_status_cli_prints_runtime_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        commands,
        "_session_request",
        lambda payload: {
            "status": "ok",
            "keymasqd_connected": True,
            "compositor_id": "gnome",
            "compositor_name": "GNOME Shell",
            "compositor_supported": True,
            "listener_active": True,
            "listener_name": "gnome",
            "recording_active": False,
            "recording_unlock_required": True,
            "recording_unlocked": False,
            "active_profiles": ["Base"],
            "devices": {
                "1234:5678": {
                    "device_name": "Example Keyboard",
                    "profiles": ["Base"],
                    "mapping_count": 2,
                }
            },
            "window": {"app_id": "firefox", "title": "Example"},
        },
    )

    commands.status_cli()

    out = capsys.readouterr().out
    assert "keymasqd: connected" in out
    assert "compositor: GNOME Shell (gnome)" in out
    assert "listener: active (gnome)" in out
    assert "recording unlock: locked" in out
    assert "active profiles: Base" in out
    assert "Example Keyboard (1234:5678)" in out
    assert "window: firefox - Example" in out


def test_status_cli_json_prints_response(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        commands,
        "_session_request",
        lambda payload: {"status": "ok", "keymasqd_connected": False},
    )

    commands.status_cli(json_output=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"status": "ok", "keymasqd_connected": False}


def test_set_diagnostics_cli_exits_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commands, "_session_request", lambda payload: {"status": "error"})

    with pytest.raises(SystemExit) as excinfo:
        commands.set_diagnostics_cli(True, interval=3.0)

    assert excinfo.value.code == 1
