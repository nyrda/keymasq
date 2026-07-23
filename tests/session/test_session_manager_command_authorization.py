from typing import cast
from unittest.mock import AsyncMock

import pytest

import keymasq.session.manager.compositor as session_compositor_module
import keymasq.session.manager.recording_unlock as recording_unlock_module
from keymasq.common.security import PeerCredentials
from keymasq.session.listeners.hyprland import HyprlandListener
from keymasq.session.manager.core import SessionManager
from tests.session.support import grant_recording_refresh_owner


@pytest.mark.asyncio
async def test_sensitive_command_requires_active_recording_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=111, uid=1000, gid=1000)
    owner_writer = object()
    other_writer = object()

    lock_recording_unlock = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(recording_unlock_module, "lock_recording_unlock", lock_recording_unlock)
    grant_recording_refresh_owner(
        manager,
        peer,
        owner_writer,
        monkeypatch,
        lease_id="lease-1",
    )

    denied = await manager._handle_session_request(
        {"command": "lock_recording_unlock"},
        peer,
        other_writer,  # type: ignore[arg-type]
    )
    assert denied["status"] == "error"
    assert denied.get("error_code") == "sensitive_command_denied"

    allowed = await manager._handle_session_request(
        {"command": "lock_recording_unlock", "lease_id": "lease-1"},
        peer,
        owner_writer,  # type: ignore[arg-type]
    )
    assert allowed["status"] == "ok"
    lock_recording_unlock.assert_awaited_once()


@pytest.mark.asyncio
async def test_sensitive_command_rejects_refresh_owner_when_unlock_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.security_policy.macro_edit_requires_unlock = True
    manager.client.send_command = AsyncMock()
    peer = PeerCredentials(pid=111, uid=1000, gid=1000)
    writer = object()
    manager.unlock_state.refresh_owner = {
        "uid": peer.uid,
        "pid": peer.pid,
        "writer_id": id(writer),
        "lease_id": "lease-1",
    }
    resolve_unlock_status_async = AsyncMock(
        return_value={"unlocked": False, "source": "none", "expires_at": 0}
    )
    monkeypatch.setattr(
        recording_unlock_module,
        "resolve_unlock_status_async",
        resolve_unlock_status_async,
    )

    result = await manager._handle_session_request(
        {"command": "get_macro", "name": "Secret"},
        peer,
        writer,  # type: ignore[arg-type]
    )

    assert result["status"] == "error"
    assert result["error_code"] == "sensitive_command_denied"
    assert manager.unlock_state.refresh_owner is None
    manager.client.send_command.assert_not_awaited()
    resolve_unlock_status_async.assert_awaited_once_with(manager, peer.uid)


@pytest.mark.parametrize(
    ("command", "payload"),
    [
        ("rename_macro", {"old": "Secret", "new": "Public"}),
        ("delete_macro", {"name": "Secret"}),
    ],
)
@pytest.mark.asyncio
async def test_macro_rename_and_delete_require_edit_unlock(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    payload: dict[str, str],
) -> None:
    manager = SessionManager()
    manager.security_policy.macro_edit_requires_unlock = True
    manager.client.send_command = AsyncMock()
    peer = PeerCredentials(pid=111, uid=1000, gid=1000)
    writer = object()
    resolve_unlock_status_async = AsyncMock(
        return_value={"unlocked": False, "source": "none", "expires_at": 0}
    )
    monkeypatch.setattr(
        recording_unlock_module,
        "resolve_unlock_status_async",
        resolve_unlock_status_async,
    )

    result = await manager._handle_session_request(
        {"command": command, **payload},
        peer,
        writer,  # type: ignore[arg-type]
    )

    assert result["status"] == "error"
    assert result["error_code"] == "sensitive_command_denied"
    manager.client.send_command.assert_not_awaited()


@pytest.mark.parametrize(
    "command",
    [
        "does_not_exist",
        "play_macro_payload",
        "play_compact_macro",
    ],
)
@pytest.mark.asyncio
async def test_handle_session_request_returns_unknown_command_error(command: str) -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    result = await manager._handle_session_request(
        {"command": command},
        peer,
        object(),
    )

    assert result == {"error": f"Unknown command: {command}"}


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
async def test_handle_session_request_get_compositor_reports_compositor_dispatch_availability() -> (
    None
):
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    listener = HyprlandListener(AsyncMock())
    listener.running = True
    manager.compositor_state.window_listener = listener
    manager.compositor_state.compositor_id = "hyprland"

    result = await manager._handle_session_request(
        {"command": "get_compositor"},
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
            "dispatcher": "toggle-window-floating",
            "args": "",
        },
        peer,
        object(),
    )

    assert result == {"status": "ok", "message": "ok"}
    listener.dispatch.assert_awaited_once_with("toggle-window-floating", "")


@pytest.mark.asyncio
async def test_handle_session_request_get_compositor_merges_listener_runtime_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
                "bridge_protocol": 0,
                "bridge_protocol_expected": 1,
            }

    manager.compositor_state.window_listener = _Listener()  # type: ignore[assignment]
    manager.compositor_state.compositor_id = "gnome"

    async def support_details(_compositor_id: str | None, _dbus=None) -> dict[str, bool | str]:
        return {"supported": True, "warning": ""}

    monkeypatch.setattr(
        session_compositor_module,
        "get_compositor_support_details",
        support_details,
    )

    result = await manager._handle_session_request(
        {"command": "get_compositor"},
        peer,
        object(),
    )

    assert result["supported"] is True
    assert result["compositor_dispatch_available"] is False
    details = cast(dict[str, object], result["details"])
    assert details["bridge_protocol"] == 0
    assert "Log out and back in" in str(details["warning"])
