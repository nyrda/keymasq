import os
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from keymasq.common.ipc import CommandType
from keymasq.common.security import SecurityPolicy
from keymasq.keymasqd import daemon as daemon_module
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
async def test_macro_recording_status_uses_requested_uid(daemon_testbed, monkeypatch):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    resolved_uids: list[int] = []

    def fake_resolve_macro_recording_status(uid: int) -> dict[str, object]:
        resolved_uids.append(uid)
        status: dict[str, object] = {"unlocked": True, "source": "persistent", "expires_at": 0}
        return status

    monkeypatch.setattr(
        daemon_module,
        "resolve_macro_recording_status",
        fake_resolve_macro_recording_status,
    )

    result = await daemon._handle_command(
        CommandType.MACRO_RECORDING_STATUS,
        {"uid": 9999},
        client=client_context(uid=1000),
    )

    assert result == {"unlocked": True, "source": "persistent", "expires_at": 0}
    assert resolved_uids == [9999]


@pytest.mark.asyncio
async def test_recording_unlock_status_uses_requested_uid(daemon_testbed, monkeypatch):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    resolved_uids: list[int] = []

    def fake_resolve_unlock_status(uid: int) -> dict[str, object]:
        resolved_uids.append(uid)
        status: dict[str, object] = {"unlocked": True, "source": "runtime", "expires_at": 123}
        return status

    monkeypatch.setattr(
        daemon_module,
        "resolve_unlock_status",
        fake_resolve_unlock_status,
    )

    result = await daemon._handle_command(
        CommandType.RECORDING_UNLOCK_STATUS,
        {"uid": 9999},
        client=client_context(uid=1000),
    )

    assert result == {"unlocked": True, "source": "runtime", "expires_at": 123}
    assert resolved_uids == [9999]


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
async def test_device_inspector_start_requires_recording_unlock(daemon_testbed, monkeypatch):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    daemon.security_policy = SecurityPolicy(recording_unlock_required=True)
    event_loop_thread = threading.get_ident()
    resolver_threads: list[int] = []

    def recording_unlocked_for_uid(_uid: int) -> tuple[bool, int, str]:
        resolver_threads.append(threading.get_ident())
        return False, 0, "none"

    monkeypatch.setattr(daemon, "_recording_unlocked_for_uid", recording_unlocked_for_uid)

    with pytest.raises(PermissionError, match="recording_locked"):
        await daemon._handle_command(
            CommandType.DEVICE_INSPECTOR_START,
            {"hardware_id": "1234:5678"},
            client=client_context(),
        )

    assert len(resolver_threads) == 1
    assert resolver_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_macro_save_recording_requires_recording_unlock(daemon_testbed, monkeypatch):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    daemon.security_policy = SecurityPolicy(recording_unlock_required=True)
    monkeypatch.setattr(daemon, "_recording_unlocked_for_uid", lambda _uid: (False, 0, "none"))

    with pytest.raises(PermissionError, match="recording_locked"):
        await daemon._handle_command(
            CommandType.MACRO_SAVE_RECORDING,
            {"pending_recording_id": "recording-1", "name": "saved"},
            client=client_context(),
        )


@pytest.mark.asyncio
async def test_macro_play_recording_does_not_require_recording_unlock(
    daemon_testbed,
    monkeypatch,
):
    daemon, device_manager, recording_manager, _macro_store, _capture_manager = daemon_testbed
    daemon.security_policy = SecurityPolicy(recording_unlock_required=True)
    monkeypatch.setattr(daemon, "_recording_unlocked_for_uid", lambda _uid: (False, 0, "none"))

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
    recording_manager.claim_pending_recording.assert_awaited_once_with("recording-1")
    recording_manager.release_pending_recording_claim.assert_awaited_once_with(
        "recording-1",
        saved=False,
    )
    device_manager.play_macro.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_end_allows_owner_after_recording_unlock_expires(
    daemon_testbed,
    monkeypatch,
):
    daemon, _device_manager, _recording_manager, _macro_store, capture_manager = daemon_testbed
    daemon.security_policy = SecurityPolicy(recording_unlock_required=True)
    client = client_context(uid=2000, pid=111, connection_id=10)
    unlocked = True

    def fake_recording_unlocked_for_uid(_uid: int):
        return (unlocked, 0, "runtime" if unlocked else "runtime")

    monkeypatch.setattr(daemon, "_recording_unlocked_for_uid", fake_recording_unlocked_for_uid)

    begin = await daemon._handle_command(
        CommandType.CAPTURE_BEGIN,
        {"hardware_id": "1234:5678"},
        client=client,
    )
    assert begin == {"token": "cap-token"}

    unlocked = False
    end = await daemon._handle_command(
        CommandType.CAPTURE_END,
        {"token": "cap-token"},
        client=client,
    )

    assert end == {"ended": True}
    capture_manager.end.assert_called_once_with("cap-token")


