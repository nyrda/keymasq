# ruff: noqa: F403, F405, I001
from tests.session.command_support import *
from keymasq.common.ipc import CommandType


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
async def test_handle_session_request_refresh_compositor_forces_binding_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    refresh = AsyncMock(return_value={"compositor_id": "gnome", "supported": True})
    monkeypatch.setattr(session_compositor_module, "refresh_compositor_binding", refresh)

    result = await manager._handle_session_request(
        {"command": "refresh_compositor"},
        "client",
        peer,
        object(),
    )

    assert result == {"compositor_id": "gnome", "supported": True}
    refresh.assert_awaited_once_with(manager)


@pytest.mark.asyncio
async def test_handle_session_request_run_compositor_setup_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    run_action = AsyncMock(
        return_value={
            "status": "ok",
            "message": "GNOME bridge enabled. Waiting for Keymasq to connect.",
        }
    )
    monkeypatch.setattr(session_compositor_module, "run_compositor_setup_action", run_action)

    result = await manager._handle_session_request(
        {
            "command": "run_compositor_setup_action",
            "compositor": "gnome",
            "action": "enable_bridge",
        },
        "client",
        peer,
        object(),
    )

    assert result["status"] == "ok"
    assert "GNOME bridge enabled" in str(result["message"])
    run_action.assert_awaited_once_with(manager, "gnome", "enable_bridge")


@pytest.mark.asyncio
async def test_get_status_uses_async_unlock_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = SessionManager()
    manager.security_policy.recording_unlock_required = True
    manager.security_policy.emergency_cancel_combo_enabled = False
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
    assert result["emergency_cancel_combo_enabled"] is False
    assert result["recording_unlock_source"] == "runtime"
    assert result["recording_unlock_expires_at"] == 1234
    resolve_unlock_status_async.assert_awaited_once_with(manager, peer.uid)


@pytest.mark.asyncio
async def test_get_active_profiles_does_not_include_window_state() -> None:
    manager = SessionManager()
    manager.profile_state.active_profile_names = ["Gaming"]
    manager.profile_state.resolved_devices = {
        "1234:5678": SimpleNamespace(
            active_profile_names=["Gaming"],
            mapping_count=2,
            always_grab_all=False,
        )
    }
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        name="Gaming Keyboard"
    )
    manager.compositor_state.current_window = {
        "class": "steam",
        "title": "Counter-Strike 2",
        "tags": ["game"],
    }
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    result = await manager._handle_session_request(
        {"command": "get_active_profiles"},
        "client",
        peer,
        object(),
    )

    assert result["status"] == "ok"
    assert result["active_profiles"] == ["Gaming"]
    assert "devices" in result
    devices = cast(dict[str, dict[str, object]], result["devices"])
    assert devices["1234:5678"]["device_name"] == "Gaming Keyboard"
    assert "window" not in result


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
async def test_play_macro_payload_forwards_sanitized_events() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    sent_commands = []

    async def send_command(command):
        sent_commands.append(command)
        return Response(status="ok", data={"status": "ok", "played": True})

    manager.client.send_command = send_command  # type: ignore[method-assign]
    manager.security_policy.macro_exec_timeout_max_ms = 100

    result = await manager._handle_session_request(
        {
            "command": "play_macro_payload",
            "macro_events": [
                {
                    "device_type": "macro",
                    "macro_action": "exec_sync",
                    "timeout_ms": 999,
                    "t_us": 0,
                }
            ],
            "speed": 1.5,
        },
        "client",
        peer,
        object(),
    )

    assert result == {"status": "ok", "played": True}
    assert sent_commands[0].command == CommandType.PLAY_MACRO
    assert sent_commands[0].data["speed"] == 1.5
    assert sent_commands[0].data["macro_events"][0]["timeout_ms"] == 100


