import asyncio
import json
import logging
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock, call

import pytest

import keymasq.session.manager.commands as session_commands_module
import keymasq.session.manager.compositor as session_compositor_module
import keymasq.session.manager.device_inspector as session_device_inspector_module
import keymasq.session.manager.events as session_events_module
import keymasq.session.manager.profiles as session_profiles_module
import keymasq.session.manager.recording as session_recording_module
import keymasq.session.settings as session_settings
from keymasq.common import paths
from keymasq.common.ipc import CommandType, Response
from keymasq.common.security import PeerCredentials
from keymasq.common.settings import GlobalSettings
from keymasq.session.listeners.kde import KDEListener
from keymasq.session.manager import SessionManager
from tests.session.support import grant_recording_refresh_owner


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
    resolve_macro_recording_status_async = AsyncMock(
        return_value={"unlocked": True, "source": "persistent", "expires_at": 0}
    )
    monkeypatch.setattr(
        session_recording_module,
        "resolve_unlock_status_async",
        resolve_unlock_status_async,
    )
    monkeypatch.setattr(
        session_recording_module,
        "resolve_macro_recording_status_async",
        resolve_macro_recording_status_async,
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
    assert result["macro_recording_enabled"] is True
    assert result["macro_recording_source"] == "persistent"
    resolve_unlock_status_async.assert_awaited_once_with(manager, peer.uid)
    resolve_macro_recording_status_async.assert_awaited_once_with(manager, peer.uid)


@pytest.mark.asyncio
async def test_macro_recording_status_prefers_daemon_when_connected() -> None:
    manager = SessionManager()
    manager.connected = True
    manager.client.send_command = AsyncMock(
        return_value=Response(
            status="ok",
            data={"unlocked": True, "source": "persistent", "expires_at": 0},
        )
    )

    result = await session_recording_module.resolve_macro_recording_status_async(
        manager,
        1000,
    )

    assert result == {"unlocked": True, "source": "persistent", "expires_at": 0}
    sent_command = manager.client.send_command.await_args.args[0]
    assert sent_command.command == CommandType.MACRO_RECORDING_STATUS
    assert sent_command.data == {"uid": 1000}


@pytest.mark.asyncio
async def test_macro_recording_status_uses_cached_daemon_status_for_unreadable_local_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.connected = True
    daemon_status = {"unlocked": True, "source": "persistent", "expires_at": 0}
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data=daemon_status),
    )

    result = await session_recording_module.resolve_macro_recording_status_async(
        manager,
        1000,
    )

    assert result == daemon_status

    monkeypatch.setattr(
        session_recording_module,
        "resolve_macro_recording_status",
        lambda uid: {
            "unlocked": False,
            "source": "none",
            "expires_at": 0,
            "unreadable": True,
        },
    )
    manager.connected = False

    result = await session_recording_module.resolve_macro_recording_status_async(
        manager,
        1000,
    )

    assert result == daemon_status
    manager.client.send_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_recording_unlock_status_prefers_daemon_when_connected() -> None:
    manager = SessionManager()
    manager.connected = True
    manager.client.send_command = AsyncMock(
        return_value=Response(
            status="ok",
            data={"unlocked": True, "source": "runtime", "expires_at": 123},
        )
    )

    result = await session_recording_module.resolve_unlock_status_async(
        manager,
        1000,
    )

    assert result == {"unlocked": True, "source": "runtime", "expires_at": 123}
    sent_command = manager.client.send_command.await_args.args[0]
    assert sent_command.command == CommandType.RECORDING_UNLOCK_STATUS
    assert sent_command.data == {"uid": 1000}


@pytest.mark.asyncio
async def test_recording_unlock_status_logs_unexpected_daemon_query_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SessionManager()
    manager.connected = True
    manager.client.send_command = AsyncMock(side_effect=RuntimeError("status bug"))
    fallback_status = {"unlocked": False, "source": "none", "expires_at": 0}
    monkeypatch.setattr(
        session_recording_module,
        "resolve_unlock_status",
        lambda _uid: fallback_status,
    )

    with caplog.at_level(logging.ERROR, logger="keymasq-session"):
        result = await session_recording_module.resolve_unlock_status_async(
            manager,
            1000,
        )

    assert result == fallback_status
    assert "Unexpected failure querying daemon recording unlock status" in caplog.text
    assert "status bug" in caplog.text


