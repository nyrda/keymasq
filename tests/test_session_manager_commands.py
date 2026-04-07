from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

import keyforge.session.manager.compositor as session_compositor_module
import keyforge.session.manager.recording as session_recording_module
from keyforge.common.ipc import Response
from keyforge.common.security import PeerCredentials, SecurityPolicy
from keyforge.session.listeners.hyprland import HyprlandListener
from keyforge.session.listeners.kde import KDEListener
from keyforge.session.manager import SessionManager


@pytest.mark.asyncio
async def test_sensitive_command_requires_active_recording_owner() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=111, uid=1000, gid=1000)
    owner_writer = object()
    other_writer = object()

    lock_recording_unlock = AsyncMock(return_value={"status": "ok"})
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(session_recording_module, "lock_recording_unlock", lock_recording_unlock)
    manager.unlock_state.refresh_owner = {
        "uid": 1000,
        "pid": 111,
        "writer_id": id(owner_writer),
        "lease_id": "lease-1",
    }

    denied = await manager._handle_session_request(
        {"command": "lock_recording_unlock"},
        "client",
        peer,
        other_writer,  # type: ignore[arg-type]
    )
    assert denied["status"] == "error"
    assert denied.get("error_code") == "sensitive_command_denied"

    allowed = await manager._handle_session_request(
        {"command": "lock_recording_unlock", "lease_id": "lease-1"},
        "client",
        peer,
        owner_writer,  # type: ignore[arg-type]
    )
    assert allowed["status"] == "ok"
    lock_recording_unlock.assert_awaited_once()
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_session_request_uses_single_policy_snapshot_for_acl_and_sensitivity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=111, uid=1000, gid=1000)
    writer = object()
    start_recording = AsyncMock(return_value={"status": "ok"})
    manager.security_policy = SecurityPolicy(
        session_command_acl={"client": []},
        daemon_command_acl={"session": []},
        recording_unlock_required=True,
    )

    def fake_command_allowed(command: str, acl: dict[str, list[str]], client_class: str) -> bool:
        assert command == "start_recording"
        assert acl is manager.security_policy.session_command_acl
        assert client_class == "client"
        manager.security_policy = SecurityPolicy(
            session_command_acl={"client": []},
            daemon_command_acl={"session": []},
            recording_unlock_required=False,
        )
        return True

    monkeypatch.setattr("keyforge.session.manager.commands.command_allowed", fake_command_allowed)
    monkeypatch.setattr(session_recording_module, "start_recording", start_recording)

    result = await manager._handle_session_request(
        {"command": "start_recording"},
        "client",
        peer,
        writer,  # type: ignore[arg-type]
    )

    assert result["status"] == "error"
    assert result["error_code"] == "sensitive_command_denied"
    start_recording.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_session_request_returns_unknown_command_error() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    result = await manager._handle_session_request(
        {"command": "does_not_exist"},
        "client",
        peer,
        object(),
    )

    assert result == {"error": "Unknown command: does_not_exist"}


@pytest.mark.asyncio
async def test_handle_session_request_get_active_window_uses_listener() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    class _Listener:
        name = "fake"

        async def get_active_window(self) -> tuple[str, str, list[str]]:
            return "steam", "Counter-Strike 2", ["game", "fullscreen"]

    manager.compositor_state.window_listener = _Listener()  # type: ignore[assignment]

    result = await manager._handle_session_request(
        {"command": "get_active_window"},
        "client",
        peer,
        object(),
    )

    assert result == {
        "status": "ok",
        "class": "steam",
        "title": "Counter-Strike 2",
        "tags": ["game", "fullscreen"],
    }
    assert manager.compositor_state.current_window == {
        "class": "steam",
        "title": "Counter-Strike 2",
        "tags": ["game", "fullscreen"],
    }


@pytest.mark.asyncio
async def test_handle_session_request_get_active_window_falls_back_to_cached_window() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    manager.compositor_state.current_window = {
        "class": "firefox",
        "title": "Docs",
        "tags": ["work"],
    }

    result = await manager._handle_session_request(
        {"command": "get_active_window"},
        "client",
        peer,
        object(),
    )

    assert result == {
        "status": "ok",
        "class": "firefox",
        "title": "Docs",
        "tags": ["work"],
    }


