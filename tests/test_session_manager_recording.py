from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import keymasq.session.manager.recording as session_recording_module
import keymasq.session.manager.recording_unlock as recording_unlock_module
from keymasq.common.ipc import Command, CommandType, Response
from keymasq.common.security import PeerCredentials
from keymasq.session.manager import SessionManager


@pytest.mark.asyncio
async def test_owner_disconnect_cleans_runtime_unlock() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=111, uid=1000, gid=1000)
    writer = object()
    manager.unlock_state.refresh_owner = {
        "uid": 1000,
        "pid": 111,
        "writer_id": id(writer),
        "lease_id": "lease-1",
        "source": "runtime",
    }
    manager.client.send_command = AsyncMock(return_value=Response(status="ok"))

    await session_recording_module.clear_recording_refresh_owner_if_writer(
        manager,
        peer,
        writer,  # type: ignore[arg-type]
    )

    assert manager.unlock_state.refresh_owner is None
    manager.client.send_command.assert_awaited_once_with(
        Command(
            command=CommandType.LOCK_RECORDING_UNLOCK,
            data={"uid": 1000, "cleanup": True},
        )
    )


@pytest.mark.asyncio
async def test_owner_disconnect_clears_persistent_owner_without_daemon_cleanup() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=111, uid=1000, gid=1000)
    writer = object()
    owner = {
        "uid": 1000,
        "pid": 111,
        "writer_id": id(writer),
        "lease_id": "lease-1",
        "source": "persistent",
    }
    manager.unlock_state.refresh_owner = owner
    manager.client.send_command = AsyncMock()

    await session_recording_module.clear_recording_refresh_owner_if_writer(
        manager,
        peer,
        writer,  # type: ignore[arg-type]
    )

    assert manager.unlock_state.refresh_owner is None
    manager.client.send_command.assert_not_awaited()


def test_owner_disconnect_clears_active_recording_owner() -> None:
    manager = SessionManager()
    writer = object()
    manager.recording_state.active = True
    manager.recording_state.active_owner_writer_id = id(writer)
    manager.recording_state.active_owner_pid = 111
    manager.recording_state.active_owner_uid = 1000

    session_recording_module.clear_active_recording_owner_if_writer(
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
    session_recording_module.begin_pending_macro_save(
        manager,
        {"pending_recording_id": "recording-1", "duration_ms": 10},
        recording_slot=1,
    )
    manager.client.send_command = AsyncMock(
        return_value=Response(status="error", error="recording_locked")
    )

    result = await session_recording_module.start_recording(manager, recording_slot=1)

    assert result == {
        "status": "error",
        "message": "recording_locked",
        "error_code": "recording_locked",
    }
    assert manager.recording_state.pending_slots[1].data["pending_recording_id"] == "recording-1"
    sent_command = manager.client.send_command.await_args.args[0]
    assert sent_command.command == CommandType.START_RECORDING


@pytest.mark.asyncio
async def test_start_recording_clears_replaced_pending_slot_after_success() -> None:
    manager = SessionManager()
    session_recording_module.begin_pending_macro_save(
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

    result = await session_recording_module.start_recording(manager, recording_slot=1)

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
async def test_last_client_disconnect_cleans_runtime_unlock_without_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=222, uid=1000, gid=1000)
    writer = object()
    manager.session_clients.add(writer)  # type: ignore[arg-type]
    manager.session_client_peers[writer] = peer  # type: ignore[index]
    resolve_unlock_status_async = AsyncMock(
        return_value={"unlocked": True, "source": "runtime", "expires_at": 2000}
    )
    monkeypatch.setattr(
        recording_unlock_module,
        "resolve_unlock_status_async",
        resolve_unlock_status_async,
    )
    manager.client.send_command = AsyncMock(return_value=Response(status="ok"))

    await session_recording_module.clear_recording_refresh_owner_if_writer(
        manager,
        peer,
        writer,  # type: ignore[arg-type]
    )

    manager.client.send_command.assert_awaited_once_with(
        Command(
            command=CommandType.LOCK_RECORDING_UNLOCK,
            data={"uid": 1000, "cleanup": True},
        )
    )


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

    result = await session_recording_module.capture_combo(manager, "Work", 15.0)

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


@pytest.mark.asyncio
async def test_claim_recording_unlock_refresh_creates_runtime_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=12, uid=101, gid=100)
    writer = object()
    resolve_unlock_status_async = AsyncMock(
        side_effect=[
            {"unlocked": True, "source": "runtime", "expires_at": 2000},
            {"unlocked": True, "source": "runtime", "expires_at": 2000},
        ]
    )
    monkeypatch.setattr(
        recording_unlock_module,
        "resolve_unlock_status_async",
        resolve_unlock_status_async,
    )

    manager.client.send_command = AsyncMock(return_value=Response(status="ok"))

    result = await session_recording_module.claim_recording_unlock_refresh(manager, peer, writer)

    assert result["status"] == "ok"
    assert result["recording_refresh_owner"] is True
    assert manager.unlock_state.refresh_owner is not None
    assert manager.unlock_state.refresh_owner["uid"] == peer.uid
    assert manager.unlock_state.refresh_owner["source"] == "runtime"
    assert manager.unlock_state.runtime_refresh_claim_consumed_until[peer.uid] == 2000
    assert resolve_unlock_status_async.await_count == 2
    manager.client.send_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_claim_recording_unlock_refresh_blocks_reclaimed_runtime_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=12, uid=202, gid=100)
    writer = object()
    manager.unlock_state.runtime_refresh_claim_consumed_until[peer.uid] = 5000

    def resolve(_uid: int) -> dict:
        return {"unlocked": True, "source": "runtime", "expires_at": 5000}

    monkeypatch.setattr(recording_unlock_module, "resolve_unlock_status", resolve)

    result = await session_recording_module.claim_recording_unlock_refresh(manager, peer, writer)

    assert result == {
        "status": "error",
        "error_code": "recording_refresh_reclaim_denied",
        "message": (
            "recording_refresh_denied: runtime lease already claimed; "
            "unlock again to re-establish owner"
        ),
    }


