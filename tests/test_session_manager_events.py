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
    manager.client.send_command = AsyncMock(side_effect=RuntimeError("daemon unavailable"))
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
