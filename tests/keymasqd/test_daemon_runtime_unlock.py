# ruff: noqa: F403, F405, I001
from tests.keymasqd.daemon_support import *

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
async def test_device_inspector_start_requires_recording_unlock(daemon_testbed, monkeypatch):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    daemon.security_policy = SecurityPolicy(recording_unlock_required=True)
    monkeypatch.setattr(daemon, "_recording_unlocked_for_uid", lambda _uid: (False, 0, "none"))

    with pytest.raises(PermissionError, match="recording_locked"):
        await daemon._handle_command(
            CommandType.DEVICE_INSPECTOR_START,
            {"hardware_id": "1234:5678"},
            client=_client(),
        )


@pytest.mark.asyncio
async def test_capture_end_allows_owner_after_recording_unlock_expires(
    daemon_testbed,
    monkeypatch,
):
    daemon, _device_manager, _recording_manager, _macro_store, capture_manager = daemon_testbed
    daemon.security_policy = SecurityPolicy(recording_unlock_required=True)
    client = _client(uid=2000, pid=111, connection_id=10)
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
async def test_client_disconnect_clears_owned_and_unowned_runtime_unlocks(
    daemon_testbed,
    monkeypatch,
    tmp_path: Path,
):
    daemon, device_manager, recording_manager, _macro_store, _capture_manager = daemon_testbed
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
    recording_manager.discard_all_pending_recordings.assert_awaited_once()
    device_manager.release_all_devices.assert_awaited_once()