@pytest.mark.asyncio
async def test_recording_unlock_status_uses_cached_daemon_status_for_unreadable_local_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.connected = True
    daemon_status = {
        "unlocked": True,
        "source": "runtime",
        "expires_at": 4_102_444_800,
    }
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data=daemon_status),
    )

    result = await session_recording_module.resolve_unlock_status_async(
        manager,
        1000,
    )

    assert result == daemon_status

    monkeypatch.setattr(
        session_recording_module,
        "resolve_unlock_status",
        lambda uid: {
            "unlocked": False,
            "source": "none",
            "expires_at": 0,
            "unreadable": True,
        },
    )
    manager.connected = False

    result = await session_recording_module.resolve_unlock_status_async(
        manager,
        1000,
    )

    assert result == daemon_status
    manager.client.send_command.assert_awaited_once()


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
async def test_get_recording_settings_uses_unlock_and_owner_state_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.security_policy.recording_unlock_required = True
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    resolve_unlock_status_async = AsyncMock(
        return_value={"unlocked": True, "source": "runtime", "expires_at": 4321}
    )
    resolve_macro_recording_status_async = AsyncMock(
        return_value={"unlocked": False, "source": "none", "expires_at": 0}
    )
    manager.unlock_state.refresh_owner = {
        "uid": peer.uid,
        "pid": peer.pid,
        "writer_id": id(writer),
        "lease_id": "lease-test",
    }
    monkeypatch.setattr(
        session_recording_module,
        "resolve_unlock_status_async",
        resolve_unlock_status_async,
    )
    monkeypatch.setattr(
        session_recording_module,
        "resolve_macro_recording_status_async",
        resolve_macro_recording_status_async,
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
    assert result["macro_recording_enabled"] is False
    assert "authorized" not in result
    resolve_unlock_status_async.assert_awaited_once_with(manager, peer.uid)
    resolve_macro_recording_status_async.assert_awaited_once_with(manager, peer.uid)


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
async def test_sensitive_recording_commands_do_not_require_owner_when_unlock_not_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.security_policy.recording_unlock_required = False
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    start_recording = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(session_recording_module, "start_recording", start_recording)

    result = await manager._handle_session_request(
        {"command": "start_recording", "recording_slot": 1},
        "client",
        peer,
        writer,  # type: ignore[arg-type]
    )

    assert result == {"status": "ok"}
    start_recording.assert_awaited_once_with(
        manager,
        reset_if_active=False,
        recording_slot=1,
        owner_peer=peer,
        owner_writer=writer,
    )


@pytest.mark.asyncio
async def test_start_recording_command_requires_explicit_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.security_policy.recording_unlock_required = False
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    start_recording = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(session_recording_module, "start_recording", start_recording)

    result = await manager._handle_session_request(
        {"command": "start_recording"},
        "client",
        peer,
        writer,  # type: ignore[arg-type]
    )

    assert result["status"] == "error"
    assert result["error_code"] == "macro_recording_slot_required"
    start_recording.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_macro_trigger_requires_explicit_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.send_notification = Mock()  # type: ignore[method-assign]
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    start_recording = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(session_recording_module, "start_recording", start_recording)

    await session_events_module.handle_start_macro_trigger(manager, {})

    manager.send_notification.assert_called_once_with(  # type: ignore[attr-defined]
        "Keymasq: Recording Slot Required",
        "Macro recording triggers must choose a slot from 1 to 4.",
    )
    manager.broadcast_to_session_clients.assert_not_called()  # type: ignore[attr-defined]
    start_recording.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_macro_trigger_warns_when_macro_recording_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.send_notification = Mock()  # type: ignore[method-assign]
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    start_recording = AsyncMock(return_value={"status": "ok"})
    resolve_macro_recording_status_async = AsyncMock(
        return_value={"unlocked": False, "source": "none", "expires_at": 0}
    )
    monkeypatch.setattr(session_recording_module, "start_recording", start_recording)
    monkeypatch.setattr(
        session_recording_module,
        "resolve_macro_recording_status_async",
        resolve_macro_recording_status_async,
    )

    await session_events_module.handle_start_macro_trigger(manager, {"recording_slot": 2})

    manager.send_notification.assert_called_once_with(  # type: ignore[attr-defined]
        "Keymasq: Macro Recording Disabled",
        (
            "Macro recording is disabled. Enable macro recording in Keymasq before using "
            "recording triggers."
        ),
    )
    manager.broadcast_to_session_clients.assert_called_once_with(  # type: ignore[attr-defined]
        {
            "event": "macro_recording_disabled",
            "macro_recording_enabled": False,
            "macro_recording_source": "none",
            "macro_recording_expires_at": 0,
        }
    )
    start_recording.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_macro_trigger_starts_selected_enabled_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.send_notification = Mock()  # type: ignore[method-assign]
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    start_recording = AsyncMock(return_value={"status": "ok"})
    resolve_macro_recording_status_async = AsyncMock(
        return_value={"unlocked": True, "source": "persistent", "expires_at": 0}
    )
    monkeypatch.setattr(session_recording_module, "start_recording", start_recording)
    monkeypatch.setattr(
        session_recording_module,
        "resolve_macro_recording_status_async",
        resolve_macro_recording_status_async,
    )

    await session_events_module.handle_start_macro_trigger(manager, {"recording_slot": 3})

    start_recording.assert_awaited_once_with(
        manager,
        reset_if_active=False,
        recording_slot=3,
    )
    manager.send_notification.assert_not_called()  # type: ignore[attr-defined]
    manager.broadcast_to_session_clients.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_start_macro_trigger_does_not_request_auth_for_non_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.send_notification = Mock()  # type: ignore[method-assign]
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    start_recording = AsyncMock(
        return_value={
            "status": "error",
            "error_code": "recording_already_active",
            "message": "Recording already in progress",
        }
    )
    resolve_macro_recording_status_async = AsyncMock(
        return_value={"unlocked": True, "source": "persistent", "expires_at": 0}
    )
    monkeypatch.setattr(session_recording_module, "start_recording", start_recording)
    monkeypatch.setattr(
        session_recording_module,
        "resolve_macro_recording_status_async",
        resolve_macro_recording_status_async,
    )

    await session_events_module.handle_start_macro_trigger(manager, {"recording_slot": 3})

    manager.send_notification.assert_not_called()  # type: ignore[attr-defined]
    manager.broadcast_to_session_clients.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_start_macro_trigger_requests_auth_for_locked_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.send_notification = Mock()  # type: ignore[method-assign]
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    start_recording = AsyncMock(
        return_value={
            "status": "error",
            "error_code": "recording_locked",
            "message": "recording_locked",
        }
    )
    resolve_macro_recording_status_async = AsyncMock(
        return_value={"unlocked": True, "source": "persistent", "expires_at": 0}
    )
    monkeypatch.setattr(session_recording_module, "start_recording", start_recording)
    monkeypatch.setattr(
        session_recording_module,
        "resolve_macro_recording_status_async",
        resolve_macro_recording_status_async,
    )

    await session_events_module.handle_start_macro_trigger(manager, {"recording_slot": 3})

    manager.send_notification.assert_called_once_with(  # type: ignore[attr-defined]
        "Keymasq: Capture Unlock Required",
        "Capture unlock is required in Keymasq GUI.",
    )
    manager.broadcast_to_session_clients.assert_called_once_with(  # type: ignore[attr-defined]
        {"event": "recording_auth_requested"}
    )


@pytest.mark.asyncio
async def test_recording_started_event_notifies_user_with_slot() -> None:
    manager = SessionManager()
    manager.send_notification = Mock()  # type: ignore[method-assign]
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]

    await session_events_module.handle_event(
        manager,
        CommandType.RECORDING_STARTED,
        {"status": "ok", "recording_slot": 2},
    )

    manager.send_notification.assert_called_once_with(  # type: ignore[attr-defined]
        "Keymasq: Macro Recording Started",
        "Slot 2 is recording.",
    )
    manager.broadcast_to_session_clients.assert_called_once_with(  # type: ignore[attr-defined]
        {"event": "recording_started", "status": "ok", "recording_slot": 2}
    )


