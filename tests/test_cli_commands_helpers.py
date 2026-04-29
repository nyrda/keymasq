import json

import evdev
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


def test_type_cli_compiles_and_sends_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, object]] = []

    def _session_request(payload: dict[str, object]) -> dict[str, object]:
        sent.append(payload)
        return {"status": "ok"}

    monkeypatch.setattr(commands, "_session_request", _session_request)

    commands.type_cli(["Hi"], speed=1.5)

    payload = sent[0]
    assert payload["command"] == "play_macro_payload"
    assert payload["speed"] == 1.5
    assert len(payload["macro_events"]) > 0


def test_type_cli_uses_unicode_fallback_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, object]] = []

    def _session_request(payload: dict[str, object]) -> dict[str, object]:
        sent.append(payload)
        return {"status": "ok"}

    monkeypatch.setattr(commands, "_session_request", _session_request)

    commands.type_cli(["é"])

    events = sent[0]["macro_events"]
    assert isinstance(events, list)
    press_codes = [event["code"] for event in events if event.get("value") == 1]
    assert press_codes[:3] == [
        evdev.ecodes.KEY_LEFTCTRL,
        evdev.ecodes.KEY_LEFTSHIFT,
        evdev.ecodes.KEY_U,
    ]


def test_type_cli_no_unicode_rejects_unsupported_character(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(commands, "_session_request", lambda payload: pytest.fail("sent request"))

    with pytest.raises(SystemExit) as excinfo:
        commands.type_cli(["é"], use_unicode_input=False)

    assert excinfo.value.code == 1


def test_play_adhoc_cli_compiles_compact_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, object]] = []

    def _session_request(payload: dict[str, object]) -> dict[str, object]:
        sent.append(payload)
        return {"status": "ok"}

    monkeypatch.setattr(commands, "_session_request", _session_request)

    commands.play_adhoc_cli(["key_a", "wait:10:20", "btn_left"], speed=0.5)

    payload = sent[0]
    assert payload["command"] == "play_macro_payload"
    assert payload["speed"] == 0.5
    events = payload["macro_events"]
    assert isinstance(events, list)
    wait_random = next(event for event in events if event.get("macro_action") == "wait_random")
    assert wait_random["min_us"] == 10_000
    assert wait_random["max_us"] == 20_000


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
