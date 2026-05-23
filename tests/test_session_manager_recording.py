from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import keymasq.session.manager.recording as session_recording_module
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


def test_owner_disconnect_discards_pending_macro_save() -> None:
    manager = SessionManager()
    writer = object()
    manager.recording_state.pending_data = {"events": [{"t_us": 0}]}
    manager.recording_state.pending_save_token = "pending-1"
    manager.recording_state.pending_save_owner_writer_id = id(writer)

    session_recording_module.clear_pending_macro_save_if_writer(
        manager,
        writer,  # type: ignore[arg-type]
    )

    assert manager.recording_state.pending_data is None
    assert manager.recording_state.pending_save_token is None
    assert manager.recording_state.pending_save_owner_writer_id is None


def test_owner_disconnect_leaves_ownerless_pending_macro_save() -> None:
    manager = SessionManager()
    writer = object()
    manager.recording_state.pending_data = {"events": [{"t_us": 0}]}
    manager.recording_state.pending_save_token = "pending-1"

    session_recording_module.clear_pending_macro_save_if_writer(
        manager,
        writer,  # type: ignore[arg-type]
    )

    assert manager.recording_state.pending_data == {"events": [{"t_us": 0}]}
    assert manager.recording_state.pending_save_token == "pending-1"
    assert manager.recording_state.pending_save_owner_writer_id is None


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
async def test_last_client_disconnect_cleans_runtime_unlock_without_owner() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=222, uid=1000, gid=1000)
    writer = object()
    manager.session_clients.add(writer)  # type: ignore[arg-type]
    manager.session_client_peers[writer] = peer  # type: ignore[index]
    resolve_unlock_status_async = AsyncMock(
        return_value={"unlocked": True, "source": "runtime", "expires_at": 2000}
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        session_recording_module,
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
    monkeypatch.undo()


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
async def test_claim_recording_unlock_refresh_creates_runtime_lease() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=12, uid=101, gid=100)
    writer = object()
    resolve_unlock_status_async = AsyncMock(
        side_effect=[
            {"unlocked": True, "source": "runtime", "expires_at": 2000},
            {"unlocked": True, "source": "runtime", "expires_at": 2000},
        ]
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        session_recording_module,
        "resolve_unlock_status_async",
        resolve_unlock_status_async,
    )

    manager.client.send_command = AsyncMock(return_value=Response(status="ok"))

    result = await session_recording_module.claim_recording_unlock_refresh(manager, peer, writer)

    assert result["status"] == "ok"
    assert result["recording_refresh_owner"] is True
    assert manager.unlock_state.refresh_owner is not None
    assert manager.unlock_state.refresh_owner["uid"] == peer.uid
    assert manager.unlock_state.runtime_refresh_claim_consumed_until[peer.uid] == 2000
    assert resolve_unlock_status_async.await_count == 2
    manager.client.send_command.assert_awaited_once()
    monkeypatch.undo()


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

    monkeypatch.setattr(session_recording_module, "resolve_unlock_status", resolve)

    result = await session_recording_module.claim_recording_unlock_refresh(manager, peer, writer)

    assert result == {
        "status": "error",
        "error_code": "recording_refresh_reclaim_denied",
        "message": (
            "recording_refresh_denied: runtime lease already claimed; "
            "unlock again to re-establish owner"
        ),
    }