@pytest.mark.asyncio
async def test_recording_stopped_event_notifies_user_with_slot_summary() -> None:
    manager = SessionManager()
    manager.send_notification = Mock()  # type: ignore[method-assign]
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]

    await session_events_module.handle_event(
        manager,
        CommandType.RECORDING_STOPPED,
        {
            "pending_recording_id": "recording-2",
            "recording_slot": 2,
            "duration_ms": 1300,
            "event_count": 3,
            "device_types": ["keyboard"],
        },
    )

    manager.send_notification.assert_called_once_with(  # type: ignore[attr-defined]
        "Keymasq: Macro Recording Stopped",
        "Slot 2 captured 3 events over 1.3s.",
    )
    broadcast = manager.broadcast_to_session_clients.call_args.args[0]  # type: ignore[attr-defined]
    assert broadcast["event"] == "recording_stopped"
    assert broadcast["recording_slot"] == 2
    assert broadcast["duration_ms"] == 1300
    assert broadcast["event_count"] == 3
    assert manager.recording_state.pending_slots[2]["pending_recording_id"] == "recording-2"


@pytest.mark.asyncio
async def test_action_trigger_play_macro_slot_dispatches_slot_playback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    play_macro_slot_trigger = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(
        session_recording_module,
        "play_macro_slot_trigger",
        play_macro_slot_trigger,
    )

    data = {
        "action_type": "play_macro_slot",
        "recording_slot": 2,
        "source_device": "kbd",
        "source_button": "key_f13",
        "trigger_value": 1,
    }
    await session_events_module.handle_event(manager, CommandType.ACTION_TRIGGER, data)
    tasks = list(manager.event_state.tasks)
    assert tasks
    await asyncio.gather(*tasks)

    play_macro_slot_trigger.assert_awaited_once_with(manager, data)


@pytest.mark.asyncio
async def test_play_macro_slot_trigger_sends_pending_recording_to_daemon() -> None:
    manager = SessionManager()
    manager.recording_state.pending_slots[4] = {
        "pending_recording_id": "recording-4",
        "recording_slot": 4,
        "move_to_start": True,
        "start_x": 120,
        "start_y": 240,
        "block_mouse_movement": True,
    }
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"status": "ok", "played": True})
    )

    result = await session_recording_module.play_macro_slot_trigger(
        manager,
        {
            "recording_slot": 4,
            "source_device": "kbd",
            "source_button": "key_f13",
            "trigger_value": 0,
        },
    )

    assert result == {"status": "ok", "played": True}
    sent_command = manager.client.send_command.await_args.args[0]
    assert sent_command.command == CommandType.MACRO_PLAY_RECORDING
    assert sent_command.data == {
        "pending_recording_id": "recording-4",
        "macro_name": "recording-slot-4",
        "replay_mouse_movement": True,
        "replay_mouse_clicks": True,
        "speed": 1.0,
        "loop_mode": "none",
        "loop_count": 1,
        "loop_stop_behavior": "finish_run",
        "move_to_start": True,
        "start_x": 120,
        "start_y": 240,
        "block_mouse_movement": True,
        "source_device": "kbd",
        "source_button": "key_f13",
        "trigger_value": 0,
    }


@pytest.mark.asyncio
async def test_play_macro_slot_trigger_refreshes_empty_cache_before_playback() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(
        side_effect=[
            Response(
                status="ok",
                data={
                    "recordings": [
                        {
                            "pending_recording_id": "recording-2",
                            "recording_slot": 2,
                            "duration_ms": 100,
                            "duration_us": 100_000,
                            "device_types": ["keyboard"],
                            "event_count": 1,
                        }
                    ]
                },
            ),
            Response(status="ok", data={"status": "ok", "played": True}),
        ]
    )

    result = await session_recording_module.play_macro_slot_trigger(
        manager,
        {"recording_slot": 2},
    )

    assert result == {"status": "ok", "played": True}
    sent_commands = [
        call.args[0].command for call in manager.client.send_command.await_args_list
    ]
    assert sent_commands == [
        CommandType.MACRO_LIST_RECORDINGS,
        CommandType.MACRO_PLAY_RECORDING,
    ]
    play_command = manager.client.send_command.await_args_list[1].args[0]
    assert play_command.data["pending_recording_id"] == "recording-2"


@pytest.mark.asyncio
async def test_list_macros_include_slots_syncs_slots_from_daemon() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    manager.client.send_command = AsyncMock(
        side_effect=[
            Response(status="ok", data={"macros": [{"name": "stored"}]}),
            Response(
                status="ok",
                data={
                    "recordings": [
                        {
                            "pending_recording_id": "recording-3",
                            "recording_slot": 3,
                            "duration_ms": 250,
                            "duration_us": 250_000,
                            "device_types": ["keyboard"],
                            "event_count": 2,
                        }
                    ]
                },
            ),
        ]
    )

    result = await manager._handle_session_request(
        {"command": "list_macros", "include_slots": True},
        "client",
        peer,
        object(),
    )

    sent_commands = [
        call.args[0].command for call in manager.client.send_command.await_args_list
    ]
    macros = cast(list[dict[str, object]], result["macros"])
    assert sent_commands == [CommandType.MACRO_LIST_META, CommandType.MACRO_LIST_RECORDINGS]
    assert manager.recording_state.pending_slots[3]["pending_recording_id"] == "recording-3"
    assert macros[0]["name"] == "stored"
    assert macros[1]["kind"] == "recording_slot"
    assert macros[1]["recording_slot"] == 3
    assert macros[1]["playable"] is True


@pytest.mark.asyncio
async def test_play_macro_slot_trigger_rejects_empty_slot() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"recordings": []})
    )

    result = await session_recording_module.play_macro_slot_trigger(
        manager,
        {"recording_slot": 4},
    )

    assert result["status"] == "error"
    assert result["error_code"] == "macro_recording_slot_empty"
    sent_command = manager.client.send_command.await_args.args[0]
    assert sent_command.command == CommandType.MACRO_LIST_RECORDINGS


