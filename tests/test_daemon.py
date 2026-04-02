from __future__ import annotations

import asyncio
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import keyforge.keyforged.daemon as daemon_module
from keyforge.common.ipc import CommandType
from keyforge.common.security import SecurityPolicy
from keyforge.keyforged.socket_server import ClientContext


@pytest.fixture
def daemon_testbed(monkeypatch):
    device_manager = SimpleNamespace(
        grab_device=AsyncMock(return_value={"grabbed": True}),
        release_device=AsyncMock(return_value={"released": True}),
        set_mapping=AsyncMock(return_value={"updated": True}),
        set_combos=AsyncMock(return_value={"updated": True, "combo_count": 0}),
        list_devices=AsyncMock(return_value={"devices": []}),
        begin_combo_capture=Mock(return_value={"token": "combo-token"}),
        read_combo_capture=Mock(return_value={"event": None}),
        end_combo_capture=Mock(return_value={"status": "ok", "ended": True}),
        grabbed_devices={},
        play_macro=AsyncMock(return_value={"played": True}),
        cancel_macro_playback=AsyncMock(return_value={"canceled": True}),
        set_diagnostics=AsyncMock(return_value={"status": "ok"}),
        complete_macro_exec_wait=Mock(return_value={"completed": True}),
        release_all_devices=AsyncMock(return_value=None),
    )
    recording_manager = SimpleNamespace(
        start=AsyncMock(return_value={"recording": "started"}),
        stop=AsyncMock(return_value={"recording": "stopped"}),
    )
    macro_store = SimpleNamespace(
        get=Mock(return_value={"events": []}),
        list_meta=Mock(return_value=[]),
        create=Mock(return_value={"name": "new"}),
        update=Mock(return_value={"name": "updated"}),
        rename=Mock(return_value={"name": "renamed"}),
        delete=Mock(return_value=None),
        ensure=Mock(return_value=None),
        register_internal=Mock(return_value=None),
    )
    capture_manager = SimpleNamespace(
        begin=Mock(return_value={"token": "cap-token"}),
        read=Mock(return_value={"captured": None}),
        end=Mock(return_value={"ended": True}),
        _authorize_combo_capture=Mock(return_value=object()),
        begin_combo=Mock(return_value={"token": "combo-token", "warnings": []}),
        read_combo=Mock(return_value={"event": None}),
        read_combo_nowait=Mock(return_value={"event": None}),
        register_combo_notifier=Mock(return_value=None),
    )

    monkeypatch.setattr(daemon_module, "DeviceManager", lambda verbosity=0: device_manager)
    monkeypatch.setattr(daemon_module, "RecordingManager", lambda: recording_manager)
    monkeypatch.setattr(daemon_module, "MacroStore", lambda _path: macro_store)
    monkeypatch.setattr(daemon_module, "CaptureManager", lambda: capture_manager)

    daemon = daemon_module.Daemon()
    return daemon, device_manager, recording_manager, macro_store, capture_manager


def _client(*, uid: int = 1000, pid: int = 4321, connection_id: int = 77) -> ClientContext:
    return ClientContext(
        connection_id=connection_id,
        pid=pid,
        uid=uid,
        gid=uid,
        client_class="session",
    )


@pytest.mark.asyncio
async def test_macro_play_by_name_loads_store_and_forwards_runtime_options(daemon_testbed):
    daemon, device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed
    macro_store.get.return_value = {
        "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
        "loop_mode": "count",
        "loop_count": 3,
        "move_to_start": True,
        "start_x": 111,
        "start_y": 222,
        "block_mouse_movement": True,
    }

    result = await daemon._handle_command(
        CommandType.MACRO_PLAY_BY_NAME,
        {
            "name": "combo",
            "speed": "2.5",
            "replay_mouse_movement": False,
            "replay_mouse_clicks": True,
        },
    )

    assert result == {"played": True}
    macro_store.get.assert_called_once_with("combo")
    device_manager.play_macro.assert_awaited_once_with(
        macro_events=[{"type": 1, "code": 30, "value": 1, "t_us": 0}],
        macro_name="combo",
        replay_mouse_movement=False,
        replay_mouse_clicks=True,
        speed=2.5,
        loop_mode="count",
        loop_count=3,
        move_to_start=True,
        start_x=111,
        start_y=222,
        block_mouse_movement=True,
    )


