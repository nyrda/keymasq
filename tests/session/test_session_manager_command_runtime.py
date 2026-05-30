# ruff: noqa: F403, F405, I001
import json

from tests.session.command_support import *
from keymasq.common import paths
from keymasq.common.ipc import CommandType
from keymasq.common.settings import GlobalSettings
import keymasq.session.settings as session_settings
import keymasq.session.manager.commands as session_commands_module
import keymasq.session.manager.device_inspector as session_device_inspector_module


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
async def test_handle_session_request_set_settings_does_not_broadcast_on_daemon_error(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(paths, "CONFIG_DIR", tmp_path / "keymasq")
    session_settings.save_global_settings(GlobalSettings(virtual_gamepad_count=3))
    manager = SessionManager()
    manager.connected = True
    manager.client.send_command = AsyncMock(
        return_value=Response(status="error", error="daemon rejected count")
    )
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    result = await manager._handle_session_request(
        {"command": "set_settings", "virtual_gamepad_count": 2},
        "client",
        peer,
        object(),
    )

    assert result["status"] == "error"
    assert result["message"] == "daemon rejected count"
    assert result["virtual_gamepad_count"] == 3
    assert manager.virtual_gamepad_count == 3
    assert session_settings.load_global_settings().virtual_gamepad_count == 3
    manager.broadcast_to_session_clients.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_handle_session_request_set_virtual_gamepads_keeps_state_on_daemon_error(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(paths, "CONFIG_DIR", tmp_path / "keymasq")
    session_settings.save_global_settings(GlobalSettings(virtual_gamepad_count=3))
    manager = SessionManager()
    manager.connected = True
    manager.client.send_command = AsyncMock(
        return_value=Response(status="error", error="daemon rejected count")
    )
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    result = await manager._handle_session_request(
        {"command": "set_virtual_gamepads", "count": 2},
        "client",
        peer,
        object(),
    )

    assert result == {"status": "error", "message": "daemon rejected count"}
    sent = manager.client.send_command.await_args.args[0]
    assert sent.command == CommandType.SET_VIRTUAL_GAMEPADS
    assert sent.data == {"count": 2}
    assert manager.virtual_gamepad_count == 3
    assert session_settings.load_global_settings().virtual_gamepad_count == 3
    manager.broadcast_to_session_clients.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_release_device_command_forwards_to_daemon_and_clears_runtime_state() -> None:
    manager = SessionManager()
    hardware_id = "045e:02a1"
    manager.profile_state.grabbed_devices.add(hardware_id)
    manager.profile_state.grabbed_interfaces[hardware_id] = {
        "gamepad": "/dev/input/event20"
    }
    manager.profile_state.grab_waiting_devices.add(hardware_id)
    manager.profile_state.last_sent_grab_signatures[hardware_id] = "grab"
    manager.profile_state.last_sent_mapping_signatures[hardware_id] = "mapping"
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"released": True})
    )
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    result = await manager._handle_session_request(
        {"command": "release_device", "hardware_id": hardware_id},
        "client",
        peer,
        object(),
    )

    assert result == {"released": True, "status": "ok"}
    sent = manager.client.send_command.await_args.args[0]
    assert sent.command == CommandType.RELEASE_DEVICE
    assert sent.data == {"hardware_id": hardware_id, "immediate": True}
    assert hardware_id not in manager.profile_state.grabbed_devices
    assert hardware_id not in manager.profile_state.grabbed_interfaces
    assert hardware_id not in manager.profile_state.grab_waiting_devices
    assert hardware_id not in manager.profile_state.last_sent_grab_signatures
    assert hardware_id not in manager.profile_state.last_sent_mapping_signatures


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
async def test_get_combo_inspector_snapshot_returns_resolved_active_combos() -> None:
    from keymasq.common.models import ActionType, ComboEvent, ComboStep, MappingAction
    from keymasq.session.profiles import ResolvedCombo

    manager = SessionManager()
    manager.profile_state.active_profile_names = ["Base", "Overlay"]
    manager.profile_state.resolved_combos = [
        ResolvedCombo(
            id="combo-1",
            name="Quick Save",
            profile_name="Overlay",
            steps=[
                ComboStep(events=[ComboEvent(evdev="")], timeout_ms=250),
                ComboStep(
                    events=[
                        ComboEvent(
                            evdev="key_s",
                            hardware_id="1234:5678",
                            source="kbd",
                        )
                    ],
                    timeout_ms=500,
                )
            ],
            action=MappingAction(action_type=ActionType.KEYBOARD, target="key_f5"),
            recall_trigger_keys=True,
            restore_trigger_keys=["key_leftctrl"],
            match_across_devices=True,
        )
    ]
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        name="Gaming Keyboard"
    )
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    result = await manager._handle_session_request(
        {"command": "get_combo_inspector_snapshot"},
        "client",
        peer,
        object(),
    )

    assert result["status"] == "ok"
    assert result["active_profiles"] == ["Base", "Overlay"]
    combos = cast(list[dict[str, object]], result["combos"])
    assert combos[0]["profile_name"] == "Overlay"
    assert combos[0]["match_across_devices"] is True
    steps = cast(list[dict[str, object]], combos[0]["steps"])
    assert len(steps) == 1
    assert steps[0]["timeout_ms"] == 500
    events = cast(list[dict[str, object]], steps[0]["events"])
    assert events[0]["device_name"] == "Gaming Keyboard"
    action = cast(dict[str, object], combos[0]["action"])
    assert action["action"] == "keyboard"
    assert action["target"] == "key_f5"