@pytest.mark.asyncio
async def test_type_text_compiles_and_forwards_events() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    sent_commands = []

    async def send_command(command):
        sent_commands.append(command)
        return Response(status="ok", data={"status": "ok"})

    manager.client.send_command = send_command  # type: ignore[method-assign]

    result = await manager._handle_session_request(
        {
            "command": "type_text",
            "text": "Hi",
            "down_ms": 0,
            "pause_ms": 0,
            "speed": 1.25,
        },
        "client",
        peer,
        object(),
    )

    assert result["status"] == "ok"
    assert result["char_count"] == 2
    assert result["event_count"] == len(sent_commands[0].data["macro_events"])
    assert sent_commands[0].command == CommandType.PLAY_MACRO
    assert sent_commands[0].data["speed"] == 1.25
    assert len(sent_commands[0].data["macro_events"]) > 0


@pytest.mark.asyncio
async def test_play_compact_macro_compiles_and_forwards_events() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    sent_commands = []

    async def send_command(command):
        sent_commands.append(command)
        return Response(status="ok", data={"status": "ok"})

    manager.client.send_command = send_command  # type: ignore[method-assign]

    result = await manager._handle_session_request(
        {
            "command": "play_compact_macro",
            "tokens": ["key_a", "wait:10:20", "btn_left"],
            "speed": 0.5,
        },
        "client",
        peer,
        object(),
    )

    assert result["status"] == "ok"
    assert result["event_count"] == len(sent_commands[0].data["macro_events"])
    assert sent_commands[0].command == CommandType.PLAY_MACRO
    assert sent_commands[0].data["speed"] == 0.5
    wait_random = next(
        event
        for event in sent_commands[0].data["macro_events"]
        if event.get("macro_action") == "wait_random"
    )
    assert wait_random["min_us"] == 10_000
    assert wait_random["max_us"] == 20_000


@pytest.mark.asyncio
async def test_play_macro_payload_requires_events() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    result = await manager._handle_session_request(
        {"command": "play_macro_payload", "macro_events": []},
        "client",
        peer,
        object(),
    )

    assert result == {"status": "error", "message": "macro_events required"}


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
    start_recording.assert_awaited_once_with(
        manager,
        reset_if_active=False,
        owner_peer=peer,
        owner_writer=writer,
    )
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_start_macro_trigger_warns_when_gui_is_missing() -> None:
    manager = SessionManager()
    manager.send_notification = Mock()  # type: ignore[method-assign]
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    start_recording = AsyncMock(return_value={"status": "ok"})
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(session_recording_module, "start_recording", start_recording)

    await session_events_module.handle_start_macro_trigger(manager)

    manager.send_notification.assert_called_once_with(  # type: ignore[attr-defined]
        "Keymasq: Recording Unavailable",
        "Macro recording from triggers requires Keymasq GUI to be open.",
    )
    manager.broadcast_to_session_clients.assert_called_once_with(  # type: ignore[attr-defined]
        {"event": "recording_auth_requested"}
    )
    start_recording.assert_not_awaited()
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_start_macro_trigger_warns_when_gui_is_open_but_locked() -> None:
    manager = SessionManager()
    manager.session_clients.add(object())  # type: ignore[arg-type]
    manager.send_notification = Mock()  # type: ignore[method-assign]
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    start_recording = AsyncMock(return_value={"status": "ok"})
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(session_recording_module, "start_recording", start_recording)

    await session_events_module.handle_start_macro_trigger(manager)

    manager.send_notification.assert_called_once_with(  # type: ignore[attr-defined]
        "Keymasq: Recording Locked",
        "Unlock macro recording in Keymasq GUI before using recording triggers.",
    )
    manager.broadcast_to_session_clients.assert_called_once_with(  # type: ignore[attr-defined]
        {"event": "recording_auth_requested"}
    )
    start_recording.assert_not_awaited()
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_start_macro_trigger_blocks_when_macro_save_is_pending() -> None:
    manager = SessionManager()
    manager.recording_state.pending_data = {"events": [{"t_us": 0}]}
    manager.recording_state.pending_save_token = "pending-1"
    manager.unlock_state.refresh_owner = {
        "uid": 1000,
        "pid": 111,
        "writer_id": 222,
        "lease_id": "lease-1",
    }
    manager.send_notification = Mock()  # type: ignore[method-assign]
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    manager.client.send_command = AsyncMock()

    await session_events_module.handle_start_macro_trigger(manager)

    manager.send_notification.assert_called_once_with(  # type: ignore[attr-defined]
        "Keymasq: Macro Save Pending",
        "Save or discard the current recording before starting another recording.",
    )
    manager.broadcast_to_session_clients.assert_called_once_with(  # type: ignore[attr-defined]
        {
            "event": "macro_save_pending",
            "message": "Save or discard the current recording before starting another recording.",
            "pending_save_token": "pending-1",
        }
    )
    manager.client.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_macro_playback_cancelled_event_notifies_user() -> None:
    manager = SessionManager()
    manager.send_notification = Mock()  # type: ignore[method-assign]
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]

    await session_events_module.handle_event(
        manager,
        CommandType.MACRO_PLAYBACK_CANCELLED,
        {"reason": "cancel_macro_playback", "cancelled": True},
    )

    manager.broadcast_to_session_clients.assert_called_once_with(  # type: ignore[attr-defined]
        {
            "event": "macro_playback_cancelled",
            "reason": "cancel_macro_playback",
            "cancelled": True,
        }
    )
    manager.send_notification.assert_called_once_with(  # type: ignore[attr-defined]
        "Keymasq: Macro Playback Cancelled",
        "Stopped all running macro playback.",
    )


