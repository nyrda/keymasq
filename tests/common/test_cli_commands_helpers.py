import json
import socket
from pathlib import Path
from typing import Any

import pytest

from keymasq.cli import commands


def test_profile_kind_variants() -> None:
    assert commands._profile_kind({"is_permanent": True, "window_rule_count": 99}) == "conditional"
    assert commands._profile_kind({"is_permanent": True, "window_rule_count": 0}) == "permanent"
    assert commands._profile_kind({"window_rule_count": 2}) == "conditional"
    assert commands._profile_kind({}) == "standard"


def test_session_request_sends_complete_json_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeSocket:
        def __init__(self, *_args: Any) -> None:
            self.timeout: float | None = None
            self.connected_to = ""
            self.sent = b""
            self.closed = False
            self.recv_chunks = [b'{"status":"ok"}\n']

        def __enter__(self) -> "FakeSocket":
            return self

        def __exit__(self, *_args: Any) -> None:
            self.close()

        def settimeout(self, timeout: float) -> None:
            self.timeout = timeout

        def connect(self, path: str) -> None:
            self.connected_to = path

        def sendall(self, data: bytes) -> None:
            self.sent += data

        def send(self, _data: bytes) -> int:
            raise AssertionError("partial-write-prone send() must not be used")

        def recv(self, _size: int) -> bytes:
            if not self.recv_chunks:
                return b""
            return self.recv_chunks.pop(0)

        def close(self) -> None:
            self.closed = True

    fake_socket = FakeSocket()
    socket_path = tmp_path / "session.sock"
    socket_path.touch()
    monkeypatch.setattr(commands, "SESSION_SOCKET_PATH", socket_path)
    monkeypatch.setattr(socket, "socket", lambda *_args: fake_socket)

    result = commands._session_request({"command": "create_macro", "events": ["x" * 8192]})

    assert result == {"status": "ok"}
    assert fake_socket.connected_to == str(socket_path)
    assert fake_socket.closed is True
    assert fake_socket.sent.endswith(b"\n")
    assert json.loads(fake_socket.sent.decode()) == {
        "command": "create_macro",
        "events": ["x" * 8192],
    }


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


def test_mpris_cli_sends_session_command(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, object]] = []

    def _session_request(payload: dict[str, object]) -> dict[str, object]:
        sent.append(payload)
        return {"status": "ok"}

    monkeypatch.setattr(commands, "_session_request", _session_request)

    commands.mpris_cli("play-pause")

    assert sent == [{"command": "mpris", "mpris_command": "play_pause"}]


def test_mpris_cli_json_prints_session_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sent: list[dict[str, object]] = []

    def _session_request(payload: dict[str, object]) -> dict[str, object]:
        sent.append(payload)
        return {"status": "ok", "command": "next", "mpris": {"started": True}}

    monkeypatch.setattr(commands, "_session_request", _session_request)

    commands.mpris_cli("next", json_output=True)

    assert sent == [{"command": "mpris", "mpris_command": "next"}]
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"status": "ok", "command": "next", "mpris": {"started": True}}


def test_mpris_cli_rejects_unknown_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(commands, "_session_request", lambda payload: pytest.fail("sent request"))

    with pytest.raises(SystemExit) as excinfo:
        commands.mpris_cli("shuffle")

    assert excinfo.value.code == 1
    assert "unknown MPRIS command" in capsys.readouterr().out


def test_mpris_status_cli_prints_controller_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        commands,
        "_session_request",
        lambda payload: {
            "status": "ok",
            "mpris": {
                "started": True,
                "players": [
                    {
                        "service": "org.mpris.MediaPlayer2.firefox.instance_1_20",
                        "owner": ":1.20",
                        "playback_status": "Playing",
                        "playing": True,
                        "can_play": True,
                        "can_go_next": False,
                        "can_go_previous": False,
                        "track": {
                            "title": "Browser Video",
                            "artists": ["Example Channel"],
                            "album": None,
                        },
                    },
                    {
                        "service": "org.mpris.MediaPlayer2.spotify",
                        "owner": ":1.10",
                        "playback_status": "Playing",
                        "playing": True,
                        "can_play": True,
                        "can_go_next": True,
                        "can_go_previous": True,
                        "track": {
                            "title": "Song Title",
                            "artists": ["Artist Name"],
                            "album": "Album Name",
                        },
                    },
                ],
                "player_order": [":1.20", ":1.10"],
                "started_order": [":1.20", ":1.10"],
                "inactive_order": [],
            },
        },
    )

    commands.mpris_status_cli()

    out = capsys.readouterr().out
    assert "MPRIS: started" not in out
    assert "1. Firefox: Playing, active=yes" in out
    assert "current: Example Channel - Browser Video" in out
    assert "2. Spotify: Playing, active=yes" in out
    assert "current: Artist Name - Song Title (Album Name)" in out
    assert "play=yes, next=yes, previous=yes" in out
    assert "targets:" in out
    assert "play: 2. Spotify" in out
    assert "play-pause: pause 1. Firefox, 2. Spotify" in out
    assert "next: 2. Spotify" in out
    assert "previous: 2. Spotify" in out
    assert "routing order:" not in out
    assert "detected:" not in out
    assert "started:" not in out
    assert "inactive:" not in out
    assert ":1.10" not in out
    assert ":1.20" not in out