@pytest.mark.asyncio
async def test_play_macro_slot_trigger_notifies_when_slot_is_recording() -> None:
    manager = SessionManager()
    manager.recording_state.active = True
    manager.recording_state.active_slot = 4
    manager.client.send_command = AsyncMock()
    manager.send_notification = Mock()  # type: ignore[method-assign]

    result = await session_recording_module.play_macro_slot_trigger(
        manager,
        {"recording_slot": 4},
    )

    assert result == {
        "status": "error",
        "error_code": "macro_recording_slot_active",
        "message": "Slot 4 is currently recording. Stop recording before playing it.",
        "recording_slot": 4,
    }
    manager.client.send_command.assert_not_awaited()
    manager.send_notification.assert_called_once_with(  # type: ignore[attr-defined]
        "Keymasq: Macro Recording Active",
        "Slot 4 is currently recording. Stop recording before playing it.",
    )


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
async def test_device_inspector_reports_daemon_connection_errors() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(side_effect=ConnectionError("daemon down"))

    result = await session_device_inspector_module.disable_device_inspector_suppression(
        manager,
        "1234:5678",
    )

    assert result == {"status": "error", "message": "Daemon unavailable"}


@pytest.mark.asyncio
async def test_device_inspector_does_not_mask_runtime_errors() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(side_effect=RuntimeError("request bug"))

    with pytest.raises(RuntimeError, match="request bug"):
        await session_device_inspector_module.disable_device_inspector_suppression(
            manager,
            "1234:5678",
        )


@pytest.mark.asyncio
async def test_clear_device_inspectors_for_writer_continues_after_stop_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
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

    with caplog.at_level("ERROR", logger="keymasq-session.device_inspector"):
        await session_device_inspector_module.clear_device_inspectors_for_writer(
            manager,
            writer,  # type: ignore[arg-type]
        )

    assert stopped == ["bad", "good"]
    assert manager.device_inspector_state.owners_by_hardware_id["bad"] == set()
    assert (
        "Failed to stop device inspector for disconnected owner hardware_id=bad" in caplog.text
    )
    assert "stop failed" in caplog.text


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
    resolve_macro_recording_status_async = AsyncMock(
        return_value={"unlocked": True, "source": "persistent", "expires_at": 0}
    )
    monkeypatch.setattr(
        session_recording_module,
        "resolve_unlock_status_async",
        resolve_unlock_status_async,
    )
    monkeypatch.setattr(
        session_recording_module,
        "resolve_macro_recording_status_async",
        resolve_macro_recording_status_async,
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
    assert result["macro_recording_enabled"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["begin_capture", "capture_read", "end_capture"])
async def test_capture_commands_with_owner_return_error_on_missing_hardware_id(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    grant_recording_refresh_owner(manager, peer, writer, monkeypatch)

    result = await manager._handle_session_request(
        {"command": command},
        "client",
        peer,
        writer,
    )

    assert result == {"error": "missing hardware_id"}


@pytest.mark.asyncio
async def test_end_capture_allows_owner_after_unlock_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.connected = True
    manager.security_policy.recording_unlock_required = True
    manager.capture_state.tokens["1234:5678"] = "capture-token"
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"ended": True})
    )
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    manager.unlock_state.refresh_owner = {
        "uid": peer.uid,
        "pid": peer.pid,
        "writer_id": id(writer),
        "lease_id": "lease-test",
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
        {"command": "end_capture", "hardware_id": "1234:5678"},
        "client",
        peer,
        writer,
    )

    assert result["status"] == "ok"
    resolve_unlock_status_async.assert_not_awaited()
    sent_command = manager.client.send_command.await_args.args[0]
    assert sent_command.command == CommandType.CAPTURE_END
    assert sent_command.data == {"token": "capture-token"}


@pytest.mark.asyncio
async def test_begin_capture_rejects_duplicate_for_same_hardware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    grant_recording_refresh_owner(manager, peer, writer, monkeypatch)

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
    grant_recording_refresh_owner(manager, peer, writer, monkeypatch)

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
async def test_begin_capture_for_numbered_hardware_uses_configured_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    grant_recording_refresh_owner(manager, peer, writer, monkeypatch)

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
    grant_recording_refresh_owner(manager, peer, writer, monkeypatch)

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
async def test_capture_end_keeps_token_when_daemon_end_fails() -> None:
    manager = SessionManager()
    hardware_id = "2dc8:3106"
    manager.capture_state.tokens[hardware_id] = "token-1"
    manager.capture_state.locks.add(hardware_id)
    manager.client.send_command = AsyncMock(side_effect=OSError("daemon down"))

    result = await session_recording_module.capture_end(manager, hardware_id)

    assert result == {"status": "error", "message": "Daemon unavailable"}
    assert manager.capture_state.tokens[hardware_id] == "token-1"
    assert hardware_id in manager.capture_state.locks


@pytest.mark.asyncio
async def test_capture_end_logs_unexpected_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SessionManager()
    hardware_id = "2dc8:3106"
    manager.capture_state.tokens[hardware_id] = "token-1"
    manager.capture_state.locks.add(hardware_id)
    manager.client.send_command = AsyncMock(side_effect=RuntimeError("capture bug"))

    with caplog.at_level(logging.ERROR, logger="keymasq-session"):
        result = await session_recording_module.capture_end(manager, hardware_id)

    assert result == {"status": "error", "message": "Failed to end capture"}
    assert manager.capture_state.tokens[hardware_id] == "token-1"
    assert hardware_id in manager.capture_state.locks
    assert "Unexpected failure ending capture for hardware_id=2dc8:3106" in caplog.text
    assert "capture bug" in caplog.text


@pytest.mark.asyncio
async def test_clear_captures_for_writer_forces_local_cleanup_on_daemon_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    hardware_id = "2dc8:3106"
    writer = object()
    manager.capture_state.tokens[hardware_id] = "token-1"
    manager.capture_state.locks.add(hardware_id)
    manager.capture_state.owner_writer_ids[hardware_id] = id(writer)
    manager.capture_state.resume_profiles[hardware_id] = ["Default"]
    manager.client.send_command = AsyncMock(side_effect=OSError("daemon down"))
    reevaluate_profiles = AsyncMock()
    monkeypatch.setattr(session_profiles_module, "reevaluate_profiles", reevaluate_profiles)

    await session_recording_module.clear_captures_for_writer(
        manager,
        writer,  # type: ignore[arg-type]
    )

    assert manager.capture_state.tokens == {}
    assert manager.capture_state.locks == set()
    assert manager.capture_state.owner_writer_ids == {}
    assert manager.capture_state.resume_profiles == {}
    reevaluate_profiles.assert_awaited_once_with(
        manager,
        reason=f"capture ended for {hardware_id}",
    )


