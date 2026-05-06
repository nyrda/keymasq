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
            "macros": [{"name": "combo", "duration_us": 150_000, "event_count": 4}],
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


def test_create_macro_cli_sends_create_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, object]] = []

    def _session_request(payload: dict[str, object]) -> dict[str, object]:
        sent.append(payload)
        return {"status": "ok"}

    monkeypatch.setattr(commands, "_session_request", _session_request)

    commands.create_macro_cli("stored", ['{"events":[{"device_type":"keyboard","t_us":0}]}'])

    payload = sent[0]
    assert payload["command"] == "create_macro"
    macro = payload["macro"]
    assert isinstance(macro, dict)
    assert macro["name"] == "stored"
    assert macro["device_types"] == ["keyboard"]
    assert macro["events"] == [{"device_type": "keyboard", "t_us": 0}]


def test_create_macro_cli_force_sends_update_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, object]] = []

    def _session_request(payload: dict[str, object]) -> dict[str, object]:
        sent.append(payload)
        return {"status": "ok"}

    monkeypatch.setattr(commands, "_session_request", _session_request)

    commands.create_macro_cli("stored", ['[{"device_type":"mouse","t_us":0}]'], force=True)

    payload = sent[0]
    assert payload["command"] == "update_macro"
    assert payload["name"] == "stored"
    macro = payload["macro"]
    assert isinstance(macro, dict)
    assert macro["name"] == "stored"


def test_delete_macro_cli_sends_delete_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, object]] = []

    def _session_request(payload: dict[str, object]) -> dict[str, object]:
        sent.append(payload)
        return {"status": "ok"}

    monkeypatch.setattr(commands, "_session_request", _session_request)

    commands.delete_macro_cli("stored")

    assert sent == [{"command": "delete_macro", "name": "stored"}]


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


def test_set_diagnostics_cli_sends_categories(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sent: list[dict[str, object]] = []

    def _session_request(payload: dict[str, object]) -> dict[str, object]:
        sent.append(payload)
        return {
            "status": "ok",
            "data": {"enabled": True, "interval": 3.0, "categories": ["mainline", "combo"]},
        }

    monkeypatch.setattr(commands, "_session_request", _session_request)

    commands.set_diagnostics_cli(True, interval=3.0, include=["combo"])

    assert sent == [
        {
            "command": "set_diagnostics",
            "enabled": True,
            "interval": 3.0,
            "categories": ["mainline", "combo"],
        }
    ]
    assert "categories=mainline, combo" in capsys.readouterr().out


def test_type_cli_compiles_and_sends_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, object]] = []

    def _session_request(payload: dict[str, object]) -> dict[str, object]:
        sent.append(payload)
        return {"status": "ok"}

    monkeypatch.setattr(commands, "_session_request", _session_request)

    commands.type_cli(["Hi"], speed=1.5)

    payload = sent[0]
    assert payload["command"] == "type_text"
    assert payload["text"] == "Hi"
    assert payload["speed"] == 1.5
    assert payload["use_unicode_input"] is True


def test_type_cli_sends_no_unicode_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, object]] = []

    def _session_request(payload: dict[str, object]) -> dict[str, object]:
        sent.append(payload)
        return {"status": "ok"}

    monkeypatch.setattr(commands, "_session_request", _session_request)

    commands.type_cli(["é"], use_unicode_input=False)

    assert sent[0]["command"] == "type_text"
    assert sent[0]["use_unicode_input"] is False


def test_type_cli_print_json_does_not_send(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(commands, "_session_request", lambda payload: pytest.fail("sent request"))

    commands.type_cli(["Hi"], print_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload["device_types"] == ["keyboard"]
    assert len(payload["events"]) > 0


def test_play_adhoc_cli_compiles_compact_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, object]] = []

    def _session_request(payload: dict[str, object]) -> dict[str, object]:
        sent.append(payload)
        return {"status": "ok"}

    monkeypatch.setattr(commands, "_session_request", _session_request)

    commands.play_adhoc_cli(["key_a", "wait:10:20", "btn_left"], speed=0.5)

    payload = sent[0]
    assert payload["command"] == "play_compact_macro"
    assert payload["speed"] == 0.5
    assert payload["tokens"] == ["key_a", "wait:10:20", "btn_left"]


def test_play_adhoc_cli_reads_json_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, object]] = []

    def _session_request(payload: dict[str, object]) -> dict[str, object]:
        sent.append(payload)
        return {"status": "ok"}

    monkeypatch.setattr(commands, "_session_request", _session_request)

    commands.play_adhoc_cli(
        ['{"events":[{"device_type":"keyboard","type":1,"code":30,"value":1,"t_us":0}]}'],
        input_json=True,
    )

    payload = sent[0]
    assert payload["command"] == "play_macro_payload"
    assert payload["macro_events"] == [
        {"device_type": "keyboard", "type": 1, "code": 30, "value": 1, "t_us": 0}
    ]


def test_play_adhoc_cli_print_json_does_not_send(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(commands, "_session_request", lambda payload: pytest.fail("sent request"))

    commands.play_adhoc_cli(["key_a"], print_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload["device_types"] == ["keyboard"]
    assert len(payload["events"]) == 2
