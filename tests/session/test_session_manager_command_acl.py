# ruff: noqa: F403, F405, I001
from tests.session.command_support import *


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

    monkeypatch.setattr("keymasq.session.manager.commands.command_allowed", fake_command_allowed)
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
async def test_get_status_omits_acl_gated_profile_and_window_sections() -> None:
    manager = SessionManager()
    manager.security_policy = SecurityPolicy(
        session_command_acl={"client": ["!get_active_profiles", "!get_active_window"]},
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
                "bridge_protocol": 0,
                "bridge_protocol_expected": 1,
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
    assert details["bridge_protocol"] == 0
    assert "Log out and back in" in str(details["warning"])