@pytest.mark.asyncio
async def test_begin_capture_with_paths_uses_configured_interfaces_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    grant_recording_refresh_owner(manager, peer, writer, monkeypatch)

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
async def test_begin_capture_with_explicit_path_does_not_use_saved_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    grant_recording_refresh_owner(manager, peer, writer, monkeypatch)

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
async def test_begin_capture_with_duplicate_logical_paths_preserves_interfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    grant_recording_refresh_owner(manager, peer, writer, monkeypatch)

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
async def test_begin_capture_for_numbered_hardware_requires_configured_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    hardware_id = "1234:5678@2"
    manager.hardware.get_hardware = lambda _hardware_id: None  # type: ignore[assignment]
    manager.client.send_command = AsyncMock()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    grant_recording_refresh_owner(manager, peer, writer, monkeypatch)

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
async def test_handle_session_request_create_macro_broadcasts_saved_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"macro": {"name": "Speedrun"}})
    )
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    refresh_macro_bindings = AsyncMock()
    monkeypatch.setattr(
        session_profiles_module,
        "refresh_macro_bindings",
        refresh_macro_bindings,
    )

    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    result = await manager._handle_session_request(
        {"command": "create_macro", "macro": {"name": "Speedrun"}},
        "client",
        peer,
        object(),
    )

    assert result == {"status": "ok", "macro": {"name": "Speedrun"}}
    refresh_macro_bindings.assert_awaited_once_with(manager)
    manager.broadcast_to_session_clients.assert_called_once_with(  # type: ignore[attr-defined]
        {"event": "macro_saved", "name": "Speedrun"}
    )


@pytest.mark.asyncio
async def test_handle_session_request_list_macros_reports_daemon_connection_errors() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(side_effect=ConnectionError("daemon down"))
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    result = await manager._handle_session_request(
        {"command": "list_macros"},
        "client",
        peer,
        object(),
    )

    assert result == {"status": "error", "message": "Daemon unavailable"}


@pytest.mark.asyncio
async def test_handle_session_request_list_macros_does_not_mask_runtime_errors() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(side_effect=RuntimeError("request bug"))
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    with pytest.raises(RuntimeError, match="request bug"):
        await manager._handle_session_request(
            {"command": "list_macros"},
            "client",
            peer,
            object(),
        )


@pytest.mark.asyncio
async def test_save_recording_keeps_pending_macro_save_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    writer = object()
    grant_recording_refresh_owner(
        manager,
        peer,
        writer,
        monkeypatch,
        lease_id="lease-1",
    )

    result = await manager._handle_session_request(
        {
            "command": "save_recording",
            "name": "Saved",
            "pending_save_token": "pending-1",
            "move_to_start": True,
            "start_x": 100,
            "start_y": 200,
            "block_mouse_movement": True,
        },
        "client",
        peer,
        writer,  # type: ignore[arg-type]
    )

    assert result == {"status": "ok", "name": "Saved"}
    sent_command = manager.client.send_command.await_args.args[0]
    assert sent_command.command == CommandType.MACRO_SAVE_RECORDING
    assert sent_command.data["pending_recording_id"] == "recording-1"
    assert sent_command.data["move_to_start"] is True
    assert sent_command.data["start_x"] == 100
    assert sent_command.data["start_y"] == 200
    assert sent_command.data["block_mouse_movement"] is True
    assert manager.recording_state.pending_save_token == "pending-1"
    assert manager.recording_state.pending_save_owner_writer_id == 123
    assert manager.recording_state.pending_data is manager.recording_state.pending_slots[1]
    assert manager.recording_state.pending_data == {
        "pending_recording_id": "recording-1",
        "duration_ms": 10,
        "device_types": ["keyboard"],
        "event_count": 1,
        "recording_slot": 1,
        "pending_save_token": "pending-1",
        "move_to_start": True,
        "start_x": 100,
        "start_y": 200,
        "block_mouse_movement": True,
    }


@pytest.mark.asyncio
async def test_save_recording_rejects_empty_sanitized_macro_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.recording_state.pending_data = {
        "pending_recording_id": "recording-1",
        "duration_ms": 10,
    }
    manager.recording_state.pending_save_token = "pending-1"
    manager.client.send_command = AsyncMock()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    grant_recording_refresh_owner(manager, peer, writer, monkeypatch)

    result = await manager._handle_session_request(
        {
            "command": "save_recording",
            "name": "!!!",
            "pending_save_token": "pending-1",
        },
        "client",
        peer,
        writer,  # type: ignore[arg-type]
    )

    assert result == {
        "status": "error",
        "error_code": "invalid_macro_name",
        "message": "Macro name is invalid or empty",
    }
    manager.client.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_recording_requires_active_unlock_owner() -> None:
    manager = SessionManager()
    manager.security_policy.recording_unlock_required = True
    manager.recording_state.pending_data = {
        "pending_recording_id": "recording-1",
        "duration_ms": 10,
        "device_types": ["keyboard"],
        "event_count": 1,
    }
    manager.recording_state.pending_save_token = "pending-1"
    manager.client.send_command = AsyncMock()
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

    assert result["status"] == "error"
    assert result["error_code"] == "sensitive_command_denied"
    manager.client.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_recording_slot_rejects_stale_pending_macro_save_token() -> None:
    manager = SessionManager()
    manager.recording_state.pending_data = {"events": [{"t_us": 0}]}
    manager.recording_state.pending_save_token = "current"
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    result = await manager._handle_session_request(
        {"command": "delete_recording_slot", "pending_save_token": "stale"},
        "client",
        peer,
        object(),
    )

    assert result["status"] == "error"
    assert result["error_code"] == "stale_pending_macro_save"
    assert manager.recording_state.pending_data == {"events": [{"t_us": 0}]}
    assert manager.recording_state.pending_save_token == "current"