@pytest.mark.asyncio
async def test_handle_session_request_get_compositor_reports_compositor_dispatch_availability(
) -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    listener = HyprlandListener(AsyncMock())
    listener.running = True
    manager.compositor_state.window_listener = listener
    manager.compositor_state.compositor_id = "hyprland"

    result = await manager._handle_session_request(
        {"command": "get_compositor"},
        "client",
        peer,
        object(),
    )

    assert result["compositor_id"] == "hyprland"
    assert result["listener_active"] is True
    assert result["listener_name"] == "hyprland"
    assert result["compositor_dispatch_available"] is True


@pytest.mark.asyncio
async def test_handle_session_request_dispatch_compositor_uses_runtime_dispatch() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    listener = AsyncMock()
    listener.dispatch = AsyncMock(return_value=(True, "ok"))
    manager.compositor_state.window_listener = listener
    manager.compositor_state.compositor_id = "niri"

    result = await manager._handle_session_request(
        {
            "command": "dispatch_compositor",
            "compositor": "niri",
            "dispatcher": "toggle_window_floating",
            "args": "",
        },
        "client",
        peer,
        object(),
    )

    assert result == {"status": "ok", "message": "ok"}
    listener.dispatch.assert_awaited_once_with("toggle_window_floating", "")


@pytest.mark.asyncio
async def test_handle_session_request_get_compositor_merges_listener_runtime_warning() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    class _Listener:
        name = "gnome"
        running = True
        supports_compositor_dispatch = True
        compositor_dispatch_available = False

        def runtime_support_details(self) -> dict[str, bool | str | int]:
            return {
                "warning": "GNOME bridge update detected. Log out and back in.",
                "bridge_protocol": 1,
                "bridge_protocol_expected": 2,
            }

    manager.compositor_state.window_listener = _Listener()  # type: ignore[assignment]
    manager.compositor_state.compositor_id = "gnome"

    async def support_details(_compositor_id: str | None, _dbus=None) -> dict[str, bool | str]:
        return {"supported": True, "warning": ""}

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        session_compositor_module,
        "get_compositor_support_details",
        support_details,
    )

    try:
        result = await manager._handle_session_request(
            {"command": "get_compositor"},
            "client",
            peer,
            object(),
        )
    finally:
        monkeypatch.undo()

    assert result["supported"] is True
    assert result["compositor_dispatch_available"] is False
    details = cast(dict[str, object], result["details"])
    assert details["bridge_protocol"] == 1
    assert "Log out and back in" in str(details["warning"])


@pytest.mark.asyncio
async def test_handle_session_request_get_compositor_reports_kde_dispatch_availability() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    listener = KDEListener(AsyncMock())
    listener.running = True
    manager.compositor_state.window_listener = listener
    manager.compositor_state.compositor_id = "kde"

    result = await manager._handle_session_request(
        {"command": "get_compositor"},
        "client",
        peer,
        object(),
    )

    assert result["compositor_id"] == "kde"
    assert result["listener_active"] is True
    assert result["listener_name"] == "kde"
    assert result["compositor_dispatch_available"] is True


@pytest.mark.asyncio
async def test_get_status_uses_async_unlock_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = SessionManager()
    manager.security_policy.recording_unlock_required = True
    manager.security_policy.gui_allow_left_right_click_remap = True
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    resolve_unlock_status_async = AsyncMock(
        return_value={"unlocked": True, "source": "runtime", "expires_at": 1234}
    )
    monkeypatch.setattr(
        session_recording_module,
        "resolve_unlock_status_async",
        resolve_unlock_status_async,
    )

    async def support_details(_compositor_id: str | None, _dbus=None) -> dict[str, bool | str]:
        return {"supported": False, "warning": ""}

    monkeypatch.setattr(
        session_compositor_module,
        "get_compositor_support_details",
        support_details,
    )

    result = await manager._handle_session_request(
        {"command": "get_status"},
        "client",
        peer,
        writer,  # type: ignore[arg-type]
    )

    assert result["status"] == "ok"
    assert result["recording_unlocked"] is True
    assert result["recording_unlock_required"] is True
    assert result["gui_allow_left_right_click_remap"] is True
    assert result["recording_unlock_source"] == "runtime"
    assert result["recording_unlock_expires_at"] == 1234
    resolve_unlock_status_async.assert_awaited_once_with(manager, peer.uid)