@pytest.mark.asyncio
async def test_get_combo_inspector_snapshot_preserves_profile_deactivation() -> None:
    from keymasq.common.models import (
        ActionType,
        ComboEvent,
        ComboStep,
        MappingAction,
        ProfileDeactivationPolicy,
    )
    from keymasq.session.profiles import ResolvedCombo

    manager = SessionManager()
    manager.profile_state.resolved_combos = [
        ResolvedCombo(
            id="combo-1",
            name="Hold Layer",
            profile_name="Base",
            steps=[ComboStep(events=[ComboEvent(evdev="key_space")])],
            action=MappingAction(
                action_type=ActionType.PROFILE_TOGGLE,
                profile_name="Layer",
                profile_deactivation=ProfileDeactivationPolicy(on_trigger_end=True),
            ),
        )
    ]
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    result = await manager._handle_session_request(
        {"command": "get_combo_inspector_snapshot"},
        "client",
        peer,
        object(),
    )

    combos = cast(list[dict[str, object]], result["combos"])
    action = cast(dict[str, object], combos[0]["action"])
    assert action["action"] == "profile_toggle"
    assert action["profile_name"] == "Layer"
    assert action["deactivation"] == {"on_trigger_end": True}


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
async def test_type_text_compiles_in_thread_and_forwards_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    sent_commands = []
    to_thread_calls = []

    async def fake_to_thread(func, /, *args, **kwargs):
        to_thread_calls.append(func)
        return func(*args, **kwargs)

    async def send_command(command):
        sent_commands.append(command)
        return Response(status="ok", data={"status": "ok"})

    monkeypatch.setattr(session_commands_module.asyncio, "to_thread", fake_to_thread)
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
    assert to_thread_calls == [session_commands_module._compile_type_text_macro]


@pytest.mark.asyncio
async def test_play_compact_macro_compiles_in_thread_and_forwards_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    sent_commands = []
    to_thread_calls = []

    async def fake_to_thread(func, /, *args, **kwargs):
        to_thread_calls.append(func)
        return func(*args, **kwargs)

    async def send_command(command):
        sent_commands.append(command)
        return Response(status="ok", data={"status": "ok"})

    monkeypatch.setattr(session_commands_module.asyncio, "to_thread", fake_to_thread)
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
    assert to_thread_calls == [session_commands_module._compile_compact_macro]


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

    await session_events_module.handle_runtime_reset_event(
        manager,
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
async def test_reevaluate_profiles_command_invalidates_runtime_payload_signatures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.profile_state.last_sent_mapping_signatures["1234:5678"] = "mapping"
    manager.profile_state.last_sent_combo_signature = "combos"
    manager.reload_config_from_disk = Mock()  # type: ignore[method-assign]
    reevaluate_profiles = AsyncMock()
    monkeypatch.setattr(session_profiles_module, "reevaluate_profiles", reevaluate_profiles)
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    result = await manager._handle_session_request(
        {"command": "reevaluate_profiles"},
        "client",
        peer,
        object(),
    )

    assert result == {"status": "ok"}
    manager.reload_config_from_disk.assert_called_once_with()  # type: ignore[attr-defined]
    assert manager.profile_state.last_sent_mapping_signatures == {}
    assert manager.profile_state.last_sent_combo_signature == ""
    reevaluate_profiles.assert_awaited_once_with(
        manager,
        reason="session command reevaluate",
    )


@pytest.mark.asyncio
async def test_diagnostics_snapshot_event_forwards_to_gui_clients() -> None:
    manager = SessionManager()
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]

    await session_events_module.handle_event(
        manager,
        CommandType.DIAGNOSTICS_SNAPSHOT,
        {
            "enabled": True,
            "interval": 5.0,
            "categories": ["mainline"],
            "samples": {"passthrough_mapped": {"n": 2, "p50": 1.0}},
        },
    )

    manager.broadcast_to_session_clients.assert_called_once_with(  # type: ignore[attr-defined]
        {
            "event": "diagnostics_snapshot",
            "enabled": True,
            "interval": 5.0,
            "categories": ["mainline"],
            "samples": {"passthrough_mapped": {"n": 2, "p50": 1.0}},
        }
    )


