import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

import keymasq.session.manager.events as session_events_module
import keymasq.session.manager.profiles as session_profiles_module
from keymasq.common.ipc import CommandType
from keymasq.session.listeners.hyprland import HyprlandListener
from keymasq.session.manager import SessionManager


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
async def test_handle_event_exec_ref_runs_command_once() -> None:
    manager = SessionManager()
    manager.exec_state.exec_refs[7] = "echo once"
    manager.action_handler.execute_command = AsyncMock(return_value=0)

    await session_events_module.handle_event(
        manager,
        CommandType.ACTION_TRIGGER,
        {
            "action_type": "exec",
            "exec_ref": 7,
            "source_device": "1234:5678",
            "source_button": "btn_side",
        },
    )

    await asyncio.sleep(0)
    manager.action_handler.execute_command.assert_awaited_once_with("echo once")


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

    get_call, play_call = manager.client.send_command.await_args_list
    assert get_call.args[0].command == CommandType.MACRO_GET
    assert get_call.args[0].data == {"name": "demo"}
    assert play_call.args[0].command == CommandType.PLAY_MACRO
    assert play_call.args[0].data == {
        "macro_name": "demo",
        "macro_events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
        "replay_mouse_movement": False,
        "replay_mouse_clicks": False,
        "speed": 2.5,
        "loop_mode": "hold",
        "loop_count": 3,
        "move_to_start": True,
        "start_x": 11,
        "start_y": 22,
        "block_mouse_movement": True,
        "source_device": "1234:5678",
        "source_button": "btn_side",
        "trigger_value": 0,
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
    manager.exec_state.exec_refs = {7: "echo device", 8: "echo combo"}
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
