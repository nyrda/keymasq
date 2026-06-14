import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

import keymasq.session.manager.events as session_events_module
import keymasq.session.manager.profiles as session_profiles_module
from keymasq.common.ipc import Command, CommandType, Response
from keymasq.common.models import ProfileConfig, ProfileDeactivationPolicy
from keymasq.session.listeners.hyprland import HyprlandListener
from keymasq.session.manager import SessionManager
from keymasq.session.manager.state import ExecBinding, RuntimeProfileActivation


def _sent_daemon_commands(
    manager: SessionManager,
    command_type: CommandType,
) -> list[Command]:
    return [
        call_args.args[0]
        for call_args in manager.client.send_command.await_args_list
        if call_args.args[0].command == command_type
    ]


@pytest.mark.asyncio
async def test_handle_cursor_position_request_sends_realtime_response() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(return_value=Response(status="ok", data={}))
    listener = SimpleNamespace(
        supports_realtime_cursor_position=True,
        get_cursor_position=AsyncMock(return_value=(12, 34)),
    )
    manager.compositor_state.window_listener = listener
    manager.compositor_state.compositor_id = "hyprland"

    await session_events_module.handle_event(
        manager,
        CommandType.CURSOR_POSITION_REQUEST,
        {"request_id": "cursor-1"},
    )
    await asyncio.sleep(0)

    sent = manager.client.send_command.await_args.args[0]
    assert sent.command == CommandType.CURSOR_POSITION_RESPONSE
    assert sent.data == {
        "request_id": "cursor-1",
        "status": "ok",
        "x": 12,
        "y": 34,
    }


@pytest.mark.asyncio
async def test_handle_cursor_position_request_passes_tracking_hint_to_listener() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(return_value=Response(status="ok", data={}))
    listener = SimpleNamespace(
        supports_realtime_cursor_position=True,
        prepare_cursor_position_tracking=AsyncMock(),
        get_cursor_position=AsyncMock(return_value=(12, 34)),
    )
    manager.compositor_state.window_listener = listener
    manager.compositor_state.compositor_id = "kde"

    await session_events_module.handle_event(
        manager,
        CommandType.CURSOR_POSITION_REQUEST,
        {"request_id": "cursor-1", "tracking_hint_ms": 250},
    )
    await asyncio.sleep(0)

    listener.prepare_cursor_position_tracking.assert_awaited_once_with(250)
    listener.get_cursor_position.assert_awaited_once_with()
    sent = manager.client.send_command.await_args.args[0]
    assert sent.command == CommandType.CURSOR_POSITION_RESPONSE
    assert sent.data == {
        "request_id": "cursor-1",
        "status": "ok",
        "x": 12,
        "y": 34,
    }


