# ruff: noqa: F403, F405, I001
from tests.session.command_support import *
from unittest.mock import call
import keymasq.session.manager.core as session_core_module


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
    manager.recording_state.devices_cache = [{"name": "Keyboard"}]
    manager.recording_state.selected_devices_cache = [{"name": "Keyboard"}]
    manager.recording_state.devices_cache_ready = True
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
    assert manager.recording_state.devices_cache == []
    assert manager.recording_state.selected_devices_cache == []
    assert manager.recording_state.devices_cache_ready is False
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

    manager = SessionManager()
    manager.running = True
    manager.restart_on_daemon_disconnect = True
    manager.client = FakeClient()  # type: ignore[assignment]
    manager._broadcast_keymasqd_status = Mock()  # type: ignore[method-assign]
    sync_cursor = AsyncMock()
    activate_initial_profiles = AsyncMock()
    refresh_devices = AsyncMock()
    monkeypatch.setattr(
        session_core_module.runtime_compositor,
        "sync_cursor_position_backend",
        sync_cursor,
    )
    monkeypatch.setattr(
        session_core_module.runtime_profiles,
        "activate_initial_profiles",
        activate_initial_profiles,
    )
    monkeypatch.setattr(
        session_core_module.runtime_recording,
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
    sync_cursor.assert_awaited_once_with(manager)
    activate_initial_profiles.assert_awaited_once_with(manager)
    refresh_devices.assert_awaited_once_with(manager)
