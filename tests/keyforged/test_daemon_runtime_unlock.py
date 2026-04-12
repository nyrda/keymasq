# ruff: noqa: F403, F405, I001
from tests.keyforged.daemon_support import *

@pytest.mark.asyncio
async def test_set_diagnostics_forwards_with_type_conversion(daemon_testbed):
    daemon, device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed

    result = await daemon._handle_command(
        CommandType.SET_DIAGNOSTICS,
        {"enabled": 1, "interval": "3.25"},
    )

    assert result == {"status": "ok"}
    device_manager.set_diagnostics.assert_awaited_once_with(True, 3.25)


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
async def test_sensitive_command_owner_mismatch_is_denied(daemon_testbed, monkeypatch):
    daemon, _device_manager, recording_manager, _macro_store, _capture_manager = daemon_testbed
    daemon.security_policy = SecurityPolicy(recording_unlock_required=True)
    monkeypatch.setattr(daemon, "_recording_unlocked_for_uid", lambda _uid: (True, 0, "runtime"))

    first_client = _client(uid=2000, pid=111, connection_id=10)
    second_client = _client(uid=2000, pid=222, connection_id=11)

    first = await daemon._handle_command(CommandType.START_RECORDING, {}, client=first_client)
    assert first == {"recording": "started"}

    with pytest.raises(PermissionError, match="sensitive_command_denied"):
        await daemon._handle_command(CommandType.START_RECORDING, {}, client=second_client)

    recording_manager.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_then_lock_runtime_unlock_updates_owner_cache_and_file(
    daemon_testbed,
    monkeypatch,
    tmp_path: Path,
):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    uid = 5555
    client = _client(uid=uid, pid=600, connection_id=9)
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
    write_unlock.assert_called_once()

    locked = daemon._lock_runtime_unlock(uid, client)
    assert locked == {"status": "ok", "uid": uid, "source": "runtime", "locked": True}
    assert uid not in daemon._recording_refresh_owners
    assert daemon._unlock_cache[uid] == (500.0, False, 0, "none")
    assert not unlock_file.exists()


@pytest.mark.asyncio
async def test_client_disconnect_clears_owned_and_unowned_runtime_unlocks(
    daemon_testbed,
    monkeypatch,
    tmp_path: Path,
):
    daemon, device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    owned_uid = 5555
    stray_uid = 7777
    runtime_dir = tmp_path / "runtime-unlocks"
    runtime_dir.mkdir()
    (runtime_dir / f"recording-unlock-{owned_uid}").write_text("10\n", encoding="utf-8")
    (runtime_dir / f"recording-unlock-{stray_uid}").write_text("20\n", encoding="utf-8")
    daemon._recording_refresh_owners[owned_uid] = (600, 9)

    monkeypatch.setattr(daemon_module, "RECORDING_UNLOCK_RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(
        daemon_module,
        "runtime_unlock_path",
        lambda uid: runtime_dir / f"recording-unlock-{uid}",
    )
    monkeypatch.setattr(daemon_module.time, "monotonic", lambda: 500.0)

    await daemon._on_client_disconnect()

    assert daemon._recording_refresh_owners == {}
    assert daemon._unlock_cache[owned_uid] == (500.0, False, 0, "none")
    assert daemon._unlock_cache[stray_uid] == (500.0, False, 0, "none")
    assert not (runtime_dir / f"recording-unlock-{owned_uid}").exists()
    assert not (runtime_dir / f"recording-unlock-{stray_uid}").exists()
    device_manager.release_all_devices.assert_awaited_once()