@pytest.mark.asyncio
async def test_device_inspector_disable_session_command_forwards_reason() -> None:
    manager = SessionManager()
    manager.client = SimpleNamespace(
        send_command=AsyncMock(return_value=Response(status="ok", data={"status": "ok"}))
    )
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    result = await manager._handle_session_request(
        {
            "command": "disable_device_inspector_suppression",
            "hardware_id": "1234:5678",
            "reason": "key_esc",
        },
        "client",
        peer,
        object(),  # type: ignore[arg-type]
    )

    sent = manager.client.send_command.await_args.args[0]
    assert result == {"status": "ok"}
    assert sent.command == CommandType.DEVICE_INSPECTOR_DISABLE_SUPPRESSION
    assert sent.data == {"hardware_id": "1234:5678", "reason": "key_esc"}


@pytest.mark.asyncio
async def test_device_inspector_events_forward_only_to_owner_clients() -> None:
    manager = SessionManager()

    class Writer:
        def __init__(self) -> None:
            self.writes: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.writes.append(data)

        async def drain(self) -> None:
            return None

    owner = Writer()
    other = Writer()
    manager.session_clients.update({owner, other})  # type: ignore[arg-type]
    manager.device_inspector_state.owners_by_hardware_id["1234:5678"] = {id(owner)}

    await session_events_module.handle_event(
        manager,
        CommandType.DEVICE_INSPECTOR_EVENT,
        {
            "hardware_id": "1234:5678",
            "code_name": "key_esc",
            "value": 1,
            "suppressed": True,
        },
    )

    assert other.writes == []
    assert len(owner.writes) == 1
    assert json.loads(owner.writes[0]) == {
        "event": "device_inspector_event",
        "hardware_id": "1234:5678",
        "code_name": "key_esc",
        "value": 1,
        "suppressed": True,
    }

    for task in list(manager.session_client_drain_tasks.values()):
        await task


@pytest.mark.asyncio
async def test_device_inspector_status_forwards_to_gui_clients() -> None:
    manager = SessionManager()
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]

    await session_events_module.handle_event(
        manager,
        CommandType.DEVICE_INSPECTOR_STATUS,
        {
            "hardware_id": "1234:5678",
            "active": True,
            "suppressed": False,
            "reason": "key_esc",
        },
    )

    manager.broadcast_to_session_clients.assert_called_once_with(  # type: ignore[attr-defined]
        {
            "event": "device_inspector_status",
            "hardware_id": "1234:5678",
            "active": True,
            "suppressed": False,
            "reason": "key_esc",
        },
    )
    assert "1234:5678" in manager.device_inspector_state.active_hardware_ids
    assert "1234:5678" not in manager.device_inspector_state.suppressed_hardware_ids


@pytest.mark.asyncio
async def test_stop_device_inspector_preserves_error_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    hardware_id = "1234:5678"
    writer = object()
    manager.device_inspector_state.owners_by_hardware_id[hardware_id] = {id(writer), 2}

    monkeypatch.setattr(
        session_device_inspector_module,
        "build_device_inspector_snapshot",
        lambda _manager, _hardware_id: {
            "status": "error",
            "message": "snapshot failed",
        },
    )

    result = await session_device_inspector_module.stop_device_inspector(
        manager,
        hardware_id,
        writer,  # type: ignore[arg-type]
    )

    assert result == {"status": "error", "message": "snapshot failed"}