@pytest.mark.parametrize(
    ("command_type", "data", "manager_method", "expected_call", "expected_result"),
    [
        (
            CommandType.CAPTURE_BEGIN,
            {"hardware_id": 1234},
            "begin",
            "1234",
            {"token": "cap-token"},
        ),
        (
            CommandType.CAPTURE_READ,
            {"token": 42},
            "read",
            "42",
            {"captured": None},
        ),
        (
            CommandType.CAPTURE_END,
            {"token": 42},
            "end",
            "42",
            {"ended": True},
        ),
        (
            CommandType.CAPTURE_COMBO,
            {"hardware_ids": ["1234:5678"], "timeout_s": 9.0},
            "_capture_combo",
            ({"1234:5678"}, 9.0),
            {"events": [{"evdev": "key_a", "hardware_id": "1234:5678", "source": "kbd"}]},
        ),
    ],
)
@pytest.mark.asyncio
async def test_capture_commands_forward_to_capture_manager(
    daemon_testbed,
    command_type: CommandType,
    data: dict,
    manager_method: str,
    expected_call,
    expected_result: dict,
):
    daemon, _device_manager, _recording_manager, _macro_store, capture_manager = daemon_testbed
    daemon._capture_combo = AsyncMock(
        return_value={
            "events": [{"evdev": "key_a", "hardware_id": "1234:5678", "source": "kbd"}]
        }
    )  # type: ignore[method-assign]

    result = await daemon._handle_command(command_type, data)

    if command_type == CommandType.CAPTURE_COMBO:
        assert result == expected_result
        daemon._capture_combo.assert_awaited_once_with(*expected_call)
        return
    assert result == expected_result
    if expected_call is None:
        getattr(capture_manager, manager_method).assert_called_once_with()
    else:
        getattr(capture_manager, manager_method).assert_called_once_with(expected_call)


@pytest.mark.asyncio
async def test_capture_combo_waits_on_event_not_sleep(daemon_testbed, monkeypatch):
    daemon, device_manager, _recording_manager, _macro_store, capture_manager = daemon_testbed
    original_sleep = asyncio.sleep
    queued_events: list[dict] = []
    waiter: dict[str, asyncio.Event] = {}

    def begin_combo_capture(
        _token: str,
        _hardware_ids: set[str],
        notify_event: asyncio.Event,
    ) -> dict:
        waiter["event"] = notify_event
        return {"token": "combo-token", "grabbed_devices": 0}

    device_manager.begin_combo_capture = Mock(side_effect=begin_combo_capture)
    device_manager.read_combo_capture = Mock(
        side_effect=lambda _token: {"event": queued_events.pop(0) if queued_events else None}
    )
    capture_manager.read_combo_nowait = Mock(return_value={"event": None})

    async def fail_sleep(delay: float) -> None:
        raise AssertionError(f"unexpected polling sleep: {delay}")

    monkeypatch.setattr(daemon_module.asyncio, "sleep", fail_sleep)

    task = asyncio.create_task(daemon._capture_combo({"1234:5678"}, 1.0))

    while "event" not in waiter:
        await original_sleep(0)

    queued_events.append(
        {"evdev": "key_a", "hardware_id": "1234:5678", "source": "kbd", "value": 1}
    )
    waiter["event"].set()
    await original_sleep(0)
    queued_events.append(
        {"evdev": "key_a", "hardware_id": "1234:5678", "source": "kbd", "value": 0}
    )
    waiter["event"].set()

    result = await task
    assert result == {
        "events": [{"evdev": "key_a", "hardware_id": "1234:5678", "source": "kbd"}],
        "warnings": [],
    }
    capture_manager.register_combo_notifier.assert_called_once()


