from typing import cast
from unittest.mock import AsyncMock

import pytest

import keymasq.session.manager.compositor as session_compositor_module
from keymasq.common.ipc import Response
from keymasq.common.security import PeerCredentials
from keymasq.session.listeners.hyprland import HyprlandListener
from keymasq.session.manager.command import hardware_masking
from keymasq.session.manager.core import SessionManager
from keymasq.session.manager.profile import coordinator as profile_coordinator


@pytest.mark.parametrize("command", hardware_masking.COMMANDS)
@pytest.mark.asyncio
async def test_hardware_masking_commands_forward_to_daemon(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = SessionManager()
    manager.connected = True
    manager.client.send_command = AsyncMock(return_value=Response(status="ok", data={"masks": []}))
    monkeypatch.setattr(profile_coordinator, "reevaluate_profiles", AsyncMock())
    result = await manager._handle_session_request(
        {"command": command}, PeerCredentials(pid=111, uid=1000, gid=1000), object()
    )
    assert result == {"status": "ok", "masks": []}
    manager.client.send_command.assert_awaited_once()


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
        active_layer = ""

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