@pytest.mark.asyncio
async def test_stop_device_inspector_preserves_state_on_daemon_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    hardware_id = "1234:5678"
    writer = object()
    writer_id = id(writer)
    manager.device_inspector_state.owners_by_hardware_id[hardware_id] = {writer_id}
    manager.device_inspector_state.active_hardware_ids.add(hardware_id)
    manager.device_inspector_state.suppressed_hardware_ids.add(hardware_id)
    manager.client.send_command = AsyncMock(
        return_value=Response(status="error", error="daemon stop failed")
    )
    reevaluate_profiles = AsyncMock()
    monkeypatch.setattr(session_profiles_module, "reevaluate_profiles", reevaluate_profiles)

    result = await session_device_inspector_module.stop_device_inspector(
        manager,
        hardware_id,
        writer,  # type: ignore[arg-type]
    )

    sent = manager.client.send_command.await_args.args[0]
    assert sent.command == CommandType.DEVICE_INSPECTOR_STOP
    assert sent.data == {"hardware_id": hardware_id}
    assert result == {"status": "error", "message": "daemon stop failed"}
    assert manager.device_inspector_state.owners_by_hardware_id[hardware_id] == {writer_id}
    assert hardware_id in manager.device_inspector_state.active_hardware_ids
    assert hardware_id in manager.device_inspector_state.suppressed_hardware_ids
    reevaluate_profiles.assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_device_inspectors_for_writer_continues_after_stop_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    writer = object()
    writer_id = id(writer)
    stopped: list[str] = []
    manager.device_inspector_state.owners_by_hardware_id.update(
        {
            "bad": {writer_id},
            "good": {writer_id},
        }
    )

    async def stop(
        _manager: SessionManager,
        hardware_id: str,
        *,
        reason: str,
    ) -> dict[str, object]:
        stopped.append(hardware_id)
        if hardware_id == "bad":
            raise RuntimeError("stop failed")
        return {"status": "ok", "reason": reason}

    monkeypatch.setattr(session_device_inspector_module, "_stop_device_inspector_unlocked", stop)

    await session_device_inspector_module.clear_device_inspectors_for_writer(
        manager,
        writer,  # type: ignore[arg-type]
    )

    assert stopped == ["bad", "good"]
    assert manager.device_inspector_state.owners_by_hardware_id["bad"] == set()


@pytest.mark.asyncio
async def test_runtime_reset_clears_session_device_inspector_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    manager.send_notification = Mock()  # type: ignore[method-assign]
    reevaluate_profiles = AsyncMock()
    monkeypatch.setattr(session_profiles_module, "reevaluate_profiles", reevaluate_profiles)
    manager.device_inspector_state.active_hardware_ids.add("1234:5678")
    manager.device_inspector_state.suppressed_hardware_ids.add("1234:5678")
    manager.device_inspector_state.owners_by_hardware_id["1234:5678"] = {1}

    await session_events_module.handle_runtime_reset_event(
        manager,
        {"reason": "emergency_reset"},
    )

    assert manager.device_inspector_state.active_hardware_ids == set()
    assert manager.device_inspector_state.suppressed_hardware_ids == set()
    assert manager.device_inspector_state.owners_by_hardware_id == {}
    manager.broadcast_to_session_clients.assert_called_once_with(  # type: ignore[attr-defined]
        {"event": "runtime_reset", "reason": "emergency_reset"}
    )
    reevaluate_profiles.assert_awaited_once_with(manager, reason="runtime reset")


@pytest.mark.asyncio
async def test_start_device_inspector_returns_snapshot_and_forces_profile_reevaluate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from keymasq.common.models import (
        ActionType,
        AnalogAxisDefinition,
        AnalogInputDefinition,
        ButtonDefinition,
        DeviceType,
        EvdevDevice,
        HardwareConfig,
        MappingAction,
    )
    from keymasq.session.profiles import ResolvedDeviceProfile

    manager = SessionManager()
    manager.security_policy.recording_unlock_required = False
    hardware_id = "1234:5678"
    manager.hardware.get_hardware = lambda _hardware_id: HardwareConfig(  # type: ignore[assignment]
        vendor_id="1234",
        product_id="5678",
        name="Inspector Pad",
        evdev_devices=[
            EvdevDevice(
                path="/dev/input/event10",
                device_type=DeviceType.GAMEPAD,
                id="pad",
            )
        ],
        buttons=[
            ButtonDefinition(
                id="btn_south",
                label="A",
                evdev="btn_south",
                evdev_code=304,
                source="pad",
            )
        ],
        analog_inputs=[
            AnalogInputDefinition(
                id="left_stick",
                label="Left Stick",
                type="stick",
                source="pad",
                axes=[
                    AnalogAxisDefinition(role="x", evdev="abs_x", evdev_code=0),
                    AnalogAxisDefinition(role="y", evdev="abs_y", evdev_code=1),
                ],
            )
        ],
    )
    manager.profile_state.resolved_devices[hardware_id] = ResolvedDeviceProfile(
        hardware_id=hardware_id,
        active_profile_names=["Desktop"],
        mappings={
            "btn_south": MappingAction(action_type=ActionType.KEYBOARD, target="key_space")
        },
        mapping_profile_names={"btn_south": "Desktop"},
    )
    manager.client.send_command = AsyncMock(
        return_value=Response(
            status="ok",
            data={"hardware_id": hardware_id, "active": True, "suppressed": False},
        )
    )
    reevaluate_profiles = AsyncMock()
    monkeypatch.setattr(session_profiles_module, "reevaluate_profiles", reevaluate_profiles)

    result = await manager._handle_session_request(
        {"command": "start_device_inspector", "hardware_id": hardware_id},
        "client",
        PeerCredentials(pid=1, uid=1000, gid=1000),
        object(),  # type: ignore[arg-type]
    )

    sent = manager.client.send_command.await_args.args[0]
    assert sent.command == CommandType.DEVICE_INSPECTOR_START
    assert sent.data == {"hardware_id": hardware_id}
    reevaluate_profiles.assert_awaited_once_with(
        manager,
        reason=f"device inspector start {hardware_id}",
    )
    assert result["status"] == "ok"
    assert result["active"] is True
    assert result["suppressed"] is False
    assert result["device_name"] == "Inspector Pad"
    assert result["active_profiles"] == ["Desktop"]
    buttons = cast(list[dict[str, object]], result["buttons"])
    action = cast(dict[str, object], buttons[0]["action"])
    analog_inputs = cast(list[dict[str, object]], result["analog_inputs"])
    axes = cast(list[dict[str, object]], analog_inputs[0]["axes"])
    assert buttons[0]["profile_name"] == "Desktop"
    assert action["target"] == "key_space"
    assert axes[0]["evdev"] == "abs_x"
    assert hardware_id in manager.device_inspector_state.active_hardware_ids