@pytest.mark.asyncio
async def test_macro_exec_complete_forwards_wait_id_and_returncode(daemon_testbed):
    daemon, device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed

    result = await daemon._handle_command(
        CommandType.MACRO_EXEC_COMPLETE,
        {"wait_id": 99, "returncode": "7"},
    )

    assert result == {"completed": True}
    device_manager.complete_macro_exec_wait.assert_called_once_with("99", 7)


@pytest.mark.parametrize("recording_unlock_required", [True, False])
@pytest.mark.asyncio
async def test_sensitive_command_owner_mismatch_is_denied(
    daemon_testbed,
    monkeypatch,
    recording_unlock_required: bool,
):
    daemon, _device_manager, _recording_manager, _macro_store, capture_manager = daemon_testbed
    daemon.security_policy = SecurityPolicy(
        recording_unlock_required=recording_unlock_required,
    )
    monkeypatch.setattr(daemon, "_recording_unlocked_for_uid", lambda _uid: (True, 0, "runtime"))

    first_client = client_context(uid=2000, pid=111, connection_id=10)
    second_client = client_context(uid=2000, pid=222, connection_id=11)

    first = await daemon._handle_command(
        CommandType.CAPTURE_BEGIN,
        {"hardware_id": "1234:5678"},
        client=first_client,
    )
    assert first == {"token": "cap-token"}

    with pytest.raises(PermissionError, match="sensitive_command_denied"):
        await daemon._handle_command(
            CommandType.CAPTURE_BEGIN,
            {"hardware_id": "1234:5678"},
            client=second_client,
        )

    capture_manager.begin.assert_called_once_with(
        hardware_id="1234:5678",
        evdev_paths=None,
        evdev_interfaces=None,
        mode="button",
    )


@pytest.mark.parametrize(
    ("command_type", "data"),
    [
        (CommandType.MACRO_GET, {"name": "recorded"}),
        (CommandType.MACRO_CREATE, {"macro": {"name": "recorded"}}),
        (
            CommandType.MACRO_UPDATE,
            {"name": "recorded", "macro": {"name": "recorded"}},
        ),
    ],
)
@pytest.mark.asyncio
async def test_macro_edit_unlock_commands_enforce_active_owner(
    daemon_testbed,
    monkeypatch,
    command_type: CommandType,
    data: dict[str, object],
):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    daemon.security_policy = SecurityPolicy(
        recording_unlock_required=True,
        macro_edit_requires_unlock=True,
    )
    monkeypatch.setattr(daemon, "_recording_unlocked_for_uid", lambda _uid: (True, 0, "runtime"))

    first_client = client_context(uid=2000, pid=111, connection_id=10)
    second_client = client_context(uid=2000, pid=222, connection_id=11)

    await daemon._handle_command(command_type, data, client=first_client)

    with pytest.raises(PermissionError, match="sensitive_command_denied"):
        await daemon._handle_command(command_type, data, client=second_client)