@pytest.mark.asyncio
async def test_replaced_pending_slot_rejects_previous_pending_save_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.security_policy.recording_unlock_required = False
    session_recording_module.begin_pending_macro_save(
        manager,
        {"pending_recording_id": "recording-a", "duration_ms": 10},
        recording_slot=1,
    )
    old_token = "old-token"
    manager.recording_state.pending_slot_tokens[1] = old_token
    manager.recording_state.pending_slots[1]["pending_save_token"] = old_token
    manager.recording_state.pending_save_token = old_token
    manager.recording_state.pending_slot_owner_writer_ids[1] = 123
    manager.recording_state.pending_slot_owner_pids[1] = 456
    manager.recording_state.pending_slot_owner_uids[1] = 789
    manager.recording_state.pending_slot_created_at[1] = 1.0

    def fake_token_urlsafe(_size: int) -> str:
        return "fresh-token"

    def fake_monotonic() -> float:
        return 20.0

    monkeypatch.setattr(session_recording_module.secrets, "token_urlsafe", fake_token_urlsafe)
    monkeypatch.setattr(session_recording_module, "monotonic", fake_monotonic)

    session_recording_module.replace_pending_macro_slots_from_daemon(
        manager,
        [
            {
                "pending_recording_id": "recording-b",
                "recording_slot": 1,
                "duration_ms": 20,
            }
        ],
    )

    assert manager.recording_state.pending_slot_tokens[1] == "fresh-token"
    assert manager.recording_state.pending_save_token == "fresh-token"
    assert manager.recording_state.pending_slots[1]["pending_save_token"] == "fresh-token"
    assert manager.recording_state.pending_slot_owner_writer_ids[1] is None
    assert manager.recording_state.pending_slot_owner_pids[1] is None
    assert manager.recording_state.pending_slot_owner_uids[1] is None
    assert manager.recording_state.pending_slot_created_at[1] == 20.0
    assert not session_recording_module.pending_macro_save_token_matches(manager, old_token)

    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    manager.client.send_command = AsyncMock()
    for request in (
        {"command": "save_recording", "name": "Saved", "pending_save_token": old_token},
        {"command": "delete_recording_slot", "pending_save_token": old_token},
    ):
        result = await manager._handle_session_request(
            request,
            "client",
            peer,
            object(),
        )

        assert result["status"] == "error"
        assert result["error_code"] == "stale_pending_macro_save"
    manager.client.send_command.assert_not_awaited()


def test_clear_pending_macro_save_keeps_slots_for_invalid_selector() -> None:
    manager = SessionManager()
    session_recording_module.begin_pending_macro_save(
        manager,
        {"pending_recording_id": "recording-1", "duration_ms": 10},
        recording_slot=1,
    )
    manager.recording_state.pending_slot_tokens[1] = "current"
    manager.recording_state.pending_slots[1]["pending_save_token"] = "current"
    session_recording_module.clear_pending_macro_save(
        manager,
        pending_save_token="stale",
    )

    assert manager.recording_state.pending_slots[1]["pending_recording_id"] == "recording-1"
    assert manager.recording_state.pending_slot_tokens[1] == "current"


@pytest.mark.asyncio
async def test_delete_recording_slot_keeps_state_when_daemon_delete_fails() -> None:
    manager = SessionManager()
    manager.recording_state.pending_data = {
        "pending_recording_id": "recording-1",
        "duration_ms": 10,
    }
    manager.recording_state.pending_save_token = "pending-1"
    manager.client.send_command = AsyncMock(return_value=Response(status="error", error="boom"))
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    result = await manager._handle_session_request(
        {"command": "delete_recording_slot", "pending_save_token": "pending-1"},
        "client",
        peer,
        object(),
    )

    assert result == {"status": "error", "message": "No pending recording"}
    assert manager.recording_state.pending_slots[1]["pending_recording_id"] == "recording-1"
    assert manager.recording_state.pending_save_token == "pending-1"


@pytest.mark.asyncio
async def test_delete_recording_slot_clears_pending_macro_save_state() -> None:
    manager = SessionManager()
    manager.recording_state.pending_data = {"events": [{"t_us": 0}]}
    manager.recording_state.pending_save_token = "pending-1"
    manager.recording_state.pending_save_owner_writer_id = 123
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    result = await manager._handle_session_request(
        {"command": "delete_recording_slot", "pending_save_token": "pending-1"},
        "client",
        peer,
        object(),
    )

    assert result == {"status": "ok"}
    assert manager.recording_state.pending_data is None
    assert manager.recording_state.pending_save_token is None
    assert manager.recording_state.pending_save_owner_writer_id is None


@pytest.mark.asyncio
async def test_handle_session_request_update_macro_refreshes_runtime_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"macro": {"name": "Speedrun"}})
    )
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    refresh_macro_bindings = AsyncMock()
    monkeypatch.setattr(
        session_profiles_module,
        "refresh_macro_bindings",
        refresh_macro_bindings,
    )

    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    result = await manager._handle_session_request(
        {"command": "update_macro", "name": "Speedrun", "macro": {"name": "Speedrun"}},
        "client",
        peer,
        object(),
    )

    assert result == {"status": "ok", "macro": {"name": "Speedrun"}}
    refresh_macro_bindings.assert_awaited_once_with(manager)
    manager.broadcast_to_session_clients.assert_called_once_with(  # type: ignore[attr-defined]
        {"event": "macro_saved", "name": "Speedrun"}
    )


@pytest.mark.asyncio
async def test_handle_session_request_delete_macro_broadcasts_deleted_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(return_value=Response(status="ok", data={}))
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    refresh_macro_bindings = AsyncMock()
    monkeypatch.setattr(
        session_profiles_module,
        "refresh_macro_bindings",
        refresh_macro_bindings,
    )

    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    result = await manager._handle_session_request(
        {"command": "delete_macro", "name": "Speedrun"},
        "client",
        peer,
        object(),
    )

    assert result == {"status": "ok"}
    refresh_macro_bindings.assert_awaited_once_with(manager)
    manager.broadcast_to_session_clients.assert_called_once_with(  # type: ignore[attr-defined]
        {"event": "macro_deleted", "name": "Speedrun"}
    )


@pytest.mark.asyncio
async def test_profile_commands_cover_simple_and_error_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    set_profile_enabled = AsyncMock(return_value={"status": "ok", "enabled": True})
    monkeypatch.setattr(
        session_profiles_module,
        "set_profile_enabled",
        set_profile_enabled,
    )
    monkeypatch.setattr(
        session_profiles_module,
        "build_profile_overview",
        lambda _manager: {"status": "ok", "profiles": [{"name": "Base"}]},
    )

    listed = await manager._handle_session_request(
        {"command": "list_profiles"},
        "client",
        peer,
        object(),
    )
    missing = await manager._handle_session_request(
        {"command": "enable_profile"},
        "client",
        peer,
        object(),
    )
    enabled = await manager._handle_session_request(
        {"command": "enable_profile", "profile_name": "Base"},
        "client",
        peer,
        object(),
    )
    toggled = await manager._handle_session_request(
        {"command": "toggle_profile", "profile_name": "Base"},
        "client",
        peer,
        object(),
    )
    ping = await manager._handle_session_request(
        {"command": "ping"},
        "client",
        peer,
        object(),
    )

    assert listed == {"status": "ok", "profiles": [{"name": "Base"}]}
    assert missing == {"status": "error", "message": "missing profile_name"}
    assert enabled == {"status": "ok", "enabled": True}
    assert toggled == {"status": "ok", "enabled": True}
    assert ping == {"status": "ok"}
    assert [call.args for call in set_profile_enabled.await_args_list] == [
        (manager, "Base", True),
        (manager, "Base", None),
    ]