@pytest.mark.asyncio
async def test_enable_device_inspector_suppression_requires_successful_grab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from keymasq.common.models import DeviceType, EvdevDevice, HardwareConfig

    manager = SessionManager()
    manager.security_policy.recording_unlock_required = False
    hardware_id = "1234:5678"
    writer = object()
    manager.hardware.get_hardware = lambda _hardware_id: HardwareConfig(  # type: ignore[assignment]
        vendor_id="1234",
        product_id="5678",
        name="Inspector Pad",
        evdev_devices=[
            EvdevDevice(path="/dev/input/event10", device_type=DeviceType.GAMEPAD, id="pad")
        ],
        buttons=[],
    )
    manager.client.send_command = AsyncMock(return_value=Response(status="ok", data={}))
    reevaluate_profiles = AsyncMock()
    monkeypatch.setattr(session_profiles_module, "reevaluate_profiles", reevaluate_profiles)

    result = await manager._handle_session_request(
        {"command": "enable_device_inspector_suppression", "hardware_id": hardware_id},
        "client",
        PeerCredentials(pid=1, uid=1000, gid=1000),
        writer,  # type: ignore[arg-type]
    )

    assert result["status"] == "error"
    assert "could not grab" in str(result["message"])
    assert reevaluate_profiles.await_count == 2
    assert reevaluate_profiles.await_args_list[0].kwargs == {
        "reason": f"device inspector suppression grab {hardware_id}"
    }
    assert reevaluate_profiles.await_args_list[1].kwargs == {
        "reason": f"device inspector suppression rollback {hardware_id}"
    }
    manager.client.send_command.assert_not_awaited()
    assert hardware_id not in manager.device_inspector_state.active_hardware_ids
    assert manager.device_inspector_state.owners_by_hardware_id == {}


@pytest.mark.asyncio
async def test_enable_device_inspector_suppression_rolls_back_on_daemon_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from keymasq.common.models import DeviceType, EvdevDevice, HardwareConfig

    manager = SessionManager()
    manager.security_policy.recording_unlock_required = False
    hardware_id = "1234:5678"
    writer = object()
    manager.hardware.get_hardware = lambda _hardware_id: HardwareConfig(  # type: ignore[assignment]
        vendor_id="1234",
        product_id="5678",
        name="Inspector Pad",
        evdev_devices=[
            EvdevDevice(path="/dev/input/event10", device_type=DeviceType.GAMEPAD, id="pad")
        ],
        buttons=[],
    )
    manager.profile_state.grabbed_devices.add(hardware_id)
    manager.client.send_command = AsyncMock(
        return_value=Response(status="error", error="daemon rejected suppression")
    )
    reevaluate_profiles = AsyncMock()
    monkeypatch.setattr(session_profiles_module, "reevaluate_profiles", reevaluate_profiles)

    result = await manager._handle_session_request(
        {"command": "enable_device_inspector_suppression", "hardware_id": hardware_id},
        "client",
        PeerCredentials(pid=1, uid=1000, gid=1000),
        writer,  # type: ignore[arg-type]
    )

    assert result == {
        "status": "error",
        "message": "daemon rejected suppression",
    }
    assert reevaluate_profiles.await_count == 2
    assert reevaluate_profiles.await_args_list[0].kwargs == {
        "reason": f"device inspector suppression grab {hardware_id}"
    }
    assert reevaluate_profiles.await_args_list[1].kwargs == {
        "reason": f"device inspector suppression rollback {hardware_id}"
    }
    assert hardware_id not in manager.device_inspector_state.active_hardware_ids
    assert manager.device_inspector_state.owners_by_hardware_id == {}