@pytest.mark.asyncio
async def test_refresh_recording_unlock_clears_owner_when_daemon_rejects_lease() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=12, uid=303, gid=100)
    writer = object()
    manager.unlock_state.refresh_owner = {
        "uid": peer.uid,
        "pid": peer.pid,
        "writer_id": id(writer),
        "lease_id": "lease-1",
        "source": "runtime",
    }
    manager.client.send_command = AsyncMock(
        return_value=Response(
            status="error",
            error="recording_refresh_denied: runtime unlock lease is not active",
        )
    )

    result = await session_recording_module.refresh_recording_unlock(
        manager,
        peer,
        writer,  # type: ignore[arg-type]
        "lease-1",
    )

    assert result["status"] == "error"
    assert result["error_code"] == "recording_refresh_denied"
    assert manager.unlock_state.refresh_owner is None


@pytest.mark.asyncio
async def test_refresh_recording_unlock_skips_daemon_for_persistent_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=12, uid=404, gid=100)
    writer = object()
    manager.unlock_state.refresh_owner = {
        "uid": peer.uid,
        "pid": peer.pid,
        "writer_id": id(writer),
        "lease_id": "lease-1",
        "source": "persistent",
    }
    resolve_unlock_status_async = AsyncMock(
        return_value={"unlocked": True, "source": "persistent", "expires_at": 0}
    )
    monkeypatch.setattr(
        recording_unlock_module,
        "resolve_unlock_status_async",
        resolve_unlock_status_async,
    )
    manager.client.send_command = AsyncMock()

    result = await session_recording_module.refresh_recording_unlock(
        manager,
        peer,
        writer,  # type: ignore[arg-type]
        "lease-1",
    )

    assert result["status"] == "ok"
    assert result["recording_refresh_owner"] is True
    manager.client.send_command.assert_not_awaited()
    resolve_unlock_status_async.assert_awaited_once_with(manager, peer.uid)