def test_mpris_status_cli_prints_resume_targets_when_nothing_is_playing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        commands,
        "_session_request",
        lambda payload: {
            "status": "ok",
            "mpris": {
                "started": True,
                "players": [
                    {
                        "service": "org.mpris.MediaPlayer2.firefox.instance_1_20",
                        "owner": ":1.20",
                        "playback_status": "Paused",
                        "playing": False,
                        "can_play": True,
                        "can_go_next": False,
                        "can_go_previous": False,
                    },
                    {
                        "service": "org.mpris.MediaPlayer2.spotify",
                        "owner": ":1.10",
                        "playback_status": "Paused",
                        "playing": False,
                        "can_play": True,
                        "can_go_next": True,
                        "can_go_previous": True,
                    },
                ],
                "player_order": [":1.20", ":1.10"],
                "started_order": [":1.20", ":1.10"],
                "inactive_order": [":1.20", ":1.10"],
            },
        },
    )

    commands.mpris_status_cli()

    out = capsys.readouterr().out
    assert "play: 2. Spotify" in out
    assert "play-pause: play 2. Spotify" in out
    assert "next: 2. Spotify" in out
    assert "previous: 2. Spotify" in out


def test_mpris_status_cli_prints_not_started_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        commands,
        "_session_request",
        lambda payload: {"status": "ok", "mpris": {"started": False, "players": []}},
    )

    commands.mpris_status_cli()

    out = capsys.readouterr().out
    assert "MPRIS: not started" in out
    assert "players: none" in out


def test_mpris_status_cli_json_prints_mpris_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sent: list[dict[str, object]] = []

    def _session_request(payload: dict[str, object]) -> dict[str, object]:
        sent.append(payload)
        return {"status": "ok", "mpris": {"started": True, "players": []}}

    monkeypatch.setattr(
        commands,
        "_session_request",
        _session_request,
    )

    commands.mpris_status_cli(json_output=True)

    assert sent == [{"command": "mpris", "mpris_command": "status"}]
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"status": "ok", "mpris": {"started": True, "players": []}}


def test_list_profiles_cli_prints_devices_without_profiles(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        commands,
        "_session_request",
        lambda payload: {
            "status": "ok",
            "profiles": [],
            "devices": [
                {
                    "hardware_id": "kbd",
                    "device_name": "Keyboard",
                    "active_profiles": [],
                    "mapping_count": 0,
                }
            ],
        },
    )

    commands.list_profiles_cli()

    out = capsys.readouterr().out
    assert "No profiles found" in out
    assert "Devices:" in out
    assert "kbd  Keyboard" in out
    assert "active: passthrough" in out


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


def test_create_macro_cli_rejects_non_object_event(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(commands, "_session_request", lambda payload: pytest.fail("sent request"))

    with pytest.raises(SystemExit) as excinfo:
        commands.create_macro_cli(
            "stored",
            ['{"events":[{"device_type":"keyboard","t_us":0},"bad",{"t_us":1}]}'],
        )

    assert excinfo.value.code == 1
    assert "Error: macro JSON events[1] must be an object" in capsys.readouterr().out


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
            "macro_recording_enabled": False,
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
    assert "macro recording: disabled" in out
    assert "capture unlock: locked" in out
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
            "data": {
                "enabled": True,
                "interval": 3.0,
                "categories": ["mainline", "combo", "macro"],
            },
        }

    monkeypatch.setattr(commands, "_session_request", _session_request)

    commands.set_diagnostics_cli(True, interval=3.0, include=["combo", "macro"])

    assert sent == [
        {
            "command": "set_diagnostics",
            "enabled": True,
            "interval": 3.0,
            "categories": ["mainline", "combo", "macro"],
        }
    ]
    assert capsys.readouterr().out == (
        "Diagnostics enabled (interval=3.00s, categories=mainline, combo, macro)\n"
    )


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


def test_type_cli_print_json_infers_mouse_device_type(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(commands, "_session_request", lambda payload: pytest.fail("sent request"))

    commands.type_cli(["<move:200:100>test"], print_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload["device_types"] == ["mouse", "keyboard"]
