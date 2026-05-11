import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

import keymasq.session.manager.events as session_events_module
import keymasq.session.manager.profiles as session_profiles_module
from keymasq.common.ipc import CommandType
from keymasq.session.listeners.hyprland import HyprlandListener
from keymasq.session.manager import SessionManager
from keymasq.session.manager.state import ExecBinding


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
async def test_handle_event_set_cursor_position_uses_native_listener_and_replies() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(return_value=SimpleNamespace(status="ok", data={}))
    listener = SimpleNamespace(
        supports_native_cursor_position_set=True,
        set_cursor_position=AsyncMock(return_value=(True, "ok")),
    )
    manager.compositor_state.window_listener = listener

    await session_events_module.handle_event(
        manager,
        CommandType.SET_CURSOR_POSITION,
        {"request_id": "cursor-1", "x": 123, "y": 456},
    )
    await asyncio.sleep(0)

    listener.set_cursor_position.assert_awaited_once_with(123, 456)
    sent = manager.client.send_command.await_args.args[0]
    assert sent.command == CommandType.SET_CURSOR_POSITION_RESULT
    assert sent.data == {"request_id": "cursor-1", "ok": True, "message": "ok"}


@pytest.mark.asyncio
async def test_handle_event_set_cursor_position_logs_listener_request(caplog) -> None:
    manager = SessionManager()
    manager.verbosity = 1
    manager.client.send_command = AsyncMock(return_value=SimpleNamespace(status="ok", data={}))
    listener = SimpleNamespace(
        supports_native_cursor_position_set=True,
        set_cursor_position=AsyncMock(return_value=(True, "ok")),
    )
    manager.compositor_state.window_listener = listener

    with caplog.at_level(logging.DEBUG, logger="keymasq-session"):
        await session_events_module.handle_event(
            manager,
            CommandType.SET_CURSOR_POSITION,
            {"request_id": "cursor-1", "x": 123, "y": 456},
        )
        await asyncio.sleep(0)

    assert (
        "Setting cursor position through SimpleNamespace listener: "
        "request_id=cursor-1 x=123 y=456"
    ) in caplog.text


@pytest.mark.asyncio
async def test_handle_event_set_cursor_position_rejects_non_native_listener() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(return_value=SimpleNamespace(status="ok", data={}))
    listener = SimpleNamespace(
        supports_native_cursor_position_set=False,
        set_cursor_position=AsyncMock(return_value=(True, "ok")),
    )
    manager.compositor_state.window_listener = listener

    await session_events_module.handle_event(
        manager,
        CommandType.SET_CURSOR_POSITION,
        {"request_id": "cursor-1", "x": 123, "y": 456},
    )
    await asyncio.sleep(0)

    listener.set_cursor_position.assert_not_awaited()
    sent = manager.client.send_command.await_args.args[0]
    assert sent.command == CommandType.SET_CURSOR_POSITION_RESULT
    assert sent.data["request_id"] == "cursor-1"
    assert sent.data["ok"] is False


@pytest.mark.asyncio
async def test_handle_event_set_cursor_position_does_not_block_listener_loop() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(return_value=SimpleNamespace(status="ok", data={}))
    started = asyncio.Event()
    finish = asyncio.Event()

    async def set_cursor_position(_x: int, _y: int) -> tuple[bool, str]:
        started.set()
        await finish.wait()
        return True, "ok"

    listener = SimpleNamespace(
        supports_native_cursor_position_set=True,
        set_cursor_position=AsyncMock(side_effect=set_cursor_position),
    )
    manager.compositor_state.window_listener = listener

    await asyncio.wait_for(
        session_events_module.handle_event(
            manager,
            CommandType.SET_CURSOR_POSITION,
            {"request_id": "cursor-1", "x": 123, "y": 456},
        ),
        timeout=0.05,
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)

    assert manager.client.send_command.await_count == 0
    tasks = list(manager.compositor_state.cursor_position_tasks)
    finish.set()
    await asyncio.gather(*tasks)
    assert manager.client.send_command.await_count == 1


@pytest.mark.asyncio
async def test_handle_event_set_cursor_position_missing_request_id_is_one_way() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(return_value=SimpleNamespace(status="ok", data={}))
    listener = SimpleNamespace(
        supports_native_cursor_position_set=True,
        set_cursor_position=AsyncMock(return_value=(True, "ok")),
    )
    manager.compositor_state.window_listener = listener

    await session_events_module.handle_event(
        manager,
        CommandType.SET_CURSOR_POSITION,
        {"x": 123, "y": 456},
    )
    await asyncio.sleep(0)

    listener.set_cursor_position.assert_awaited_once_with(123, 456)
    manager.client.send_command.assert_not_awaited()


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

    async def _execute_command(cmd: str) -> int:
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
            },
        ),
        timeout=1.0,
    )

    await asyncio.wait_for(started.wait(), timeout=1.0)
    manager.client.send_command.assert_not_awaited()

    finish.set()
    await asyncio.wait_for(sent.wait(), timeout=1.0)

    manager.action_handler.execute_command.assert_awaited_once_with("echo macro")
    assert sent_commands[0].command == CommandType.MACRO_EXEC_COMPLETE
    assert sent_commands[0].data == {"wait_id": "wait-1", "returncode": 17}


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


def test_handle_device_grab_status_timeout_notifies_and_schedules_retry() -> None:
    manager = SessionManager()
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(name="Test Keyboard")  # type: ignore[assignment]
    manager.send_notification = Mock()  # type: ignore[method-assign]
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    monkeypatch = pytest.MonkeyPatch()
    schedule_grab_retry = Mock()
    monkeypatch.setattr(session_profiles_module, "schedule_grab_retry", schedule_grab_retry)
    manager.profile_state.grab_waiting_devices.add("1234:5678")

    event = {
        "hardware_id": "1234:5678",
        "state": "timed_out",
        "active_keys": ["key_l"],
        "waited_s": 300.0,
    }

    try:
        session_events_module.handle_device_grab_status_event(manager, event)
    finally:
        monkeypatch.undo()

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