@pytest.mark.asyncio
async def test_runtime_reset_event_invalidates_and_reevaluates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.profile_state.grabbed_devices.add("1234:5678")
    manager.profile_state.last_sent_grab_signatures["1234:5678"] = "grab"
    manager.profile_state.last_sent_mapping_signatures["1234:5678"] = "mapping"
    manager.profile_state.last_sent_combo_signature = "combos"
    manager.send_notification = Mock()  # type: ignore[method-assign]
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    reevaluate_profiles = AsyncMock()
    monkeypatch.setattr(session_profiles_module, "reevaluate_profiles", reevaluate_profiles)

    await session_events_module.handle_event(
        manager,
        CommandType.RUNTIME_RESET,
        {"reason": "emergency_reset"},
    )
    await asyncio.sleep(0)

    assert manager.profile_state.grabbed_devices == set()
    assert manager.profile_state.last_sent_grab_signatures == {}
    assert manager.profile_state.last_sent_mapping_signatures == {}
    assert manager.profile_state.last_sent_combo_signature == ""
    reevaluate_profiles.assert_awaited_once_with(manager, reason="runtime reset")
    manager.broadcast_to_session_clients.assert_called_once_with(  # type: ignore[attr-defined]
        {"event": "runtime_reset", "reason": "emergency_reset"}
    )
    manager.send_notification.assert_called_once_with(  # type: ignore[attr-defined]
        "Keymasq: Emergency Reset",
        "Released all grabbed devices. Reapplying active profiles.",
    )


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
    refresh_macro_bindings = AsyncMock()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        session_profiles_module,
        "refresh_macro_bindings",
        refresh_macro_bindings,
    )

    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    try:
        result = await manager._handle_session_request(
            {"command": "create_macro", "macro": {"name": "Speedrun"}},
            "client",
            peer,
            object(),
        )
    finally:
        monkeypatch.undo()

    assert result == {"status": "ok", "macro": {"name": "Speedrun"}}
    refresh_macro_bindings.assert_awaited_once_with(manager)
    manager.broadcast_to_session_clients.assert_called_once_with(  # type: ignore[attr-defined]
        {"event": "macro_saved", "name": "Speedrun"}
    )