@pytest.mark.asyncio
async def test_enable_device_inspector_suppression_preserves_existing_inspector_on_daemon_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from keymasq.common.models import DeviceType, EvdevDevice, HardwareConfig

    manager = SessionManager()
    manager.security_policy.recording_unlock_required = False
    hardware_id = "1234:5678"
    writer = object()
    writer_id = id(writer)
    manager.hardware.get_hardware = lambda _hardware_id: HardwareConfig(  # type: ignore[assignment]
        vendor_id="1234",
        product_id="5678",
        name="Inspector Pad",
        evdev_devices=[
            EvdevDevice(path="/dev/input/event10", device_type=DeviceType.GAMEPAD, id="pad")
        ],
        buttons=[],
    )
    manager.profile_state.grabbed_devices.add(hardware_id)
    manager.device_inspector_state.active_hardware_ids.add(hardware_id)
    manager.device_inspector_state.owners_by_hardware_id[hardware_id] = {writer_id}
    manager.client.send_command = AsyncMock(
        return_value=Response(status="error", error="daemon rejected suppression")
    )
    reevaluate_profiles = AsyncMock()
    monkeypatch.setattr(session_profiles_module, "reevaluate_profiles", reevaluate_profiles)

    result = await manager._handle_session_request(
        {"command": "enable_device_inspector_suppression", "hardware_id": hardware_id},
        "client",
        PeerCredentials(pid=1, uid=1000, gid=1000),
        writer,  # type: ignore[arg-type]
    )

    assert result == {
        "status": "error",
        "message": "daemon rejected suppression",
    }
    reevaluate_profiles.assert_awaited_once_with(
        manager,
        reason=f"device inspector suppression grab {hardware_id}",
    )
    assert hardware_id in manager.device_inspector_state.active_hardware_ids
    assert manager.device_inspector_state.owners_by_hardware_id[hardware_id] == {writer_id}


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
async def test_begin_capture_rejects_duplicate_for_same_hardware() -> None:
    manager = SessionManager()
    hardware_id = "2dc8:3106"
    manager.client.send_command = AsyncMock(
        side_effect=[
            Response(status="ok", data={"token": "token-1", "warnings": []}),
            Response(status="ok", data={"token": "token-2", "warnings": []}),
        ]
    )
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    manager.unlock_state.refresh_owner = {
        "uid": peer.uid,
        "pid": peer.pid,
        "writer_id": id(writer),
        "lease_id": "lease-test",
    }

    first = await manager._handle_session_request(
        {"command": "begin_capture", "hardware_id": hardware_id},
        "client",
        peer,
        writer,
    )
    second = await manager._handle_session_request(
        {"command": "begin_capture", "hardware_id": hardware_id},
        "client",
        peer,
        writer,
    )

    assert first["status"] == "ok"
    assert first["token"] == "token-1"
    assert second == {
        "status": "error",
        "error_code": "capture_already_active",
        "message": f"capture already active for {hardware_id}",
    }
    assert manager.capture_state.tokens[hardware_id] == "token-1"
    assert manager.client.send_command.await_count == 1


@pytest.mark.asyncio
async def test_clear_captures_for_writer_ends_owned_capture_on_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    hardware_id = "2dc8:3106"
    manager.client.send_command = AsyncMock(
        side_effect=[
            Response(status="ok", data={"token": "token-1", "warnings": []}),
            Response(status="ok"),
        ]
    )
    reevaluate_profiles = AsyncMock()
    monkeypatch.setattr(session_profiles_module, "reevaluate_profiles", reevaluate_profiles)
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    manager.unlock_state.refresh_owner = {
        "uid": peer.uid,
        "pid": peer.pid,
        "writer_id": id(writer),
        "lease_id": "lease-test",
    }

    result = await manager._handle_session_request(
        {
            "command": "begin_capture",
            "hardware_id": hardware_id,
            "end_on_disconnect": True,
        },
        "client",
        peer,
        writer,
    )

    assert result["status"] == "ok"
    assert manager.capture_state.tokens[hardware_id] == "token-1"
    assert manager.capture_state.owner_writer_ids[hardware_id] == id(writer)
    assert hardware_id in manager.capture_state.locks

    await session_recording_module.clear_captures_for_writer(
        manager,
        writer,  # type: ignore[arg-type]
    )

    assert manager.capture_state.tokens == {}
    assert manager.capture_state.locks == set()
    assert manager.capture_state.resume_profiles == {}
    assert manager.capture_state.owner_writer_ids == {}
    end_command = manager.client.send_command.await_args_list[1].args[0]
    assert end_command.command == CommandType.CAPTURE_END
    assert end_command.data == {"token": "token-1"}
    reevaluate_profiles.assert_awaited_once_with(
        manager,
        reason=f"capture ended for {hardware_id}",
    )