@pytest.mark.asyncio
async def test_profile_commands_report_reload_and_release_failures() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    manager.reload_profiles = AsyncMock(return_value=False)  # type: ignore[method-assign]

    reload_result = await manager._handle_session_request(
        {"command": "reload"},
        "client",
        peer,
        object(),
    )
    missing_release = await manager._handle_session_request(
        {"command": "release_device"},
        "client",
        peer,
        object(),
    )

    manager.client.send_command = AsyncMock(side_effect=OSError("daemon down"))
    unavailable_release = await manager._handle_session_request(
        {"command": "release_device", "hardware_id": "1234:5678"},
        "client",
        peer,
        object(),
    )

    manager.client.send_command = AsyncMock(
        return_value=Response(status="error", error="still pressed")
    )
    rejected_release = await manager._handle_session_request(
        {"command": "release_device", "hardware_id": "1234:5678", "immediate": False},
        "client",
        peer,
        object(),
    )

    manager.reload_config_from_disk = Mock(side_effect=ValueError("bad config"))  # type: ignore[method-assign]
    manager.send_notification = Mock()  # type: ignore[method-assign]
    reevaluate = await manager._handle_session_request(
        {"command": "reevaluate_profiles"},
        "client",
        peer,
        object(),
    )

    assert reload_result["status"] == "error"
    assert missing_release == {"status": "error", "message": "missing hardware_id"}
    assert unavailable_release == {"status": "error", "message": "Daemon unavailable"}
    assert rejected_release == {"status": "error", "message": "still pressed"}
    assert reevaluate == {"status": "error", "message": "bad config"}
    manager.send_notification.assert_called_once()  # type: ignore[attr-defined]
    assert manager.client.send_command.await_args.args[0].data == {
        "hardware_id": "1234:5678",
        "immediate": False,
    }


@pytest.mark.asyncio
async def test_settings_and_virtual_gamepad_commands_cover_success_and_unavailable(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(paths, "CONFIG_DIR", tmp_path / "keymasq")
    session_settings.save_global_settings(GlobalSettings(virtual_gamepad_count=2))
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]

    virtuals = await manager._handle_session_request(
        {"command": "get_virtual_gamepads"},
        "client",
        peer,
        object(),
    )
    settings = await manager._handle_session_request(
        {"command": "get_settings"},
        "client",
        peer,
        object(),
    )

    manager.connected = True
    manager.client.send_command = AsyncMock(side_effect=OSError("daemon down"))
    virtual_unavailable = await manager._handle_session_request(
        {"command": "set_virtual_gamepads", "count": 3},
        "client",
        peer,
        object(),
    )
    settings_unavailable = await manager._handle_session_request(
        {"command": "set_settings", "virtual_gamepad_count": 3},
        "client",
        peer,
        object(),
    )

    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"count": 4})
    )
    settings_saved = await manager._handle_session_request(
        {"command": "set_settings", "virtual_gamepad_count": 3},
        "client",
        peer,
        object(),
    )

    assert virtuals["count"] == 2
    assert settings["virtual_gamepad_count"] == 2
    assert virtual_unavailable == {"status": "error", "message": "Daemon unavailable"}
    assert settings_unavailable["status"] == "error"
    assert settings_unavailable["message"] == "Daemon unavailable"
    assert settings_saved["status"] == "ok"
    assert settings_saved["virtual_gamepad_count"] == 4
    manager.broadcast_to_session_clients.assert_called_once_with(  # type: ignore[attr-defined]
        {"event": "settings_changed", "virtual_gamepad_count": 4}
    )


@pytest.mark.asyncio
async def test_macro_commands_cover_remaining_daemon_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    refresh_macro_bindings = AsyncMock()
    monkeypatch.setattr(
        session_profiles_module,
        "refresh_macro_bindings",
        refresh_macro_bindings,
    )

    manager.client.send_command = AsyncMock(
        side_effect=[
            Response(status="error", error="empty list"),
            Response(status="ok", data={"macro": {"name": "Demo"}}),
            Response(status="error", error="missing"),
            OSError("daemon down"),
            Response(status="error", error="create failed"),
            OSError("daemon down"),
            Response(status="error", error="delete failed"),
            Response(status="ok", data={"macro": {"name": "Renamed"}}),
            Response(status="ok", data=None),
            Response(status="error", error="rename failed"),
            Response(status="ok", data={"status": "ok", "queued": True}),
            Response(status="error", error="play failed"),
            Response(status="ok", data={"cancelled": 2}),
            Response(status="error", error="cancel failed"),
        ]
    )

    requests = [
        {"command": "list_macros"},
        {"command": "get_macro", "name": "Demo"},
        {"command": "get_macro", "name": "Missing"},
        {"command": "get_macro", "name": "Missing"},
        {"command": "create_macro", "macro": {"name": "Demo"}},
        {"command": "update_macro", "name": "Demo", "macro": {"name": "Demo"}},
        {"command": "delete_macro", "name": "Demo", "expected_revision": 3},
        {"command": "rename_macro", "old": "Demo", "new": "Renamed"},
        {"command": "rename_macro", "old": "Demo", "new": "Renamed"},
        {"command": "rename_macro", "old": "Demo", "new": "Renamed"},
        {
            "command": "play_macro",
            "name": "Demo",
            "replay_mouse_movement": False,
            "replay_mouse_clicks": False,
            "speed": 1.5,
        },
        {"command": "play_macro", "name": "Demo"},
        {"command": "cancel_macro_playback"},
        {"command": "cancel_macro_playback"},
    ]
    results = [
        await manager._handle_session_request(request, "client", peer, object())
        for request in requests
    ]

    assert results == [
        {"status": "error", "message": "empty list"},
        {"status": "ok", "macro": {"name": "Demo"}},
        {"status": "error", "message": "missing"},
        {"status": "error", "message": "Daemon unavailable"},
        {"status": "error", "message": "create failed"},
        {"status": "error", "message": "Daemon unavailable"},
        {"status": "error", "message": "delete failed"},
        {"status": "ok", "macro": {"name": "Renamed"}},
        {"status": "ok"},
        {"status": "error", "message": "rename failed"},
        {"status": "ok", "queued": True},
        {"status": "error", "message": "play failed"},
        {"cancelled": 2},
        {"status": "error", "message": "cancel failed"},
    ]
    update_command = manager.client.send_command.await_args_list[5].args[0]
    delete_command = manager.client.send_command.await_args_list[6].args[0]
    play_command = manager.client.send_command.await_args_list[10].args[0]
    assert update_command.command == CommandType.MACRO_UPDATE
    assert delete_command.data == {"name": "Demo", "expected_revision": 3}
    assert play_command.data == {
        "name": "Demo",
        "replay_mouse_movement": False,
        "replay_mouse_clicks": False,
        "speed": 1.5,
    }
    assert refresh_macro_bindings.await_count == 2
    assert manager.broadcast_to_session_clients.call_args_list == [  # type: ignore[attr-defined]
        call({"event": "macro_deleted", "name": "Demo"}),
        call({"event": "macro_saved", "name": "Renamed"}),
    ]