@pytest.mark.asyncio
async def test_handle_event_dispatches_action_trigger_variants(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SessionManager(verbosity=1)
    manager.exec_state.exec_refs[9] = ExecBinding(
        cmd="echo hardware",
        owner="combo",
        hardware_id="1234:5678",
    )
    handlers = {
        "start": AsyncMock(),
        "stop": AsyncMock(),
        "slot": AsyncMock(return_value={"status": "ok"}),
        "cancel": AsyncMock(),
        "reset": AsyncMock(),
        "profile": AsyncMock(),
    }
    monkeypatch.setattr(session_events_module, "handle_start_macro_trigger", handlers["start"])
    monkeypatch.setattr(session_events_module, "handle_stop_macro_trigger", handlers["stop"])
    monkeypatch.setattr(
        session_events_module.runtime_recording,
        "play_macro_slot_trigger",
        handlers["slot"],
    )
    monkeypatch.setattr(session_events_module, "handle_cancel_macro_trigger", handlers["cancel"])
    monkeypatch.setattr(session_events_module, "handle_emergency_reset_trigger", handlers["reset"])
    monkeypatch.setattr(session_events_module, "handle_profile_trigger", handlers["profile"])
    manager.action_handler.execute_command = AsyncMock(return_value=0)

    with caplog.at_level(logging.DEBUG, logger="keymasq-session"):
        for action_type in (
            "start_macro_recording",
            "stop_macro_recording",
            "play_macro_slot",
            "cancel_macro_playback",
            "emergency_reset",
            "profile_enable",
        ):
            await session_events_module.handle_event(
                manager,
                CommandType.ACTION_TRIGGER,
                {"action_type": action_type, "events": [{"type": 1}]},
            )
        await session_events_module.handle_event(
            manager,
            CommandType.ACTION_TRIGGER,
            {"action_type": "exec", "exec_ref": 9},
        )
        await session_events_module.handle_event(
            manager,
            CommandType.ACTION_TRIGGER,
            {"action_type": "exec", "exec_ref": 404},
        )
        await asyncio.sleep(0)

    assert "Event: action_trigger ->" in caplog.text
    assert "<1 events>" in caplog.text
    for handler in handlers.values():
        handler.assert_awaited()
    manager.action_handler.execute_command.assert_awaited_once_with("echo hardware")
    assert "Unknown exec_ref: 404" in caplog.text


@pytest.mark.asyncio
async def test_handle_event_dispatches_device_and_runtime_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    connected = AsyncMock()
    disconnected = AsyncMock()
    grab_status = Mock()
    runtime_reset = AsyncMock()
    deactivate_requested = AsyncMock()
    monkeypatch.setattr(session_events_module, "on_device_connected", connected)
    monkeypatch.setattr(session_events_module, "on_device_disconnected", disconnected)
    monkeypatch.setattr(session_events_module, "handle_device_grab_status_event", grab_status)
    monkeypatch.setattr(session_events_module, "handle_runtime_reset_event", runtime_reset)
    monkeypatch.setattr(
        session_events_module,
        "handle_profile_deactivate_requested",
        deactivate_requested,
    )

    await session_events_module.handle_event(
        manager,
        CommandType.DEVICE_CONNECTED,
        {"vendor_id": "1234", "product_id": "5678"},
    )
    await session_events_module.handle_event(
        manager,
        CommandType.DEVICE_DISCONNECTED,
        {"hardware_id": "1234:5678"},
    )
    await session_events_module.handle_event(
        manager,
        CommandType.DEVICE_GRAB_STATUS,
        {"hardware_id": "1234:5678", "state": "ready"},
    )
    await session_events_module.handle_event(
        manager,
        CommandType.RUNTIME_RESET,
        {"reason": "test"},
    )
    await session_events_module.handle_event(
        manager,
        CommandType.PROFILE_DEACTIVATE_REQUESTED,
        {"profile_name": "Nav"},
    )
    await asyncio.sleep(0)

    connected.assert_awaited_once_with(manager, {"vendor_id": "1234", "product_id": "5678"})
    disconnected.assert_awaited_once_with(manager, {"hardware_id": "1234:5678"})
    grab_status.assert_called_once_with(
        manager,
        {"hardware_id": "1234:5678", "state": "ready"},
    )
    runtime_reset.assert_awaited_once_with(manager, {"reason": "test"})
    deactivate_requested.assert_awaited_once_with(manager, {"profile_name": "Nav"})


@pytest.mark.asyncio
async def test_handle_event_compositor_dispatch_uses_listener() -> None:
    manager = SessionManager()
    manager.action_handler = AsyncMock()

    listener = HyprlandListener(AsyncMock())
    listener.running = True
    listener.dispatch = AsyncMock(return_value=(True, "ok"))  # type: ignore[method-assign]
    manager.compositor_state.window_listener = listener
    manager.compositor_state.compositor_id = "hyprland"

    await session_events_module.handle_event(
        manager,
        CommandType.ACTION_TRIGGER,
        {
            "action_type": "compositor_dispatch",
            "dispatcher": "workspace",
            "args": "e+1",
            "source_device": "1234:5678",
            "source_button": "btn_back",
        },
    )

    await asyncio.sleep(0)
    listener.dispatch.assert_awaited_once_with("workspace", "e+1")


@pytest.mark.asyncio
async def test_handle_event_exec_ref_schedules_command_without_blocking() -> None:
    manager = SessionManager()
    manager.exec_state.exec_refs[7] = ExecBinding(cmd="echo once", owner="combo")
    started = asyncio.Event()
    finish = asyncio.Event()

    async def _execute_command(cmd: str) -> int:
        started.set()
        await finish.wait()
        return 0

    manager.action_handler.execute_command = AsyncMock(side_effect=_execute_command)

    await asyncio.wait_for(
        session_events_module.handle_event(
            manager,
            CommandType.ACTION_TRIGGER,
            {
                "action_type": "exec",
                "exec_ref": 7,
                "source_device": "1234:5678",
                "source_button": "btn_side",
            },
        ),
        timeout=1.0,
    )

    await asyncio.wait_for(started.wait(), timeout=1.0)
    manager.action_handler.execute_command.assert_awaited_once_with("echo once")
    finish.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_event_background_task_logs_exception_and_clears_reference(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SessionManager()
    manager.action_handler.execute_command = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("exec failed")
    )

    with caplog.at_level(logging.ERROR, logger="keymasq-session"):
        await session_events_module.handle_event(
            manager,
            CommandType.ACTION_TRIGGER,
            {"action_type": "exec", "cmd": "echo fail"},
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert manager.event_state.tasks == set()
    assert "Unhandled exception in exec event task" in caplog.text
    assert "RuntimeError: exec failed" in caplog.text


@pytest.mark.asyncio
async def test_cancel_event_tasks_cancels_tracked_background_task() -> None:
    manager = SessionManager()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _wait_forever() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = session_events_module.create_event_task(
        manager,
        _wait_forever(),
        name="test",
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await session_events_module.cancel_event_tasks(manager)

    assert cancelled.is_set()
    assert task.cancelled()
    assert manager.event_state.tasks == set()


@pytest.mark.asyncio
async def test_create_event_task_tracks_extra_task_set() -> None:
    manager = SessionManager()
    extra_tasks = set()

    async def done() -> None:
        return None

    task = session_events_module.create_event_task(
        manager,
        done(),
        name="extra",
        extra_task_set=extra_tasks,
    )

    assert task in manager.event_state.tasks
    assert task in extra_tasks
    await task
    await asyncio.sleep(0)
    assert task not in manager.event_state.tasks
    assert task not in extra_tasks


@pytest.mark.asyncio
async def test_handle_event_high_exec_ref_schedules_command_without_numeric_split() -> None:
    manager = SessionManager()
    manager.exec_state.exec_refs[10000] = ExecBinding(cmd="echo high", owner="combo")
    started = asyncio.Event()
    finish = asyncio.Event()

    async def _execute_command(cmd: str) -> int:
        started.set()
        await finish.wait()
        return 0

    manager.action_handler.execute_command = AsyncMock(side_effect=_execute_command)

    await asyncio.wait_for(
        session_events_module.handle_event(
            manager,
            CommandType.ACTION_TRIGGER,
            {
                "action_type": "exec",
                "exec_ref": 10000,
                "source_device": "1234:5678",
                "source_button": "btn_side",
            },
        ),
        timeout=1.0,
    )

    await asyncio.wait_for(started.wait(), timeout=1.0)
    manager.action_handler.execute_command.assert_awaited_once_with("echo high")
    finish.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_handle_event_macro_async_exec_uses_exec_trigger_path() -> None:
    manager = SessionManager()
    manager.action_handler.handle_action = AsyncMock()
    manager.action_handler.execute_command_sync = Mock()

    await session_events_module.handle_event(
        manager,
        CommandType.ACTION_TRIGGER,
        {
            "action_type": "exec",
            "cmd": "echo macro",
            "macro_exec_async": True,
        },
    )

    await asyncio.sleep(0)
    manager.action_handler.handle_action.assert_not_awaited()
    manager.action_handler.execute_command_sync.assert_called_once_with("echo macro")


@pytest.mark.asyncio
async def test_handle_event_macro_sync_exec_waits_and_reports_completion() -> None:
    manager = SessionManager()
    started = asyncio.Event()
    finish = asyncio.Event()
    sent = asyncio.Event()
    sent_commands = []

    async def _execute_command(cmd: str, *, timeout_s: float = 300.0) -> int:
        assert timeout_s == pytest.approx(1.25)
        started.set()
        await finish.wait()
        return 17

    async def _send_command(command):
        sent_commands.append(command)
        sent.set()
        return SimpleNamespace(status="ok", data={})

    manager.action_handler.execute_command = AsyncMock(side_effect=_execute_command)
    manager.client.send_command = AsyncMock(side_effect=_send_command)

    await asyncio.wait_for(
        session_events_module.handle_event(
            manager,
            CommandType.ACTION_TRIGGER,
            {
                "action_type": "exec",
                "cmd": "echo macro",
                "macro_exec_wait_id": "wait-1",
                "macro_exec_timeout_ms": 1250,
            },
        ),
        timeout=1.0,
    )

    await asyncio.wait_for(started.wait(), timeout=1.0)
    manager.client.send_command.assert_not_awaited()

    finish.set()
    await asyncio.wait_for(sent.wait(), timeout=1.0)

    manager.action_handler.execute_command.assert_awaited_once_with(
        "echo macro",
        timeout_s=1.25,
    )
    assert sent_commands[0].command == CommandType.MACRO_EXEC_COMPLETE
    assert sent_commands[0].data == {"wait_id": "wait-1", "returncode": 17}


@pytest.mark.asyncio
async def test_handle_event_macro_sync_exec_clamps_timeout_to_session_policy() -> None:
    manager = SessionManager()
    manager.security_policy.macro_exec_timeout_max_ms = 750
    manager.client.send_command = AsyncMock(return_value=SimpleNamespace(status="ok", data={}))

    async def _execute_command(_cmd: str, *, timeout_s: float = 300.0) -> int:
        assert timeout_s == pytest.approx(0.75)
        return 0

    manager.action_handler.execute_command = AsyncMock(side_effect=_execute_command)

    await session_events_module.handle_event(
        manager,
        CommandType.ACTION_TRIGGER,
        {
            "action_type": "exec",
            "cmd": "echo macro",
            "macro_exec_wait_id": "wait-1",
            "macro_exec_timeout_ms": 30_000,
        },
    )
    await asyncio.sleep(0)

    manager.action_handler.execute_command.assert_awaited_once_with(
        "echo macro",
        timeout_s=0.75,
    )


@pytest.mark.asyncio
async def test_handle_event_macro_sync_exec_logs_unexpected_completion_report_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(side_effect=RuntimeError("report bug"))
    manager.action_handler.execute_command = AsyncMock(return_value=0)

    with caplog.at_level(logging.ERROR, logger="keymasq-session"):
        await session_events_module.handle_event(
            manager,
            CommandType.ACTION_TRIGGER,
            {
                "action_type": "exec",
                "cmd": "echo macro",
                "macro_exec_wait_id": "wait-1",
            },
        )
        await asyncio.sleep(0)

    assert "Unexpected failure reporting macro exec completion" in caplog.text
    assert "report bug" in caplog.text


@pytest.mark.asyncio
async def test_handle_exec_trigger_ignores_missing_command_or_handler() -> None:
    manager = SessionManager()
    manager.action_handler.execute_command = AsyncMock()

    await session_events_module.handle_exec_trigger(manager, {"cmd": ""})

    manager.action_handler.execute_command.assert_not_awaited()
    manager.action_handler = None

    await session_events_module.handle_exec_trigger(manager, {"cmd": "echo missing"})


@pytest.mark.asyncio
async def test_handle_event_macro_sync_exec_tolerates_daemon_os_errors() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(side_effect=OSError("daemon down"))
    manager.action_handler.execute_command = AsyncMock(return_value=0)

    await session_events_module.handle_event(
        manager,
        CommandType.ACTION_TRIGGER,
        {
            "action_type": "exec",
            "cmd": "echo macro",
            "macro_exec_wait_id": "wait-1",
        },
    )
    await asyncio.sleep(0)

    manager.action_handler.execute_command.assert_awaited_once()
    manager.client.send_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_event_macro_trigger_forwards_full_playback_payload() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(
        side_effect=[
            SimpleNamespace(
                status="ok",
                data={
                    "macro": {
                        "name": "demo",
                        "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
                        "loop_mode": "count",
                        "loop_count": 2,
                        "move_to_start": False,
                        "start_x": 0,
                        "start_y": 0,
                        "block_mouse_movement": False,
                    }
                },
            ),
            SimpleNamespace(status="ok", data={}),
        ]
    )

    await session_events_module.handle_event(
        manager,
        CommandType.ACTION_TRIGGER,
        {
            "action_type": "macro",
            "macro_name": "demo",
            "replay_mouse_movement": False,
            "replay_mouse_clicks": False,
            "speed": 2.5,
            "loop_mode": "hold",
            "loop_count": 3,
            "loop_stop_behavior": "cancel_run",
            "move_to_start": True,
            "start_x": 11,
            "start_y": 22,
            "block_mouse_movement": True,
            "source_device": "1234:5678",
            "source_button": "btn_side",
            "trigger_value": 0,
        },
    )

    await asyncio.sleep(0)

    (play_call,) = manager.client.send_command.await_args_list
    assert play_call.args[0].command == CommandType.PLAY_MACRO
    assert play_call.args[0].data == {
        "macro_name": "demo",
        "macro_events": [],
        "replay_mouse_movement": False,
        "replay_mouse_clicks": False,
        "speed": 2.5,
        "loop_mode": "hold",
        "loop_count": 3,
        "loop_stop_behavior": "cancel_run",
        "move_to_start": True,
        "start_x": 11,
        "start_y": 22,
        "block_mouse_movement": True,
        "source_device": "1234:5678",
        "source_button": "btn_side",
        "trigger_value": 0,
    }


@pytest.mark.asyncio
async def test_handle_event_macro_trigger_logs_playback_exceptions(caplog) -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(side_effect=RuntimeError("daemon boom"))

    with caplog.at_level(logging.ERROR, logger="keymasq-session"):
        await session_events_module.handle_event(
            manager,
            CommandType.ACTION_TRIGGER,
            {"action_type": "macro", "macro_name": "demo"},
        )
        await asyncio.sleep(0)

    assert "Failed to play macro trigger" in caplog.text
    assert "daemon boom" in caplog.text


@pytest.mark.asyncio
async def test_lifetime_profile_enable_creates_runtime_activation_without_persisting(
    temp_config_dir,
) -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(return_value=SimpleNamespace(status="ok", data={}))
    manager.profiles.save_profile(ProfileConfig(name="Nav", enabled=False, is_permanent=True))

    await session_events_module.handle_profile_trigger(
        manager,
        {
            "action_type": "profile_enable",
            "profile_name": "Nav",
            "source_device": "1234:5678",
            "source_button": "btn_back",
            "trigger_id": "1234:5678:btn_back",
            "deactivation": {"after_actions": 1},
        },
    )

    assert manager.profiles.get_profile("Nav").config.enabled is False
    assert manager.profile_state.active_profile_names[-1] == "Nav"
    activation = manager.profile_state.runtime_profile_activations["Nav"]
    track_calls = _sent_daemon_commands(manager, CommandType.TRACK_PROFILE_ACTIVATION)
    assert len(track_calls) == 1
    assert track_calls[0].data == {
        "profile_name": "Nav",
        "activation_id": activation.activation_id,
        "trigger_id": "1234:5678:btn_back",
        "deactivation": {"after_actions": 1},
    }

    reloaded = manager.profiles.__class__()
    assert reloaded.get_profile("Nav").config.enabled is False


@pytest.mark.asyncio
async def test_lifetime_profile_enable_rolls_back_when_daemon_tracking_fails(
    temp_config_dir,
) -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(side_effect=ConnectionError("daemon unavailable"))
    manager.profiles.save_profile(ProfileConfig(name="Nav", enabled=False, is_permanent=True))

    await session_events_module.handle_profile_trigger(
        manager,
        {
            "action_type": "profile_enable",
            "profile_name": "Nav",
            "deactivation": {"after_actions": 1},
        },
    )

    assert "Nav" not in manager.profile_state.runtime_profile_activations
    assert "Nav" not in manager.profile_state.active_profile_names
    assert manager.profiles.get_profile("Nav").config.enabled is False


@pytest.mark.asyncio
async def test_lifetime_profile_enable_logs_unexpected_tracking_failures(
    temp_config_dir,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(side_effect=RuntimeError("tracking bug"))
    manager.profiles.save_profile(ProfileConfig(name="Nav", enabled=False, is_permanent=True))

    with caplog.at_level(logging.ERROR, logger="keymasq-session"):
        await session_events_module.handle_profile_trigger(
            manager,
            {
                "action_type": "profile_enable",
                "profile_name": "Nav",
                "deactivation": {"after_actions": 1},
            },
        )

    assert "Unexpected failure tracking runtime profile activation" in caplog.text
    assert "tracking bug" in caplog.text
    assert "Nav" not in manager.profile_state.runtime_profile_activations
    assert "Nav" not in manager.profile_state.active_profile_names
    assert manager.profiles.get_profile("Nav").config.enabled is False


@pytest.mark.asyncio
async def test_lifetime_profile_enable_rolls_back_when_daemon_rejects_tracking(
    temp_config_dir,
) -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(
        return_value=Response(status="error", error="unknown command")
    )
    manager.profiles.save_profile(ProfileConfig(name="Nav", enabled=False, is_permanent=True))

    await session_events_module.handle_profile_trigger(
        manager,
        {
            "action_type": "profile_enable",
            "profile_name": "Nav",
            "deactivation": {"after_actions": 1},
        },
    )

    assert "Nav" not in manager.profile_state.runtime_profile_activations
    assert "Nav" not in manager.profile_state.active_profile_names
    assert manager.profiles.get_profile("Nav").config.enabled is False


@pytest.mark.asyncio
async def test_explicit_profile_disable_cancels_runtime_activation(
    temp_config_dir,
) -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(return_value=SimpleNamespace(status="ok", data={}))
    manager.profiles.save_profile(ProfileConfig(name="Nav", enabled=False, is_permanent=True))

    await session_events_module.handle_profile_trigger(
        manager,
        {
            "action_type": "profile_enable",
            "profile_name": "Nav",
            "deactivation": {"after_actions": 1},
        },
    )
    activation = manager.profile_state.runtime_profile_activations["Nav"]

    result = await session_profiles_module.set_profile_enabled(manager, "Nav", False)

    assert result["status"] == "ok"
    assert result["enabled"] is False
    assert "Nav" not in manager.profile_state.runtime_profile_activations
    assert "Nav" not in manager.profile_state.active_profile_names
    cancel_calls = _sent_daemon_commands(manager, CommandType.CANCEL_PROFILE_ACTIVATION)
    assert len(cancel_calls) == 1
    assert cancel_calls[0].data == {
        "profile_name": "Nav",
        "activation_id": activation.activation_id,
    }


@pytest.mark.asyncio
async def test_set_profile_enabled_cancels_runtime_activation_with_single_reevaluate(
    temp_config_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(return_value=SimpleNamespace(status="ok", data={}))
    manager.profiles.save_profile(ProfileConfig(name="Nav", enabled=True, is_permanent=True))
    manager.profile_state.runtime_profile_activations["Nav"] = RuntimeProfileActivation(
        profile_name="Nav",
        activation_id="activation-1",
        sequence=1,
        deactivation=ProfileDeactivationPolicy(after_actions=1),
    )
    reevaluate_profiles = AsyncMock()
    monkeypatch.setattr(
        session_profiles_module,
        "reevaluate_profiles",
        reevaluate_profiles,
    )

    result = await session_profiles_module.set_profile_enabled(manager, "Nav", False)

    assert result["status"] == "ok"
    assert result["enabled"] is False
    assert "Nav" not in manager.profile_state.runtime_profile_activations
    reevaluate_profiles.assert_awaited_once_with(
        manager,
        reason="profile Nav enabled=False",
    )
    cancel_calls = _sent_daemon_commands(manager, CommandType.CANCEL_PROFILE_ACTIVATION)
    assert len(cancel_calls) == 1
    assert cancel_calls[0].data == {
        "profile_name": "Nav",
        "activation_id": "activation-1",
    }


@pytest.mark.asyncio
async def test_lifetime_profile_toggle_creates_and_cancels_runtime_activation(
    temp_config_dir,
) -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(return_value=SimpleNamespace(status="ok", data={}))
    manager.profiles.save_profile(ProfileConfig(name="Nav", enabled=False, is_permanent=True))
    trigger = {
        "action_type": "profile_toggle",
        "profile_name": "Nav",
        "deactivation": {"after_actions": 1, "timeout_ms": 1500},
    }

    await session_events_module.handle_profile_trigger(manager, trigger)

    assert manager.profiles.get_profile("Nav").config.enabled is False
    activation = manager.profile_state.runtime_profile_activations["Nav"]
    track_calls = _sent_daemon_commands(manager, CommandType.TRACK_PROFILE_ACTIVATION)
    assert len(track_calls) == 1
    assert track_calls[0].data == {
        "profile_name": "Nav",
        "activation_id": activation.activation_id,
        "trigger_id": ":",
        "deactivation": {
            "after_actions": 1,
            "timeout_ms": 1500,
        },
    }

    await session_events_module.handle_profile_trigger(manager, trigger)

    assert "Nav" not in manager.profile_state.runtime_profile_activations
    assert manager.profiles.get_profile("Nav").config.enabled is False
    cancel_calls = _sent_daemon_commands(manager, CommandType.CANCEL_PROFILE_ACTIVATION)
    assert len(cancel_calls) == 1
    assert cancel_calls[0].data == {
        "profile_name": "Nav",
        "activation_id": activation.activation_id,
    }

    reloaded = manager.profiles.__class__()
    assert reloaded.get_profile("Nav").config.enabled is False


@pytest.mark.asyncio
async def test_runtime_activation_replacement_ignores_stale_expiry(
    temp_config_dir,
) -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(return_value=SimpleNamespace(status="ok", data={}))
    manager.profiles.save_profile(ProfileConfig(name="Nav", enabled=False, is_permanent=True))
    trigger = {
        "action_type": "profile_enable",
        "profile_name": "Nav",
        "deactivation": {"timeout_ms": 1500},
    }

    await session_events_module.handle_profile_trigger(manager, trigger)
    old_id = manager.profile_state.runtime_profile_activations["Nav"].activation_id
    await session_events_module.handle_profile_trigger(manager, trigger)
    new_id = manager.profile_state.runtime_profile_activations["Nav"].activation_id

    await session_events_module.handle_profile_deactivate_requested(
        manager,
        {"profile_name": "Nav", "activation_id": old_id, "reason": "timeout"},
    )
    assert manager.profile_state.runtime_profile_activations["Nav"].activation_id == new_id

    await session_events_module.handle_profile_deactivate_requested(
        manager,
        {"profile_name": "Nav", "activation_id": new_id, "reason": "timeout"},
    )
    assert "Nav" not in manager.profile_state.runtime_profile_activations


@pytest.mark.asyncio
async def test_macro_recording_trigger_edge_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.send_notification = Mock()  # type: ignore[method-assign]
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]

    await session_events_module.handle_start_macro_trigger(manager, {})

    manager.recording_state.active = True
    manager.recording_state.active_slot = 2
    stop_trigger = AsyncMock()
    monkeypatch.setattr(session_events_module, "handle_stop_macro_trigger", stop_trigger)

    await session_events_module.handle_start_macro_trigger(manager, {"recording_slot": 2})
    await session_events_module.handle_start_macro_trigger(manager, {"recording_slot": 3})

    manager.recording_state.active = False
    monkeypatch.setattr(
        session_events_module.runtime_recording,
        "resolve_macro_recording_status_async",
        AsyncMock(return_value={"unlocked": False, "source": "disabled"}),
    )
    notify_disabled = Mock()
    monkeypatch.setattr(
        session_events_module.runtime_recording,
        "notify_macro_recording_disabled",
        notify_disabled,
    )

    await session_events_module.handle_start_macro_trigger(manager, {"recording_slot": 1})

    assert manager.send_notification.call_count == 2  # type: ignore[attr-defined]
    stop_trigger.assert_awaited_once_with(manager, {"recording_slot": 2})
    notify_disabled.assert_called_once_with(manager)
    manager.broadcast_to_session_clients.assert_called_once_with(  # type: ignore[attr-defined]
        {
            "event": "macro_recording_disabled",
            "macro_recording_enabled": False,
            "macro_recording_source": "disabled",
            "macro_recording_expires_at": 0,
        }
    )


@pytest.mark.asyncio
async def test_macro_recording_trigger_reports_failed_start_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    monkeypatch.setattr(
        session_events_module.runtime_recording,
        "resolve_macro_recording_status_async",
        AsyncMock(return_value={"unlocked": True}),
    )
    start_recording = AsyncMock(
        side_effect=[
            {"status": "error", "error_code": "macro_recording_disabled"},
            {"status": "error", "error_code": "sensitive_command_denied"},
        ]
    )
    notify_disabled = Mock()
    notify_unlock = Mock()
    monkeypatch.setattr(session_events_module.runtime_recording, "start_recording", start_recording)
    monkeypatch.setattr(
        session_events_module.runtime_recording,
        "notify_macro_recording_disabled",
        notify_disabled,
    )
    monkeypatch.setattr(
        session_events_module.runtime_recording,
        "notify_recording_unlock_required",
        notify_unlock,
    )

    await session_events_module.handle_start_macro_trigger(manager, {"recording_slot": 1})
    await session_events_module.handle_start_macro_trigger(manager, {"recording_slot": 1})

    notify_disabled.assert_called_once_with(manager)
    notify_unlock.assert_called_once_with(
        manager,
        {"status": "error", "error_code": "sensitive_command_denied"},
    )
    assert manager.broadcast_to_session_clients.call_args_list == [  # type: ignore[attr-defined]
        call({"event": "macro_recording_disabled"}),
        call({"event": "recording_auth_requested"}),
    ]


@pytest.mark.asyncio
async def test_stop_macro_trigger_edge_and_error_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    stop_recording = AsyncMock(side_effect=[OSError("daemon down"), RuntimeError("bug")])
    monkeypatch.setattr(
        session_events_module.runtime_recording,
        "stop_recording",
        stop_recording,
    )

    await session_events_module.handle_stop_macro_trigger(manager, {"recording_slot": 1})

    manager.recording_state.active = True
    manager.recording_state.active_slot = 2
    await session_events_module.handle_stop_macro_trigger(manager, {"recording_slot": 3})
    await session_events_module.handle_stop_macro_trigger(manager, {"recording_slot": 2})
    await session_events_module.handle_stop_macro_trigger(manager, {"recording_slot": 2})

    assert stop_recording.await_count == 2
    assert stop_recording.await_args_list[0].kwargs == {
        "error_if_idle": False,
        "recording_slot": 2,
    }


@pytest.mark.asyncio
async def test_cancel_and_emergency_triggers_tolerate_daemon_failures() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(
        side_effect=[
            OSError("daemon down"),
            RuntimeError("bug"),
            OSError("daemon down"),
            RuntimeError("bug"),
        ]
    )

    await session_events_module.handle_cancel_macro_trigger(manager)
    await session_events_module.handle_cancel_macro_trigger(manager)
    await session_events_module.handle_emergency_reset_trigger(manager)
    await session_events_module.handle_emergency_reset_trigger(manager)

    assert [
        call_args.args[0].command
        for call_args in manager.client.send_command.await_args_list
    ] == [
        CommandType.CANCEL_MACRO_PLAYBACK,
        CommandType.CANCEL_MACRO_PLAYBACK,
        CommandType.EMERGENCY_RESET,
        CommandType.EMERGENCY_RESET,
    ]


@pytest.mark.asyncio
async def test_runtime_reset_reports_reapply_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    manager.send_notification = Mock()  # type: ignore[method-assign]
    monkeypatch.setattr(
        session_profiles_module,
        "reevaluate_profiles",
        AsyncMock(side_effect=OSError("daemon down")),
    )

    await session_events_module.handle_runtime_reset_event(manager, {"reason": "test"})

    monkeypatch.setattr(
        session_profiles_module,
        "reevaluate_profiles",
        AsyncMock(side_effect=RuntimeError("bug")),
    )
    await session_events_module.handle_runtime_reset_event(manager, {"reason": "test"})

    assert manager.broadcast_to_session_clients.call_count == 2  # type: ignore[attr-defined]
    assert manager.send_notification.call_args_list[-1] == call(  # type: ignore[attr-defined]
        "Keymasq: Reapply Failed",
        "Emergency reset completed, but active profiles could not be reapplied.",
    )


@pytest.mark.asyncio
async def test_device_topology_events_schedule_known_hardware_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.hardware.get_hardware = Mock(  # type: ignore[method-assign]
        side_effect=lambda hardware_id: (
            SimpleNamespace(name="Keyboard") if hardware_id == "1234:5678" else None
        )
    )
    manager.hardware.list_hardware = Mock(  # type: ignore[method-assign]
        return_value=[SimpleNamespace(model_id="abcd:ef01")]
    )
    schedule_topology_refresh = Mock()
    created_tasks = []

    def create_event_task(_manager, coro, **kwargs):
        coro.close()
        created_tasks.append(kwargs)
        return Mock()

    monkeypatch.setattr(
        session_profiles_module,
        "schedule_topology_refresh",
        schedule_topology_refresh,
    )
    monkeypatch.setattr(session_events_module, "create_event_task", create_event_task)

    await session_events_module.on_device_connected(
        manager,
        {"vendor_id": "1234", "product_id": "5678"},
    )
    await session_events_module.on_device_disconnected(
        manager,
        {"vendor_id": "abcd", "product_id": "ef01"},
    )
    await session_events_module.on_device_connected(
        manager,
        {"vendor_id": "0000", "product_id": "0000"},
    )
    await session_events_module.on_device_disconnected(manager, {})

    assert schedule_topology_refresh.call_count == 2
    assert [task["name"] for task in created_tasks] == [
        "recording_devices_refresh",
        "recording_devices_refresh",
    ]
    assert session_events_module.device_name_for_hardware(manager, "missing:id") == "missing:id"
    assert session_events_module._hardware_or_model_known(manager, "abcd:ef01") is True
    assert session_events_module.event_log_view({"events": [1, 2]}) == {
        "events": "<2 events>",
        "event_count": 2,
    }
    assert session_events_module.event_log_view({"events": [1], "event_count": 9}) == {
        "events": "<1 events>",
        "event_count": 9,
    }


@pytest.mark.asyncio
async def test_no_lifetime_disable_cancels_runtime_activation_and_persists_disabled(
    temp_config_dir,
) -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(return_value=SimpleNamespace(status="ok", data={}))
    manager.profiles.save_profile(ProfileConfig(name="Nav", enabled=True, is_permanent=True))

    await session_events_module.handle_profile_trigger(
        manager,
        {
            "action_type": "profile_enable",
            "profile_name": "Nav",
            "deactivation": {"timeout_ms": 1500},
        },
    )
    activation_id = manager.profile_state.runtime_profile_activations["Nav"].activation_id
    await session_events_module.handle_profile_trigger(
        manager,
        {"action_type": "profile_disable", "profile_name": "Nav"},
    )

    assert "Nav" not in manager.profile_state.runtime_profile_activations
    assert manager.profiles.get_profile("Nav").config.enabled is False
    cancel_calls = _sent_daemon_commands(manager, CommandType.CANCEL_PROFILE_ACTIVATION)
    assert cancel_calls[-1].data == {
        "profile_name": "Nav",
        "activation_id": activation_id,
    }


@pytest.mark.asyncio
async def test_no_lifetime_toggle_cancels_runtime_activation_before_persisting_enabled(
    temp_config_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(return_value=SimpleNamespace(status="ok", data={}))
    manager.profiles.save_profile(ProfileConfig(name="Nav", enabled=False, is_permanent=True))

    await session_events_module.handle_profile_trigger(
        manager,
        {
            "action_type": "profile_enable",
            "profile_name": "Nav",
            "deactivation": {"timeout_ms": 1500},
        },
    )
    activation_id = manager.profile_state.runtime_profile_activations["Nav"].activation_id
    real_set_profile_enabled = session_events_module.runtime_profiles.set_profile_enabled
    activation_cancelled_before_persist = False

    async def observe_set_profile_enabled(
        manager_arg: SessionManager,
        profile_name: str,
        enabled: bool | None,
    ) -> dict[str, object]:
        nonlocal activation_cancelled_before_persist
        activation_cancelled_before_persist = (
            profile_name not in manager.profile_state.runtime_profile_activations
        )
        return await real_set_profile_enabled(manager_arg, profile_name, enabled)

    monkeypatch.setattr(
        session_events_module.runtime_profiles,
        "set_profile_enabled",
        observe_set_profile_enabled,
    )

    await session_events_module.handle_profile_trigger(
        manager,
        {"action_type": "profile_toggle", "profile_name": "Nav"},
    )

    assert activation_cancelled_before_persist is True
    assert "Nav" not in manager.profile_state.runtime_profile_activations
    assert manager.profiles.get_profile("Nav").config.enabled is True
    cancel_calls = _sent_daemon_commands(manager, CommandType.CANCEL_PROFILE_ACTIVATION)
    assert cancel_calls[-1].data == {
        "profile_name": "Nav",
        "activation_id": activation_id,
    }


@pytest.mark.asyncio
async def test_device_disconnect_event_invalidates_cached_grabs_and_reevaluates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(name="Test Mouse")  # type: ignore[assignment]
    manager.profile_state.grabbed_devices = {"1234:5678"}
    manager.profile_state.grabbed_interfaces = {"1234:5678": {"mouse": "/dev/input/event10"}}
    manager.profile_state.last_sent_mapping_signatures = {"1234:5678": "sig"}
    manager.profile_state.last_sent_combo_signature = "combo"
    manager.exec_state.device_exec_refs = {"1234:5678": {7}}
    manager.exec_state.combo_exec_refs = {8}
    manager.exec_state.exec_refs = {
        7: ExecBinding(cmd="echo device", owner="device", hardware_id="1234:5678"),
        8: ExecBinding(cmd="echo combo", owner="combo"),
    }
    monkeypatch.setattr(session_profiles_module, "reevaluate_profiles", AsyncMock())

    async def instant_sleep(_delay: float) -> None:
        return

    monkeypatch.setattr(session_profiles_module.asyncio, "sleep", instant_sleep)

    await session_events_module.handle_event(
        manager,
        CommandType.DEVICE_DISCONNECTED,
        {"hardware_id": "1234:5678", "vendor_id": "1234", "product_id": "5678"},
    )

    task = manager.profile_state.topology_refresh_task
    assert task is not None
    await task

    assert manager.profile_state.grabbed_devices == set()
    assert manager.profile_state.grabbed_interfaces == {}
    assert manager.profile_state.last_sent_mapping_signatures == {}
    assert manager.profile_state.last_sent_combo_signature == ""
    assert manager.exec_state.exec_refs == {}
    session_profiles_module.reevaluate_profiles.assert_awaited_once()  # type: ignore[attr-defined]


def test_handle_device_grab_status_waiting_notifies_once_and_broadcasts() -> None:
    manager = SessionManager()
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(name="Test Keyboard")  # type: ignore[assignment]
    manager.send_notification = Mock()  # type: ignore[method-assign]
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]

    event = {
        "hardware_id": "1234:5678",
        "state": "waiting",
        "active_keys": ["key_l"],
        "waited_s": 1.2,
    }

    session_events_module.handle_device_grab_status_event(manager, event)
    session_events_module.handle_device_grab_status_event(manager, event)

    manager.send_notification.assert_called_once_with(  # type: ignore[attr-defined]
        "Keymasq: Grab Pending",
        "Test Keyboard: waiting for keys to be released (key_l).",
    )
    assert manager.broadcast_to_session_clients.call_args_list == [  # type: ignore[attr-defined]
        call({"event": "device_grab_status", **event}),
        call({"event": "device_grab_status", **event}),
    ]
    assert manager.profile_state.grab_waiting_devices == {"1234:5678"}


def test_handle_device_grab_status_ready_reapplies_waiting_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.session_clients.add(object())  # type: ignore[arg-type]
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    created_tasks = []

    def create_event_task(_manager, coro, **kwargs):
        coro.close()
        created_tasks.append(kwargs)
        return Mock()

    monkeypatch.setattr(session_events_module, "create_event_task", create_event_task)
    reevaluate_profiles = AsyncMock()
    monkeypatch.setattr(session_profiles_module, "reevaluate_profiles", reevaluate_profiles)
    hardware_id = "1234:5678"
    event = {"hardware_id": hardware_id, "state": "ready"}
    manager.profile_state.grab_waiting_devices.add(hardware_id)
    manager.profile_state.grab_status[hardware_id] = {
        "state": "waiting",
        "path": "/dev/input/event4",
    }

    session_events_module.handle_device_grab_status_event(manager, event)

    assert hardware_id not in manager.profile_state.grab_waiting_devices
    assert hardware_id not in manager.profile_state.grab_status
    assert created_tasks == [{"name": "grab_ready"}]
    reevaluate_profiles.assert_called_once_with(manager, reason=f"grab ready for {hardware_id}")
    profiles_event = {
        "event": "profiles_changed",
        "runtime_only": True,
        **session_profiles_module.build_active_profiles_payload(manager),
    }
    manager.broadcast_to_session_clients.assert_has_calls(  # type: ignore[attr-defined]
        [
            call({"event": "device_grab_status", **event}),
            call(profiles_event),
        ]
    )


def test_handle_device_grab_status_timeout_notifies_and_schedules_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(name="Test Keyboard")  # type: ignore[assignment]
    manager.send_notification = Mock()  # type: ignore[method-assign]
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    schedule_grab_retry = Mock()
    monkeypatch.setattr(session_profiles_module, "schedule_grab_retry", schedule_grab_retry)
    manager.profile_state.grab_waiting_devices.add("1234:5678")

    event = {
        "hardware_id": "1234:5678",
        "state": "timed_out",
        "active_keys": ["key_l"],
        "waited_s": 300.0,
    }

    session_events_module.handle_device_grab_status_event(manager, event)

    manager.send_notification.assert_called_once_with(  # type: ignore[attr-defined]
        "Keymasq: Grab Timed Out",
        "Test Keyboard: keys stayed down too long (key_l). Retrying automatically.",
    )
    schedule_grab_retry.assert_called_once_with(
        manager,
        "1234:5678",
        session_events_module.GRAB_RETRY_DELAY_S,
    )
    manager.broadcast_to_session_clients.assert_called_once_with(  # type: ignore[attr-defined]
        {"event": "device_grab_status", **event}
    )
    assert manager.profile_state.grab_waiting_devices == set()
