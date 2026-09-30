from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import keymasq.session.manager.recording_capture as recording_capture_module
import keymasq.session.manager.recording_lifecycle as recording_lifecycle_module
from keymasq.common.ipc import CommandType, Response
from keymasq.session.manager.core import SessionManager


def test_owner_disconnect_clears_active_recording_owner() -> None:
    manager = SessionManager()
    writer = object()
    manager.recording_state.active = True
    manager.recording_state.active_owner_writer_id = id(writer)
    manager.recording_state.active_owner_pid = 111
    manager.recording_state.active_owner_uid = 1000

    recording_lifecycle_module.clear_active_recording_owner_if_writer(
        manager,
        writer,  # type: ignore[arg-type]
    )

    assert manager.recording_state.active is True
    assert manager.recording_state.active_owner_writer_id is None
    assert manager.recording_state.active_owner_pid is None
    assert manager.recording_state.active_owner_uid is None


@pytest.mark.asyncio
async def test_start_recording_keeps_pending_slot_when_daemon_start_fails() -> None:
    manager = SessionManager()
    recording_lifecycle_module.begin_pending_macro_save(
        manager,
        {"pending_recording_id": "recording-1", "duration_ms": 10},
        recording_slot=1,
    )
    manager.client.send_command = AsyncMock(
        return_value=Response(status="error", error="macro_recording_disabled: disabled")
    )

    result = await recording_lifecycle_module.start_recording(manager, recording_slot=1)

    assert result == {
        "status": "error",
        "message": "macro_recording_disabled: disabled",
        "error_code": "macro_recording_disabled",
    }
    assert manager.recording_state.pending_slots[1].data["pending_recording_id"] == "recording-1"
    sent_command = manager.client.send_command.await_args.args[0]
    assert sent_command.command == CommandType.START_RECORDING


@pytest.mark.asyncio
async def test_start_recording_clears_replaced_pending_slot_after_success() -> None:
    manager = SessionManager()
    recording_lifecycle_module.begin_pending_macro_save(
        manager,
        {"pending_recording_id": "recording-1", "duration_ms": 10},
        recording_slot=1,
    )
    manager.client.send_command = AsyncMock(
        side_effect=[
            Response(status="ok", data={"devices": []}),
            Response(status="ok", data={"status": "ok"}),
            Response(status="ok", data={"status": "ok"}),
        ]
    )

    result = await recording_lifecycle_module.start_recording(manager, recording_slot=1)

    assert result == {"status": "ok", "recording_slot": 1}
    assert manager.recording_state.pending_slots == {}
    assert manager.recording_state.active is True
    assert manager.recording_state.active_slot == 1
    sent_commands = [call.args[0] for call in manager.client.send_command.await_args_list]
    assert [command.command for command in sent_commands] == [
        CommandType.LIST_DEVICES,
        CommandType.START_RECORDING,
        CommandType.MACRO_DELETE_RECORDING,
    ]
    assert sent_commands[2].data == {"pending_recording_id": "recording-1"}


@pytest.mark.asyncio
async def test_capture_combo_uses_all_known_hardware_ids_not_just_profile_layers() -> None:
    manager = SessionManager()
    manager.hardware.list_hardware_ids = lambda: ["1234:5678", "9999:0001"]  # type: ignore[assignment]
    manager.profiles.get_profile = lambda _name: SimpleNamespace(  # type: ignore[assignment]
        config=SimpleNamespace(device_layers={"1234:5678": object()}, combos=[])
    )
    manager.client.send_command = AsyncMock(
        return_value=Response(
            status="ok",
            data={
                "events": [
                    {"evdev": "alt", "hardware_id": "9999:0001", "source": "kbd-left"},
                    {"evdev": "key_7", "hardware_id": "1234:5678", "source": "kbd-right"},
                ],
                "warnings": [],
            },
        )
    )

    result = await recording_capture_module.capture_combo(manager, "Work", 15.0)

    assert result == {
        "status": "ok",
        "events": [
            {"evdev": "alt", "hardware_id": "9999:0001", "source": "kbd-left"},
            {"evdev": "key_7", "hardware_id": "1234:5678", "source": "kbd-right"},
        ],
        "warnings": [],
    }
    manager.client.send_command.assert_awaited_once()
    sent_command = manager.client.send_command.await_args.args[0]
    assert sent_command.data["hardware_ids"] == ["1234:5678", "9999:0001"]