@pytest.mark.asyncio
async def test_refresh_then_lock_runtime_unlock_updates_owner_cache_and_file(
    daemon_testbed,
    monkeypatch,
    tmp_path: Path,
):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    uid = 5555
    client = client_context(uid=uid, pid=600, connection_id=9)
    unlock_file = tmp_path / "recording-unlock"
    unlock_file.write_text("1\n", encoding="utf-8")

    monkeypatch.setattr(
        daemon_module,
        "resolve_unlock_status",
        lambda requested_uid: {"unlocked": requested_uid == uid, "source": "runtime"},
    )
    monkeypatch.setattr(daemon_module, "runtime_unlock_path", lambda _uid: unlock_file)
    write_unlock = Mock(return_value=None)
    monkeypatch.setattr(daemon_module, "write_unlock_expires_at", write_unlock)
    monkeypatch.setattr(daemon_module.time, "time", lambda: 1000)
    monkeypatch.setattr(daemon_module.time, "monotonic", lambda: 500.0)

    refreshed = daemon._refresh_runtime_unlock(uid, 0, client)
    assert refreshed["status"] == "ok"
    assert refreshed["expires_at"] == 1001
    assert daemon._recording_refresh_owners[uid] == (600, 9)
    assert daemon._unlock_cache[uid] == (500.0, True, 1001, "runtime")
    write_unlock.assert_called_once_with(
        unlock_file,
        1001,
        owner_uid=os.geteuid(),
        owner_gid=os.getegid(),
        mode=0o600,
    )

    locked = daemon._lock_runtime_unlock(uid, client)
    assert locked == {"status": "ok", "uid": uid, "source": "runtime", "locked": True}
    assert uid not in daemon._recording_refresh_owners
    assert daemon._unlock_cache[uid] == (500.0, False, 0, "none")
    assert not unlock_file.exists()


def test_runtime_unlock_owner_denials_are_preserved(daemon_testbed):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    uid = 5555
    owner_client = client_context(uid=uid, pid=600, connection_id=9)
    other_client = client_context(uid=uid, pid=700, connection_id=10)
    daemon._recording_refresh_owners[uid] = (600, 9)

    with pytest.raises(
        PermissionError,
        match="recording_refresh_denied: caller is not active session owner",
    ):
        daemon._refresh_runtime_unlock(uid, 60, other_client)

    with pytest.raises(
        PermissionError,
        match="recording_lock_denied: caller is not active session owner",
    ):
        daemon._lock_runtime_unlock(uid, other_client)

    daemon._recording_refresh_owners.pop(uid)
    with pytest.raises(PermissionError, match="recording_lock_denied: no active session owner"):
        daemon._lock_runtime_unlock(uid, owner_client)


def test_expired_unlocked_cache_entry_is_re_resolved(daemon_testbed, monkeypatch):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    uid = 5555
    resolved_uids: list[int] = []

    def fake_resolve_unlock_status(requested_uid: int) -> dict[str, object]:
        resolved_uids.append(requested_uid)
        return {"unlocked": False, "source": "none", "expires_at": 0}

    monkeypatch.setattr(daemon_module, "resolve_unlock_status", fake_resolve_unlock_status)
    monkeypatch.setattr(daemon_module.time, "monotonic", lambda: 500.25)
    monkeypatch.setattr(daemon_module.time, "time", lambda: 1002)

    daemon._unlock_cache[uid] = (500.0, True, 1001, "runtime")

    assert daemon._recording_unlocked_for_uid(uid) == (False, 0, "none")
    assert resolved_uids == [uid]
    assert daemon._unlock_cache[uid] == (500.25, False, 0, "none")


@pytest.mark.asyncio
async def test_stale_unlocked_cache_entry_is_re_resolved_before_sensitive_command(
    daemon_testbed,
    monkeypatch,
):
    daemon, _device_manager, _recording_manager, _macro_store, capture_manager = daemon_testbed
    daemon.security_policy = SecurityPolicy(recording_unlock_required=True)
    daemon._unlock_cache_interval_s = 0.25
    uid = 5555
    now_mono = 500.0
    statuses: list[dict[str, object]] = [
        {"unlocked": True, "source": "runtime", "expires_at": 2000},
        {"unlocked": False, "source": "none", "expires_at": 0},
    ]
    resolved_uids: list[int] = []

    def fake_resolve_unlock_status(requested_uid: int) -> dict[str, object]:
        resolved_uids.append(requested_uid)
        return statuses[min(len(resolved_uids) - 1, len(statuses) - 1)]

    monkeypatch.setattr(daemon_module, "resolve_unlock_status", fake_resolve_unlock_status)
    monkeypatch.setattr(daemon_module.time, "time", lambda: 1000)
    monkeypatch.setattr(daemon_module.time, "monotonic", lambda: now_mono)

    client = client_context(uid=uid, pid=600, connection_id=9)
    result = await daemon._handle_command(
        CommandType.CAPTURE_BEGIN,
        {"hardware_id": "1234:5678"},
        client=client,
    )
    assert result == {"token": "cap-token"}

    now_mono = 500.3
    with pytest.raises(PermissionError, match="recording_locked"):
        await daemon._handle_command(
            CommandType.CAPTURE_READ,
            {"token": "cap-token"},
            client=client,
        )

    assert resolved_uids == [uid, uid]
    capture_manager.read.assert_not_called()