@pytest.mark.asyncio
async def test_start_offloads_macro_store_prep_to_thread(
    daemon_testbed,
    monkeypatch,
    tmp_path: Path,
):
    daemon, device_manager, recording_manager, macro_store, _capture_manager = daemon_testbed
    to_thread_calls: list[tuple[object, tuple[object, ...]]] = []
    fake_socket_server = SimpleNamespace(
        start=AsyncMock(side_effect=lambda: daemon._shutdown_event.set()),
        stop=AsyncMock(),
        broadcast_event=AsyncMock(),
    )

    async def fake_to_thread(func, /, *args, **kwargs):
        assert kwargs == {}
        to_thread_calls.append((func, args))
        return func(*args)

    monkeypatch.setattr(daemon_module.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(daemon_module, "SocketServer", lambda *args, **kwargs: fake_socket_server)
    monkeypatch.setattr(daemon_module, "RUN_DIR", tmp_path / "run")
    monkeypatch.setattr(daemon_module, "SOCKET_PATH", tmp_path / "daemon.sock")
    monkeypatch.setattr(daemon_module, "load_security_policy", lambda _path: SecurityPolicy())
    monkeypatch.setattr(daemon_module, "sd_notify", lambda _state: None)
    monkeypatch.setattr(daemon, "_secure_run_dir", Mock())

    await daemon.start()

    assert to_thread_calls[0][0].__name__ == "_prepare_macro_store"
    macro_store.ensure.assert_called_once()
    macro_store.register_internal.assert_called_once()
    assert device_manager.broadcast_callback == fake_socket_server.broadcast_event
    assert recording_manager.broadcast_callback == fake_socket_server.broadcast_event


@pytest.mark.asyncio
async def test_read_capture_combo_event_drains_sources_once_before_waiting(
    daemon_testbed,
    monkeypatch,
):
    daemon, device_manager, _recording_manager, _macro_store, capture_manager = daemon_testbed
    notify_event = asyncio.Event()
    seen_before_wait: dict[str, int] = {}
    released = {"ready": False}

    def read_combo_capture(_token: str) -> dict:
        if released["ready"]:
            return {
                "event": {
                    "evdev": "key_a",
                    "hardware_id": "1234:5678",
                    "source": "kbd",
                    "value": 1,
                }
            }
        return {"event": None}

    async def fake_wait_for(awaitable, timeout):
        seen_before_wait["device"] = device_manager.read_combo_capture.call_count
        seen_before_wait["capture"] = capture_manager.read_combo_nowait.call_count
        released["ready"] = True
        notify_event.set()
        return await awaitable

    device_manager.read_combo_capture = Mock(side_effect=read_combo_capture)
    capture_manager.read_combo_nowait = Mock(return_value={"event": None})
    monkeypatch.setattr(daemon_module.asyncio, "wait_for", fake_wait_for)

    event = await daemon._read_capture_combo_event("combo-token", notify_event, float("inf"))

    assert event == {
        "evdev": "key_a",
        "hardware_id": "1234:5678",
        "source": "kbd",
        "value": 1,
    }
    assert seen_before_wait == {"device": 1, "capture": 1}


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


@pytest.mark.asyncio
async def test_refresh_and_lock_commands_require_client_context(daemon_testbed):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed

    with pytest.raises(PermissionError, match="missing client context"):
        await daemon._handle_command(CommandType.REFRESH_RECORDING_UNLOCK, {"uid": 10})

    with pytest.raises(PermissionError, match="missing client context"):
        await daemon._handle_command(CommandType.LOCK_RECORDING_UNLOCK, {"uid": 10})


def test_signal_handler_only_sets_shutdown_event(daemon_testbed):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    daemon.running = True

    daemon._signal_handler()

    assert daemon.running is True
    assert daemon._shutdown_event.is_set()


@pytest.mark.asyncio
async def test_unknown_command_raises_value_error(daemon_testbed):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed

    class FakeCommand(Enum):
        UNKNOWN = "unknown"

    with pytest.raises(ValueError, match="Unknown command"):
        await daemon._handle_command(FakeCommand.UNKNOWN, {})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("expected_allowed",),
    [
        (True,),
        (False,),
    ],
)
def test_validate_peer_behavior(daemon_testbed, expected_allowed: bool):
    daemon, *_rest = daemon_testbed
    daemon.security_policy = SecurityPolicy(
        daemon_allowed_uids=[1111],
        recording_unlock_required=True,
    )

    peer = SimpleNamespace(uid=1111 if expected_allowed else 2222, pid=1, gid=1)
    allowed, peer_class, reason = daemon._validate_peer(peer)

    if expected_allowed:
        assert allowed is True
        assert peer_class == "session"
        assert reason == "peer uid allowed"
    else:
        assert allowed is False
        assert peer_class == "unknown"
        assert "not allowed" in reason