@pytest.mark.asyncio
async def test_begin_capture_for_numbered_hardware_uses_configured_paths() -> None:
    manager = SessionManager()
    hardware_id = "1234:5678@2"
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        evdev_devices=[SimpleNamespace(path="/dev/input/by-path/test-event-kbd")]
    )
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"token": "capture-token", "warnings": []})
    )
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    manager.unlock_state.refresh_owner = {
        "uid": peer.uid,
        "pid": peer.pid,
        "writer_id": id(writer),
        "lease_id": "lease-test",
    }

    result = await manager._handle_session_request(
        {"command": "begin_capture", "hardware_id": hardware_id},
        "client",
        peer,
        writer,
    )

    assert result["status"] == "ok"
    sent = manager.client.send_command.await_args.args[0]
    assert sent.command == CommandType.CAPTURE_BEGIN
    assert sent.data == {
        "hardware_id": hardware_id,
        "evdev_paths": ["/dev/input/by-path/test-event-kbd"],
    }


@pytest.mark.asyncio
async def test_begin_capture_default_lifetime_survives_request_writer_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    hardware_id = "2dc8:3106"
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"token": "token-1", "warnings": []})
    )
    reevaluate_profiles = AsyncMock()
    monkeypatch.setattr(session_profiles_module, "reevaluate_profiles", reevaluate_profiles)
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = cast(asyncio.StreamWriter, object())
    manager.unlock_state.refresh_owner = {
        "uid": peer.uid,
        "pid": peer.pid,
        "writer_id": id(writer),
        "lease_id": "lease-test",
    }

    result = await manager._handle_session_request(
        {"command": "begin_capture", "hardware_id": hardware_id},
        "client",
        peer,
        writer,
    )

    assert result["status"] == "ok"
    assert manager.capture_state.tokens[hardware_id] == "token-1"
    assert manager.capture_state.owner_writer_ids == {}
    assert hardware_id in manager.capture_state.locks

    await session_recording_module.clear_captures_for_writer(
        manager,
        writer,
    )

    assert manager.capture_state.tokens[hardware_id] == "token-1"
    assert hardware_id in manager.capture_state.locks
    manager.client.send_command.assert_awaited_once()
    reevaluate_profiles.assert_not_awaited()

@pytest.mark.asyncio
async def test_begin_capture_with_paths_uses_configured_interfaces_when_omitted() -> None:
    manager = SessionManager()
    hardware_id = "2dc8:3106"
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        evdev_devices=[
            SimpleNamespace(
                id="gamepad",
                path="keymasq:2dc8:3106",
                device_type=SimpleNamespace(value="gamepad"),
                phys="bluetooth/input0",
                capabilities=["btn_south"],
            )
        ]
    )
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"token": "capture-token", "warnings": []})
    )
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    manager.unlock_state.refresh_owner = {
        "uid": peer.uid,
        "pid": peer.pid,
        "writer_id": id(writer),
        "lease_id": "lease-test",
    }

    result = await manager._handle_session_request(
        {
            "command": "begin_capture",
            "hardware_id": hardware_id,
            "evdev_paths": ["keymasq:2dc8:3106"],
            "mode": "analog",
        },
        "client",
        peer,
        writer,
    )

    assert result["status"] == "ok"
    sent = manager.client.send_command.await_args.args[0]
    assert sent.command == CommandType.CAPTURE_BEGIN
    assert sent.data == {
        "hardware_id": hardware_id,
        "mode": "analog",
        "evdev_paths": ["keymasq:2dc8:3106"],
        "evdev_interfaces": [
            {
                "id": "gamepad",
                "path": "keymasq:2dc8:3106",
                "type": "gamepad",
                "phys": "bluetooth/input0",
                "capabilities": ["btn_south"],
            }
        ],
    }