@pytest.mark.asyncio
async def test_client_disconnect_clears_owned_runtime_unlock_only(
    daemon_testbed,
    monkeypatch,
    tmp_path: Path,
):
    daemon, device_manager, recording_manager, _macro_store, capture_manager = daemon_testbed
    owned_uid = 5555
    unrelated_uid = 7777
    client = client_context(uid=owned_uid, pid=600, connection_id=9)
    runtime_dir = tmp_path / "runtime-unlocks"
    runtime_dir.mkdir()
    (runtime_dir / f"recording-unlock-{owned_uid}").write_text("10\n", encoding="utf-8")
    (runtime_dir / f"recording-unlock-{unrelated_uid}").write_text("20\n", encoding="utf-8")
    daemon._recording_refresh_owners[owned_uid] = (600, 9)
    daemon._recording_refresh_owners[unrelated_uid] = (700, 10)

    monkeypatch.setattr(daemon_module, "RECORDING_UNLOCK_RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(
        daemon_module,
        "runtime_unlock_path",
        lambda uid: runtime_dir / f"recording-unlock-{uid}",
    )
    monkeypatch.setattr(daemon_module.time, "monotonic", lambda: 500.0)

    await daemon._on_client_disconnect(client)

    assert daemon._recording_refresh_owners == {unrelated_uid: (700, 10)}
    assert daemon._unlock_cache[owned_uid] == (500.0, False, 0, "none")
    assert unrelated_uid not in daemon._unlock_cache
    assert not (runtime_dir / f"recording-unlock-{owned_uid}").exists()
    assert (runtime_dir / f"recording-unlock-{unrelated_uid}").exists()
    recording_manager.discard_all_pending_recordings.assert_awaited_once()
    capture_manager.end_all.assert_called_once()
    device_manager.release_all_devices.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_runtime_unlock_cleanup_offloads_file_io(
    daemon_testbed,
    monkeypatch,
    tmp_path: Path,
):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    loop = daemon_module.asyncio.get_running_loop()
    executor_calls: list[str] = []
    uid = 5555
    client = client_context(uid=uid, pid=600, connection_id=9)
    runtime_dir = tmp_path / "runtime-unlocks"
    runtime_dir.mkdir()

    class _Loop:
        def run_in_executor(self, executor: object, func):
            executor_calls.append(getattr(func, "__name__", repr(func)))
            future = loop.create_future()
            future.set_result(func())
            return future

    monkeypatch.setattr(daemon_module.asyncio, "get_running_loop", lambda: _Loop())
    monkeypatch.setattr(daemon_module, "RECORDING_UNLOCK_RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(
        daemon_module,
        "runtime_unlock_path",
        lambda requested_uid: runtime_dir / f"recording-unlock-{requested_uid}",
    )

    (runtime_dir / f"recording-unlock-{uid}").write_text("10\n", encoding="utf-8")
    await daemon._clear_all_runtime_unlocks_async(reason="test")

    assert "_runtime_unlock_file_uids" in executor_calls
    assert "unlink" in executor_calls

    executor_calls.clear()
    (runtime_dir / f"recording-unlock-{uid}").write_text("10\n", encoding="utf-8")
    daemon._recording_refresh_owners[uid] = (600, 9)

    await daemon._clear_runtime_unlock_for_client_async(client, reason="test")

    assert executor_calls == ["unlink"]


@pytest.mark.asyncio
async def test_client_disconnect_releases_devices_after_recording_discard_fails(daemon_testbed):
    daemon, device_manager, recording_manager, _macro_store, capture_manager = daemon_testbed
    recording_manager.discard_all_pending_recordings.side_effect = RuntimeError("discard failed")

    await daemon._on_client_disconnect()

    recording_manager.discard_all_pending_recordings.assert_awaited_once()
    capture_manager.end_all.assert_called_once()
    device_manager.release_all_devices.assert_awaited_once()