class _BadRunDir:
    def __init__(self, path: str) -> None:
        self.path = path

    def __fspath__(self) -> str:
        return self.path

    def __str__(self) -> str:
        return self.path

    def chmod(self, _mode: int) -> None:
        return None

    def stat(self):
        return SimpleNamespace(st_mode=0o40777)


def test_secure_run_dir_rejects_insecure_permissions(
    daemon_testbed,
    monkeypatch,
    tmp_path: Path,
):
    daemon, *_rest = daemon_testbed
    bad_dir = tmp_path / "bad-run"
    bad_dir.mkdir()

    monkeypatch.setattr(daemon_module.os, "chmod", lambda _path, _mode: None)
    monkeypatch.setattr(daemon_module, "RUN_DIR", _BadRunDir(str(bad_dir)))

    with pytest.raises(RuntimeError, match="Insecure run directory permissions"):
        daemon._secure_run_dir()


@pytest.mark.asyncio
async def test_resolve_mapping_macros_loads_macro_definition(daemon_testbed):
    daemon, _device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed

    macro_store.get.return_value = {
        "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
        "loop_mode": "count",
        "loop_count": 3,
        "move_to_start": True,
        "start_x": 111,
        "start_y": 222,
        "block_mouse_movement": True,
    }

    resolved = await daemon._resolve_mapping_macros(
        {
            "btn_side": {
                "action": "macro",
                "macro_name": "combo",
            }
        }
    )

    action = resolved["btn_side"]
    assert action["macro_events"] == [{"type": 1, "code": 30, "value": 1, "t_us": 0}]
    assert action["macro_loop_mode"] == "count"
    assert action["macro_loop_count"] == 3
    assert action["macro_move_to_start"] is True
    assert action["macro_start_x"] == 111
    assert action["macro_start_y"] == 222
    assert action["macro_block_mouse_movement"] is True


@pytest.mark.asyncio
async def test_resolve_mapping_macros_deduplicates_macro_store_reads(daemon_testbed):
    daemon, _device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed

    macro_store.get.side_effect = lambda name: {
        "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
        "loop_mode": "count",
        "loop_count": 3 if name == "combo" else 2,
        "move_to_start": False,
        "start_x": 0,
        "start_y": 0,
        "block_mouse_movement": False,
    }

    resolved = await daemon._resolve_mapping_macros(
        {
            "btn_side": {"action": "macro", "macro_name": "combo"},
            "btn_extra": {"action": "macro", "macro_name": "combo"},
            "btn_middle": {"action": "macro", "macro_name": "other"},
        }
    )

    assert resolved["btn_side"]["macro_loop_count"] == 3
    assert resolved["btn_extra"]["macro_loop_count"] == 3
    assert resolved["btn_middle"]["macro_loop_count"] == 2
    assert macro_store.get.call_count == 2


@pytest.mark.asyncio
async def test_resolve_mapping_macros_ignores_malformed_stored_macro_values(daemon_testbed):
    daemon, _device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed

    macro_store.get.return_value = {
        "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
        "loop_mode": "count",
        "loop_count": "",
        "move_to_start": True,
        "start_x": "abc",
        "start_y": 0,
        "block_mouse_movement": False,
    }

    resolved = await daemon._resolve_mapping_macros(
        {
            "btn_side": {"action": "macro", "macro_name": "broken"},
            "btn_middle": {"action": "keyboard", "target": "key_a"},
        }
    )

    assert resolved["btn_side"] == {"action": "macro", "macro_name": "broken"}
    assert resolved["btn_middle"] == {"action": "keyboard", "target": "key_a"}


@pytest.mark.asyncio
async def test_handle_command_set_mapping_resolves_macro_values(daemon_testbed):
    daemon, device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed
    daemon.security_policy = SecurityPolicy(recording_unlock_required=False)
    macro_store.get.return_value = {
        "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
        "loop_mode": "count",
        "loop_count": 2,
        "move_to_start": False,
        "start_x": 4,
        "start_y": 5,
        "block_mouse_movement": False,
    }

    await daemon._handle_command(
        CommandType.SET_MAPPING,
        {
            "hardware_id": "123",
            "mapping": {"btn_side": {"action": "macro", "macro_name": "combo"}},
        },
    )

    sent_mapping = device_manager.set_mapping.await_args.kwargs["mapping"]
    resolved = sent_mapping["btn_side"]
    assert resolved["macro_events"] == [{"type": 1, "code": 30, "value": 1, "t_us": 0}]
    assert resolved["macro_loop_mode"] == "count"
    assert resolved["macro_loop_count"] == 2