@pytest.mark.asyncio
async def test_macro_commands_validate_payloads_and_compile_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    async def fake_to_thread(_func, /, *_args, **_kwargs):
        raise ValueError("bad macro")

    monkeypatch.setattr(session_commands_module.asyncio, "to_thread", fake_to_thread)

    create_missing = await manager._handle_session_request(
        {"command": "create_macro"},
        "client",
        peer,
        object(),
    )
    update_missing = await manager._handle_session_request(
        {"command": "update_macro", "name": "Demo"},
        "client",
        peer,
        object(),
    )
    type_text_error = await manager._handle_session_request(
        {"command": "type_text", "text": "Hi"},
        "client",
        peer,
        object(),
    )
    compact_empty = await manager._handle_session_request(
        {"command": "play_compact_macro", "tokens": []},
        "client",
        peer,
        object(),
    )
    compact_error = await manager._handle_session_request(
        {"command": "play_compact_macro", "tokens": ["key_a"]},
        "client",
        peer,
        object(),
    )

    assert create_missing == {"status": "error", "message": "macro payload required"}
    assert update_missing == {"status": "error", "message": "macro payload required"}
    assert type_text_error == {"status": "error", "message": "bad macro"}
    assert compact_empty == {"status": "error", "message": "tokens required"}
    assert compact_error == {"status": "error", "message": "bad macro"}


@pytest.mark.asyncio
async def test_adhoc_macro_payload_reports_daemon_failures() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    payload = {
        "command": "play_macro_payload",
        "macro_events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
    }

    manager.client.send_command = AsyncMock(side_effect=OSError("daemon down"))
    unavailable = await manager._handle_session_request(payload, "client", peer, object())

    manager.client.send_command = AsyncMock(
        return_value=Response(status="error", error="play failed")
    )
    failed = await manager._handle_session_request(payload, "client", peer, object())

    assert unavailable == {"status": "error", "message": "Daemon unavailable"}
    assert failed == {"status": "error", "message": "play failed"}


@pytest.mark.asyncio
async def test_capture_and_diagnostics_commands_cover_remaining_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.security_policy.recording_unlock_required = False
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    get_devices = AsyncMock(return_value=[{"name": "Keyboard"}])
    capture_read = AsyncMock(return_value={"status": "ok", "events": []})
    update_selected = Mock()
    monkeypatch.setattr(session_recording_module, "get_devices_for_recording", get_devices)
    monkeypatch.setattr(session_recording_module, "capture_read", capture_read)
    monkeypatch.setattr(
        session_recording_module,
        "update_selected_recording_devices_cache",
        update_selected,
    )

    devices = await manager._handle_session_request(
        {"command": "list_devices_for_recording", "include_other": True},
        "client",
        peer,
        writer,
    )
    read = await manager._handle_session_request(
        {"command": "capture_read", "hardware_id": "1234:5678"},
        "client",
        peer,
        writer,
    )
    combo_missing = await manager._handle_session_request(
        {"command": "capture_combo"},
        "client",
        peer,
        writer,
    )

    manager.client.send_command = AsyncMock(side_effect=OSError("daemon down"))
    diagnostics_unavailable = await manager._handle_session_request(
        {"command": "set_diagnostics", "enabled": True},
        "client",
        peer,
        writer,
    )

    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"enabled": True})
    )
    diagnostics_ok = await manager._handle_session_request(
        {
            "command": "set_diagnostics",
            "enabled": True,
            "interval": 2,
            "categories": ["runtime", "", "devices"],
        },
        "client",
        peer,
        writer,
    )

    manager.client.send_command = AsyncMock(
        return_value=Response(status="error", error="bad category")
    )
    diagnostics_error = await manager._handle_session_request(
        {"command": "set_diagnostics"},
        "client",
        peer,
        writer,
    )

    assert devices == {"status": "ok", "devices": [{"name": "Keyboard"}]}
    assert read == {"status": "ok", "events": []}
    assert combo_missing == {"error": "missing profile_name"}
    assert diagnostics_unavailable == {"status": "error", "message": "Daemon unavailable"}
    assert diagnostics_ok == {"status": "ok", "data": {"enabled": True}}
    assert diagnostics_error == {"status": "error", "message": "bad category"}
    get_devices.assert_awaited_once_with(
        manager,
        ["keyboard", "gamepad", "mouse", "touchpad", "pointstick", "other"],
        include_grabbed=True,
    )
    assert manager.recording_state.devices_cache == [{"name": "Keyboard"}]
    assert manager.recording_state.devices_cache_ready is True
    update_selected.assert_called_once_with(manager)
    sent = manager.client.send_command.await_args.args[0]
    assert sent.command == CommandType.SET_DIAGNOSTICS


@pytest.mark.asyncio
async def test_capture_combo_session_command_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    grant_recording_refresh_owner(manager, peer, writer, monkeypatch)

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
