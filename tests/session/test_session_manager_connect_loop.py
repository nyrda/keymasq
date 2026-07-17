import asyncio
from unittest.mock import AsyncMock, Mock, call

import pytest

import keymasq.session.manager.core as session_core_module
import keymasq.session.manager.recording_device_selection as recording_device_selection_module
from keymasq.common.ipc import CommandType, Response
from keymasq.session.manager.core import SessionManager
from keymasq.session.manager.state import ExecBinding


@pytest.mark.asyncio
async def test_keymasqd_disconnect_clears_runtime_state() -> None:
    manager = SessionManager()
    retry_task = asyncio.create_task(asyncio.sleep(60))
    manager.connected = True
    manager.profile_state.grabbed_devices.add("1234:5678")
    manager.profile_state.grabbed_interfaces["1234:5678"] = {"event0": "/dev/input/event0"}
    manager.profile_state.grab_waiting_devices.add("1234:5678")
    manager.profile_state.grab_retry_tasks["1234:5678"] = retry_task
    manager.profile_state.last_sent_grab_signatures["1234:5678"] = "grab"
    manager.profile_state.last_sent_mapping_signatures["1234:5678"] = "mapping"
    manager.profile_state.last_sent_combo_signature = "combos"
    manager.profile_state.active_profile_names = ["Default"]
    manager.profile_state.resolved_devices["1234:5678"] = object()  # type: ignore[assignment]
    manager.recording_state.active = True
    manager.recording_state.active_slot = 2
    manager.recording_state.start_cursor = (100, 200)
    manager.recording_state.active_owner_writer_id = 123
    manager.recording_state.active_owner_pid = 456
    manager.recording_state.active_owner_uid = 1000
    manager.recording_state.devices_cache = [{"name": "Keyboard"}]
    manager.recording_state.selected_devices_cache = [{"name": "Keyboard"}]
    manager.recording_state.devices_cache_ready = True
    manager.recording_state.devices_cache_include_other = True
    manager.capture_state.tokens["1234:5678"] = "stale-token"
    manager.capture_state.locks.add("1234:5678")
    manager.capture_state.resume_profiles["1234:5678"] = ["Default"]
    manager.capture_state.owner_writer_ids["1234:5678"] = 42
    manager.exec_state.exec_refs[7] = ExecBinding(
        cmd="notify-send stale",
        owner="device",
        hardware_id="1234:5678",
    )
    manager.exec_state.device_exec_refs["1234:5678"] = {7}
    manager.exec_state.combo_exec_refs.add(8)
    manager.exec_state.next_exec_ref = 9
    manager._broadcast_keymasqd_status = Mock()  # type: ignore[method-assign]

    manager._handle_keymasqd_disconnect()
    await asyncio.sleep(0)

    assert manager.connected is False
    assert manager.profile_state.grabbed_devices == set()
    assert manager.profile_state.grabbed_interfaces == {}
    assert manager.profile_state.grab_waiting_devices == set()
    assert retry_task.cancelled()
    assert manager.profile_state.grab_retry_tasks == {}
    assert manager.profile_state.last_sent_grab_signatures == {}
    assert manager.profile_state.last_sent_mapping_signatures == {}
    assert manager.profile_state.last_sent_combo_signature == ""
    assert manager.profile_state.active_profile_names == []
    assert manager.profile_state.resolved_devices == {}
    assert manager.recording_state.active is False
    assert manager.recording_state.active_slot == 0
    assert manager.recording_state.start_cursor is None
    assert manager.recording_state.active_owner_writer_id is None
    assert manager.recording_state.active_owner_pid is None
    assert manager.recording_state.active_owner_uid is None
    assert manager.recording_state.devices_cache == []
    assert manager.recording_state.selected_devices_cache == []
    assert manager.recording_state.devices_cache_ready is False
    assert manager.recording_state.devices_cache_include_other is False
    assert manager.capture_state.tokens == {}
    assert manager.capture_state.locks == set()
    assert manager.capture_state.resume_profiles == {}
    assert manager.capture_state.owner_writer_ids == {}
    assert manager.exec_state.exec_refs == {}
    assert manager.exec_state.device_exec_refs == {}
    assert manager.exec_state.combo_exec_refs == set()
    assert manager.exec_state.next_exec_ref == 9
    manager._broadcast_keymasqd_status.assert_called_once_with(False)  # type: ignore[attr-defined]

    with pytest.raises(asyncio.CancelledError):
        await retry_task


@pytest.mark.asyncio
async def test_connect_loop_requests_session_restart_after_established_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        async def connect(self) -> None:
            return None

        async def wait_disconnected(self) -> None:
            return None

        async def send_command(self, _command: object) -> Response:
            return Response(status="ok", data={"count": 1})

    manager = SessionManager()
    manager.running = True
    manager.restart_on_daemon_disconnect = True
    manager.client = FakeClient()  # type: ignore[assignment]
    manager._broadcast_keymasqd_status = Mock()  # type: ignore[method-assign]
    activate_initial_profiles = AsyncMock()
    refresh_devices = AsyncMock()
    monkeypatch.setattr(
        session_core_module.coordinator,
        "activate_initial_profiles",
        activate_initial_profiles,
    )
    monkeypatch.setattr(
        recording_device_selection_module,
        "refresh_recording_devices_cache",
        refresh_devices,
    )

    await manager.connect_loop()

    assert manager.restart_requested is True
    assert manager._shutdown_event.is_set()
    assert manager.connected is False
    manager._broadcast_keymasqd_status.assert_has_calls(  # type: ignore[attr-defined]
        [call(True), call(False)]
    )
    activate_initial_profiles.assert_awaited_once_with(manager)
    refresh_devices.assert_awaited_once_with(manager)


@pytest.mark.asyncio
async def test_reload_profiles_releases_removed_hardware_immediately(monkeypatch) -> None:
    manager = SessionManager()
    hardware_id = "045e:02a1"
    manager.profile_state.grabbed_devices.add(hardware_id)
    manager.profile_state.grabbed_interfaces[hardware_id] = {"gamepad": "/dev/input/event20"}
    manager.reload_config_from_disk = Mock()  # type: ignore[method-assign]
    manager.hardware.list_hardware_ids = Mock(return_value=[])  # type: ignore[method-assign]
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"released": True})
    )
    reevaluate_profiles = AsyncMock()
    monkeypatch.setattr(
        session_core_module.coordinator,
        "reevaluate_profiles",
        reevaluate_profiles,
    )

    await manager.reload_profiles()

    sent = manager.client.send_command.await_args.args[0]
    assert sent.command == CommandType.RELEASE_DEVICE
    assert sent.data == {"hardware_id": hardware_id, "immediate": True}
    assert hardware_id not in manager.profile_state.grabbed_devices
    assert hardware_id not in manager.profile_state.grabbed_interfaces
    reevaluate_profiles.assert_awaited_once_with(manager, reason="config reload")