@pytest.mark.asyncio
async def test_get_recording_settings_uses_unlock_and_owner_state_only() -> None:
    manager = SessionManager()
    manager.security_policy.recording_unlock_required = True
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    resolve_unlock_status_async = AsyncMock(
        return_value={"unlocked": True, "source": "runtime", "expires_at": 4321}
    )
    manager.unlock_state.refresh_owner = {
        "uid": peer.uid,
        "pid": peer.pid,
        "writer_id": id(writer),
        "lease_id": "lease-test",
    }
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        session_recording_module,
        "resolve_unlock_status_async",
        resolve_unlock_status_async,
    )

    result = await manager._handle_session_request(
        {"command": "get_recording_settings"},
        "client",
        peer,
        writer,  # type: ignore[arg-type]
    )

    assert result["status"] == "ok"
    assert result["recording_unlocked"] is True
    assert result["recording_unlock_required"] is True
    assert result["recording_refresh_owner"] is True
    assert "authorized" not in result
    resolve_unlock_status_async.assert_awaited_once_with(manager, peer.uid)
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_sensitive_recording_commands_do_not_require_owner_when_unlock_not_required() -> None:
    manager = SessionManager()
    manager.security_policy.recording_unlock_required = False
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    start_recording = AsyncMock(return_value={"status": "ok"})
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(session_recording_module, "start_recording", start_recording)

    result = await manager._handle_session_request(
        {"command": "start_recording"},
        "client",
        peer,
        writer,  # type: ignore[arg-type]
    )

    assert result == {"status": "ok"}
    start_recording.assert_awaited_once_with(manager, reset_if_active=False)
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_get_status_reports_effective_unlock_when_unlock_not_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.security_policy.recording_unlock_required = False
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    resolve_unlock_status_async = AsyncMock(
        return_value={"unlocked": False, "source": "none", "expires_at": 0}
    )
    monkeypatch.setattr(
        session_recording_module,
        "resolve_unlock_status_async",
        resolve_unlock_status_async,
    )

    async def support_details(_compositor_id: str | None, _dbus=None) -> dict[str, bool | str]:
        return {"supported": False, "warning": ""}

    monkeypatch.setattr(
        session_compositor_module,
        "get_compositor_support_details",
        support_details,
    )

    result = await manager._handle_session_request(
        {"command": "get_status"},
        "client",
        peer,
        writer,  # type: ignore[arg-type]
    )

    assert result["status"] == "ok"
    assert result["recording_unlock_required"] is False
    assert result["recording_unlocked"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["begin_capture", "capture_read", "end_capture"])
async def test_capture_commands_with_owner_return_error_on_missing_hardware_id(
    command: str,
) -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    manager.unlock_state.refresh_owner = {
        "uid": peer.uid,
        "pid": peer.pid,
        "writer_id": id(writer),
        "lease_id": "lease-test",
    }

    result = await manager._handle_session_request(
        {"command": command},
        "client",
        peer,
        writer,
    )

    assert result == {"error": "missing hardware_id"}


@pytest.mark.asyncio
async def test_handle_session_request_create_macro_broadcasts_saved_event() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"macro": {"name": "Speedrun"}})
    )
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]

    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    result = await manager._handle_session_request(
        {"command": "create_macro", "macro": {"name": "Speedrun"}},
        "client",
        peer,
        object(),
    )

    assert result == {"status": "ok", "macro": {"name": "Speedrun"}}
    manager.broadcast_to_session_clients.assert_called_once_with(  # type: ignore[attr-defined]
        {"event": "macro_saved", "name": "Speedrun"}
    )


@pytest.mark.asyncio
async def test_capture_combo_session_command_round_trip() -> None:
    manager = SessionManager()
    manager.hardware.list_hardware_ids = lambda: ["1234:5678"]  # type: ignore[assignment]
    manager.profiles.get_profile = Mock(
        return_value=SimpleNamespace(
            config=SimpleNamespace(device_layers={"1234:5678": object()})
        )
    )
    manager.client.send_command = AsyncMock(
        return_value=Response(
            status="ok",
            data={
                "events": [
                    {
                        "evdev": "key_a",
                        "hardware_id": "1234:5678",
                        "source": "kbd",
                    }
                ],
                "warnings": [],
            },
        )
    )

    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    manager.unlock_state.refresh_owner = {
        "uid": peer.uid,
        "pid": peer.pid,
        "writer_id": id(writer),
        "lease_id": "lease-test",
    }

    capture = await manager._handle_session_request(
        {"command": "capture_combo", "profile_name": "Desktop"},
        "client",
        peer,
        writer,
    )

    assert capture == {
        "status": "ok",
        "events": [{"evdev": "key_a", "hardware_id": "1234:5678", "source": "kbd"}],
        "warnings": [],
    }