@pytest.mark.asyncio
async def test_begin_capture_with_explicit_path_does_not_use_saved_interface() -> None:
    manager = SessionManager()
    hardware_id = "2dc8:3106"
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        evdev_devices=[
            SimpleNamespace(
                id="gamepad",
                path="keymasq:2dc8:3106",
                device_type=SimpleNamespace(value="gamepad"),
                phys="bluetooth/input0",
                capabilities=["btn_south"],
            )
        ]
    )
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"token": "capture-token", "warnings": []})
    )
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    manager.unlock_state.refresh_owner = {
        "uid": peer.uid,
        "pid": peer.pid,
        "writer_id": id(writer),
        "lease_id": "lease-test",
    }

    result = await manager._handle_session_request(
        {
            "command": "begin_capture",
            "hardware_id": hardware_id,
            "evdev_paths": ["/dev/input/event17"],
        },
        "client",
        peer,
        writer,
    )

    assert result["status"] == "ok"
    sent = manager.client.send_command.await_args.args[0]
    assert sent.command == CommandType.CAPTURE_BEGIN
    assert sent.data == {
        "hardware_id": hardware_id,
        "evdev_paths": ["/dev/input/event17"],
    }


@pytest.mark.asyncio
async def test_begin_capture_with_duplicate_logical_paths_preserves_interfaces() -> None:
    manager = SessionManager()
    hardware_id = "2dc8:3106"
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        evdev_devices=[
            SimpleNamespace(
                id="gamepad",
                path="keymasq:2dc8:3106",
                device_type=SimpleNamespace(value="gamepad"),
                phys="bluetooth/input0",
                capabilities=["btn_south"],
            ),
            SimpleNamespace(
                id="kbd",
                path="keymasq:2dc8:3106",
                device_type=SimpleNamespace(value="keyboard"),
                phys="bluetooth/input1",
                capabilities=["key_a"],
            ),
        ]
    )
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"token": "capture-token", "warnings": []})
    )
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    manager.unlock_state.refresh_owner = {
        "uid": peer.uid,
        "pid": peer.pid,
        "writer_id": id(writer),
        "lease_id": "lease-test",
    }

    result = await manager._handle_session_request(
        {
            "command": "begin_capture",
            "hardware_id": hardware_id,
            "evdev_paths": ["keymasq:2dc8:3106", "keymasq:2dc8:3106"],
        },
        "client",
        peer,
        writer,
    )

    assert result["status"] == "ok"
    sent = manager.client.send_command.await_args.args[0]
    assert sent.command == CommandType.CAPTURE_BEGIN
    assert sent.data == {
        "hardware_id": hardware_id,
        "evdev_paths": ["keymasq:2dc8:3106", "keymasq:2dc8:3106"],
        "evdev_interfaces": [
            {
                "id": "gamepad",
                "path": "keymasq:2dc8:3106",
                "type": "gamepad",
                "phys": "bluetooth/input0",
                "capabilities": ["btn_south"],
            },
            {
                "id": "kbd",
                "path": "keymasq:2dc8:3106",
                "type": "keyboard",
                "phys": "bluetooth/input1",
                "capabilities": ["key_a"],
            },
        ],
    }


@pytest.mark.asyncio
async def test_begin_capture_for_numbered_hardware_requires_configured_paths() -> None:
    manager = SessionManager()
    hardware_id = "1234:5678@2"
    manager.hardware.get_hardware = lambda _hardware_id: None  # type: ignore[assignment]
    manager.client.send_command = AsyncMock()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    manager.unlock_state.refresh_owner = {
        "uid": peer.uid,
        "pid": peer.pid,
        "writer_id": id(writer),
        "lease_id": "lease-test",
    }

    result = await manager._handle_session_request(
        {"command": "begin_capture", "hardware_id": hardware_id},
        "client",
        peer,
        writer,
    )

    assert result == {
        "status": "error",
        "message": "Hardware config for 1234:5678@2 has no evdev paths",
    }
    manager.client.send_command.assert_not_called()
    assert hardware_id not in manager.capture_state.locks


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
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        evdev_devices=[
            SimpleNamespace(
                id="kbd",
                path="/dev/input/by-id/test-kbd",
                device_type=SimpleNamespace(value="keyboard"),
                phys="usb/input0",
                capabilities=["key_a"],
            )
        ]
    )
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
    sent = manager.client.send_command.await_args.args[0]
    assert sent.command == CommandType.CAPTURE_COMBO
    assert sent.data["hardware_paths"] == {"1234:5678": ["/dev/input/by-id/test-kbd"]}
    assert sent.data["hardware_interfaces"] == {
        "1234:5678": [
            {
                "id": "kbd",
                "path": "/dev/input/by-id/test-kbd",
                "type": "keyboard",
                "phys": "usb/input0",
                "capabilities": ["key_a"],
            }
        ]
    }