@pytest.mark.asyncio
async def test_handle_command_set_combos_resolves_macro_values(daemon_testbed):
    daemon, device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed
    daemon.security_policy = SecurityPolicy(recording_unlock_required=False)
    macro_store.get.return_value = {
        "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
        "loop_mode": "count",
        "loop_count": 4,
        "move_to_start": True,
        "start_x": 7,
        "start_y": 8,
        "block_mouse_movement": True,
    }

    await daemon._handle_command(
        CommandType.SET_COMBOS,
        {
            "combos": [
                {
                    "id": "combo-1",
                    "name": "Combo",
                    "steps": [
                        {
                            "events": [
                                {
                                    "hardware_id": "1234:5678",
                                    "source": "mouse",
                                    "evdev": "btn_side",
                                }
                            ]
                        }
                    ],
                    "action": {"action": "macro", "macro_name": "combo"},
                }
            ]
        },
    )

    sent_combos = device_manager.set_combos.await_args.args[0]
    assert sent_combos[0]["action"]["macro_events"] == [
        {"type": 1, "code": 30, "value": 1, "t_us": 0}
    ]
    assert sent_combos[0]["action"]["macro_loop_mode"] == "count"
    assert sent_combos[0]["action"]["macro_loop_count"] == 4


@pytest.mark.asyncio
async def test_resolve_combo_macros_deduplicates_macro_store_reads(daemon_testbed):
    daemon, _device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed

    macro_store.get.side_effect = lambda name: {
        "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
        "loop_mode": "count",
        "loop_count": 4 if name == "combo" else 1,
        "move_to_start": False,
        "start_x": 0,
        "start_y": 0,
        "block_mouse_movement": False,
    }

    resolved = await daemon._resolve_combo_macros(
        [
            {
                "id": "combo-1",
                "name": "First",
                "steps": [],
                "action": {"action": "macro", "macro_name": "combo"},
            },
            {
                "id": "combo-2",
                "name": "Second",
                "steps": [],
                "action": {"action": "macro", "macro_name": "combo"},
            },
            {
                "id": "combo-3",
                "name": "Third",
                "steps": [],
                "action": {"action": "macro", "macro_name": "other"},
            },
        ]
    )

    assert resolved[0]["action"]["macro_loop_count"] == 4
    assert resolved[1]["action"]["macro_loop_count"] == 4
    assert resolved[2]["action"]["macro_loop_count"] == 1
    assert macro_store.get.call_count == 2


@pytest.mark.asyncio
async def test_resolve_combo_macros_ignores_malformed_stored_macro_values(daemon_testbed):
    daemon, _device_manager, _recording_manager, macro_store, _capture_manager = daemon_testbed

    macro_store.get.return_value = {
        "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
        "loop_mode": "count",
        "loop_count": "",
        "move_to_start": True,
        "start_x": "abc",
        "start_y": 0,
        "block_mouse_movement": False,
    }

    resolved = await daemon._resolve_combo_macros(
        [
            {
                "id": "combo-1",
                "name": "Broken",
                "steps": [],
                "action": {"action": "macro", "macro_name": "broken"},
            },
            {
                "id": "combo-2",
                "name": "Keyboard",
                "steps": [],
                "action": {"action": "keyboard", "target": "key_f5"},
            },
        ]
    )

    assert resolved[0]["action"] == {"action": "macro", "macro_name": "broken"}
    assert resolved[1]["action"] == {"action": "keyboard", "target": "key_f5"}


@pytest.mark.asyncio
async def test_handle_command_start_recording_respects_runtime_lock(daemon_testbed, monkeypatch):
    daemon, _device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    daemon.security_policy = SecurityPolicy(recording_unlock_required=True)

    monkeypatch.setattr(
        daemon_module,
        "resolve_unlock_status",
        lambda _uid: {"unlocked": False, "source": "none", "expires_at": 0},
    )

    with pytest.raises(PermissionError, match="recording_locked"):
        await daemon._handle_command(
            CommandType.START_RECORDING,
            {},
            client=_client(uid=1000, pid=111, connection_id=7),
        )