@pytest.mark.asyncio
async def test_save_recording_clears_pending_macro_save_state() -> None:
    manager = SessionManager()
    manager.recording_state.pending_data = {
        "pending_recording_id": "recording-1",
        "duration_ms": 10,
        "device_types": ["keyboard"],
        "event_count": 1,
    }
    manager.recording_state.pending_save_token = "pending-1"
    manager.recording_state.pending_save_owner_writer_id = 123
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"macro": {"name": "Saved"}})
    )
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    result = await manager._handle_session_request(
        {
            "command": "save_recording",
            "name": "Saved",
            "pending_save_token": "pending-1",
        },
        "client",
        peer,
        object(),
    )

    assert result == {"status": "ok", "name": "Saved"}
    sent_command = manager.client.send_command.await_args.args[0]
    assert sent_command.command == CommandType.MACRO_SAVE_RECORDING
    assert sent_command.data["pending_recording_id"] == "recording-1"
    assert manager.recording_state.pending_data is None
    assert manager.recording_state.pending_save_token is None
    assert manager.recording_state.pending_save_owner_writer_id is None


@pytest.mark.asyncio
async def test_discard_recording_rejects_stale_pending_macro_save_token() -> None:
    manager = SessionManager()
    manager.recording_state.pending_data = {"events": [{"t_us": 0}]}
    manager.recording_state.pending_save_token = "current"
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    result = await manager._handle_session_request(
        {"command": "discard_recording", "pending_save_token": "stale"},
        "client",
        peer,
        object(),
    )

    assert result["status"] == "error"
    assert result["error_code"] == "stale_pending_macro_save"
    assert manager.recording_state.pending_data == {"events": [{"t_us": 0}]}
    assert manager.recording_state.pending_save_token == "current"


@pytest.mark.asyncio
async def test_discard_recording_clears_pending_macro_save_state() -> None:
    manager = SessionManager()
    manager.recording_state.pending_data = {"events": [{"t_us": 0}]}
    manager.recording_state.pending_save_token = "pending-1"
    manager.recording_state.pending_save_owner_writer_id = 123
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    result = await manager._handle_session_request(
        {"command": "discard_recording", "pending_save_token": "pending-1"},
        "client",
        peer,
        object(),
    )

    assert result == {"status": "ok"}
    assert manager.recording_state.pending_data is None
    assert manager.recording_state.pending_save_token is None
    assert manager.recording_state.pending_save_owner_writer_id is None


@pytest.mark.asyncio
async def test_handle_session_request_update_macro_refreshes_runtime_bindings() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"macro": {"name": "Speedrun"}})
    )
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    refresh_macro_bindings = AsyncMock()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        session_profiles_module,
        "refresh_macro_bindings",
        refresh_macro_bindings,
    )

    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    try:
        result = await manager._handle_session_request(
            {"command": "update_macro", "name": "Speedrun", "macro": {"name": "Speedrun"}},
            "client",
            peer,
            object(),
        )
    finally:
        monkeypatch.undo()

    assert result == {"status": "ok", "macro": {"name": "Speedrun"}}
    refresh_macro_bindings.assert_awaited_once_with(manager)
    manager.broadcast_to_session_clients.assert_called_once_with(  # type: ignore[attr-defined]
        {"event": "macro_saved", "name": "Speedrun"}
    )


@pytest.mark.asyncio
async def test_handle_session_request_delete_macro_broadcasts_deleted_event() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(return_value=Response(status="ok", data={}))
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    refresh_macro_bindings = AsyncMock()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        session_profiles_module,
        "refresh_macro_bindings",
        refresh_macro_bindings,
    )

    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    try:
        result = await manager._handle_session_request(
            {"command": "delete_macro", "name": "Speedrun"},
            "client",
            peer,
            object(),
        )
    finally:
        monkeypatch.undo()

    assert result == {"status": "ok"}
    refresh_macro_bindings.assert_awaited_once_with(manager)
    manager.broadcast_to_session_clients.assert_called_once_with(  # type: ignore[attr-defined]
        {"event": "macro_deleted", "name": "Speedrun"}
    )


@pytest.mark.asyncio
async def test_capture_combo_session_command_round_trip() -> None:
    manager = SessionManager()
    manager.hardware.list_hardware_ids = lambda: ["1234:5678"]  # type: ignore[assignment]
    manager.profiles.get_profile = Mock(
        return_value=SimpleNamespace(config=SimpleNamespace(device_layers={"1234:5678": object()}))
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
