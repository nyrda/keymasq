from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

import keymasq.session.manager.compositor as session_compositor_module
import keymasq.session.manager.recording as session_recording_module
from keymasq.common.security import PeerCredentials, SecurityPolicy
from keymasq.session.listeners.hyprland import HyprlandListener
from keymasq.session.manager import SessionManager
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
    monkeypatch.setattr(session_recording_module, "lock_recording_unlock", lock_recording_unlock)
    grant_recording_refresh_owner(
        manager,
        peer,
        owner_writer,
        monkeypatch,
        lease_id="lease-1",
    )

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
        session_recording_module,
        "resolve_unlock_status_async",
        resolve_unlock_status_async,
    )

    result = await manager._handle_session_request(
        {"command": "get_macro", "name": "Secret"},
        "client",
        peer,
        writer,  # type: ignore[arg-type]
    )

    assert result["status"] == "error"
    assert result["error_code"] == "sensitive_command_denied"
    assert manager.unlock_state.refresh_owner is None
    manager.client.send_command.assert_not_awaited()
    resolve_unlock_status_async.assert_awaited_once_with(manager, peer.uid)


@pytest.mark.asyncio
async def test_session_request_uses_single_policy_snapshot_for_acl_and_sensitivity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=111, uid=1000, gid=1000)
    writer = object()
    capture_begin_for_paths = AsyncMock(return_value={"status": "ok"})
    manager.security_policy = SecurityPolicy(
        session_command_acl={"client": []},
        daemon_command_acl={"session": []},
        recording_unlock_required=True,
    )

    def fake_command_allowed(command: str, acl: dict[str, list[str]], client_class: str) -> bool:
        assert command == "begin_capture"
        assert acl is manager.security_policy.session_command_acl
        assert client_class == "client"
        manager.security_policy = SecurityPolicy(
            session_command_acl={"client": []},
            daemon_command_acl={"session": []},
            recording_unlock_required=False,
        )
        return True

    monkeypatch.setattr("keymasq.session.manager.commands.command_allowed", fake_command_allowed)
    monkeypatch.setattr(
        session_recording_module,
        "capture_begin_for_paths",
        capture_begin_for_paths,
    )

    result = await manager._handle_session_request(
        {"command": "begin_capture", "hardware_id": "1234:5678"},
        "client",
        peer,
        writer,  # type: ignore[arg-type]
    )

    assert result["status"] == "error"
    assert result["error_code"] == "sensitive_command_denied"
    capture_begin_for_paths.assert_not_awaited()


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
async def test_get_status_omits_acl_gated_profile_window_and_mpris_sections() -> None:
    manager = SessionManager()
    manager.security_policy = SecurityPolicy(
        session_command_acl={"client": ["!get_active_profiles", "!get_active_window", "!mpris"]},
        daemon_command_acl={"session": []},
    )
    manager.profile_state.active_profile_names = ["Gaming"]
    manager.profile_state.resolved_devices = {
        "1234:5678": SimpleNamespace(
            active_profile_names=["Gaming"],
            mapping_count=3,
            always_grab_all=False,
        )
    }
    manager.compositor_state.current_window = {
        "class": "steam",
        "title": "Counter-Strike 2",
        "tags": ["game"],
    }
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    result = await manager._handle_session_request(
        {"command": "get_status"},
        "client",
        peer,
        object(),
    )

    assert result["status"] == "ok"
    assert "active_profiles" not in result
    assert "devices" not in result
    assert "window" not in result
    assert "mpris" not in result


@pytest.mark.asyncio
async def test_combo_inspector_snapshot_requires_active_profile_permission() -> None:
    from keymasq.common.models import ActionType, ComboEvent, ComboStep, MappingAction
    from keymasq.session.profiles import ResolvedCombo

    manager = SessionManager()
    manager.security_policy = SecurityPolicy(
        session_command_acl={"client": ["!get_active_profiles"]},
        daemon_command_acl={"session": []},
    )
    manager.profile_state.active_profile_names = ["Base", "Overlay"]
    manager.profile_state.resolved_combos = [
        ResolvedCombo(
            id="combo-1",
            name="Quick Save",
            profile_name="Overlay",
            steps=[
                ComboStep(
                    events=[
                        ComboEvent(
                            evdev="key_s",
                            hardware_id="1234:5678",
                            source="kbd",
                        )
                    ],
                )
            ],
            action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
        )
    ]
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    result = await manager._handle_session_request(
        {"command": "get_combo_inspector_snapshot"},
        "client",
        peer,
        object(),
    )

    assert result["status"] == "error"
    assert "get_active_profiles" in str(result["message"])
    assert "combos" not in result


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
            "dispatcher": "toggle-window-floating",
            "args": "",
        },
        "client",
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
        "client",
        peer,
        object(),
    )

    assert result["supported"] is True
    assert result["compositor_dispatch_available"] is False
    details = cast(dict[str, object], result["details"])
    assert details["bridge_protocol"] == 0
    assert "Log out and back in" in str(details["warning"])
