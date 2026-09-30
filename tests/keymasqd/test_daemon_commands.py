from unittest.mock import Mock

import pytest

from keymasq.common.ipc import CommandType
from keymasq.common.security import SecurityPolicy
from tests.keymasqd.daemon_support import client_context


@pytest.mark.asyncio
async def test_set_diagnostics_forwards_with_type_conversion(daemon_testbed):
    daemon, device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed

    result = await daemon._handle_command(
        CommandType.SET_DIAGNOSTICS,
        {"enabled": 1, "interval": "3.25"},
    )

    assert result == {"status": "ok"}
    device_manager.set_diagnostics.assert_awaited_once_with(True, 3.25, None)


@pytest.mark.asyncio
async def test_set_diagnostics_forwards_categories(daemon_testbed):
    daemon, device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed

    result = await daemon._handle_command(
        CommandType.SET_DIAGNOSTICS,
        {"enabled": 1, "interval": "3.25", "categories": ["mainline", "combo"]},
    )

    assert result == {"status": "ok"}
    device_manager.set_diagnostics.assert_awaited_once_with(
        True,
        3.25,
        ["mainline", "combo"],
    )


@pytest.mark.asyncio
async def test_device_inspector_commands_forward_to_device_manager(daemon_testbed):
    daemon, device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed

    await daemon._handle_command(
        CommandType.DEVICE_INSPECTOR_START,
        {"hardware_id": "1234:5678"},
    )
    await daemon._handle_command(
        CommandType.DEVICE_INSPECTOR_ENABLE_SUPPRESSION,
        {"hardware_id": "1234:5678"},
    )
    await daemon._handle_command(
        CommandType.DEVICE_INSPECTOR_DISABLE_SUPPRESSION,
        {"hardware_id": "1234:5678", "reason": "key_esc"},
    )
    await daemon._handle_command(
        CommandType.DEVICE_INSPECTOR_STOP,
        {"hardware_id": "1234:5678"},
    )

    device_manager.start_device_inspector.assert_awaited_once_with(  # type: ignore[attr-defined]
        hardware_id="1234:5678"
    )
    device_manager.enable_device_inspector_suppression.assert_awaited_once_with(  # type: ignore[attr-defined]
        hardware_id="1234:5678"
    )
    device_manager.disable_device_inspector_suppression.assert_awaited_once_with(  # type: ignore[attr-defined]
        hardware_id="1234:5678",
        reason="key_esc",
    )
    device_manager.stop_device_inspector.assert_awaited_once_with(  # type: ignore[attr-defined]
        hardware_id="1234:5678"
    )


@pytest.mark.asyncio
async def test_macro_exec_complete_forwards_wait_id_and_returncode(daemon_testbed):
    daemon, device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed

    result = await daemon._handle_command(
        CommandType.MACRO_EXEC_COMPLETE,
        {"wait_id": 99, "returncode": "7"},
    )

    assert result == {"completed": True}
    device_manager.complete_macro_exec_wait.assert_called_once_with("99", 7)


@pytest.mark.asyncio
async def test_start_recording_is_refused_when_policy_disallows_recording(daemon_testbed):
    daemon, _device_manager, recording_manager, _macro_store, _capture_manager = daemon_testbed
    daemon.security_policy = SecurityPolicy(macro_recording_allowed=False)

    with pytest.raises(PermissionError, match="macro_recording_disabled"):
        await daemon._handle_command(
            CommandType.START_RECORDING,
            {"devices": [], "recording_slot": 1},
            client=client_context(),
        )

    recording_manager.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_commands_bind_retained_recordings_to_the_client_uid(daemon_testbed):
    daemon, _device_manager, recording_manager, _macro_store, _capture_manager = daemon_testbed
    daemon.security_policy = SecurityPolicy()
    calls: list[tuple[object, ...]] = []

    def bind_owner(uid: int) -> None:
        calls.append(("bind_owner", uid))

    def list_pending_recordings() -> list[object]:
        calls.append(("list_pending_recordings",))
        return []

    recording_manager.bind_owner.side_effect = bind_owner
    recording_manager.list_pending_recordings.side_effect = list_pending_recordings

    result = await daemon._handle_command(
        CommandType.MACRO_LIST_RECORDINGS,
        {},
        client=client_context(uid=1234),
    )

    assert result == {"recordings": []}
    assert calls == [("bind_owner", 1234), ("list_pending_recordings",)]


@pytest.mark.asyncio
async def test_pending_recording_still_plays_when_policy_disallows_recording(daemon_testbed):
    daemon, device_manager, recording_manager, _macro_store, _capture_manager = daemon_testbed
    daemon.security_policy = SecurityPolicy(macro_recording_allowed=False)

    class Snapshot:
        recording_id = "recording-1"
        duration_ms = 5
        device_types = ["keyboard"]
        event_count = 1

        def iter_events(self):
            yield {"type": 1, "code": 30, "value": 1, "t_us": 0}

    recording_manager.claim_pending_recording.return_value = Snapshot()

    result = await daemon._handle_command(
        CommandType.MACRO_PLAY_RECORDING,
        {"pending_recording_id": "recording-1"},
        client=client_context(),
    )

    assert result == {"played": True}
    device_manager.play_macro.assert_awaited_once()


@pytest.mark.asyncio
async def test_client_disconnect_releases_devices_after_recording_discard_fails(daemon_testbed):
    daemon, device_manager, recording_manager, _macro_store, capture_manager = daemon_testbed
    capture_manager.close_all = Mock(return_value=0)
    recording_manager.discard_all_pending_recordings.side_effect = RuntimeError("discard failed")

    await daemon._on_client_disconnect()

    recording_manager.abort.assert_awaited_once()
    recording_manager.discard_all_pending_recordings.assert_awaited_once()
    capture_manager.close_all.assert_called_once()
    device_manager.release_all_devices.assert_awaited_once()
