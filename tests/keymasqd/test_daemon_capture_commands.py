# ruff: noqa: F403, F405, I001
from tests.keymasqd.daemon_support import *

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


@pytest.mark.asyncio
async def test_set_cursor_position_command_forwards_to_device_manager(daemon_testbed):
    daemon, device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    device_manager.set_cursor_position.return_value = {"status": "ok", "x": 123, "y": 456}

    result = await daemon._handle_command(
        CommandType.SET_CURSOR_POSITION,
        {"x": "123", "y": 456},
    )

    assert result == {"status": "ok", "x": 123, "y": 456}
    device_manager.set_cursor_position.assert_awaited_once_with(123, 456)


@pytest.mark.asyncio
async def test_cursor_position_backend_commands_forward_to_device_manager(daemon_testbed):
    daemon, device_manager, _recording_manager, _macro_store, _capture_manager = daemon_testbed
    device_manager.set_cursor_position_backend.return_value = {"status": "ok", "enabled": True}

    backend_result = await daemon._handle_command(
        CommandType.SET_CURSOR_POSITION_BACKEND,
        {"enabled": True},
    )
    result_result = await daemon._handle_command(
        CommandType.SET_CURSOR_POSITION_RESULT,
        {"request_id": "cursor-1", "ok": True, "message": "ok"},
    )

    assert backend_result == {"status": "ok", "enabled": True}
    assert result_result == {"status": "ok", "completed": True}
    device_manager.set_cursor_position_backend.assert_called_once_with(True)
    device_manager.complete_cursor_position_request.assert_called_once_with(
        "cursor-1",
        ok=True,
        message="ok",
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
    capture_combo = AsyncMock(
        return_value={
            "events": [{"evdev": "key_a", "hardware_id": "1234:5678", "source": "kbd"}]
        }
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(daemon_capture_commands, "capture_combo", capture_combo)

    result = await daemon._handle_command(command_type, data)

    if command_type == CommandType.CAPTURE_COMBO:
        assert result == expected_result
        capture_combo.assert_awaited_once_with(daemon, *expected_call)
        monkeypatch.undo()
        return
    assert result == expected_result
    if expected_call is None:
        getattr(capture_manager, manager_method).assert_called_once_with()
    else:
        getattr(capture_manager, manager_method).assert_called_once_with(expected_call)
    monkeypatch.undo()


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

    task = asyncio.create_task(daemon_capture_commands.capture_combo(daemon, {"1234:5678"}, 1.0))

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
    device_manager.start_topology_watcher.assert_awaited_once()
    device_manager.stop_topology_watcher.assert_awaited_once()
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
    monkeypatch.setattr(daemon_capture_commands.asyncio, "wait_for", fake_wait_for)

    event = await daemon_capture_commands.read_capture_combo_event(
        daemon,
        "combo-token",
        notify_event,
        float("inf"),
    )

    assert event == {
        "evdev": "key_a",
        "hardware_id": "1234:5678",
        "source": "kbd",
        "value": 1,
    }
    assert seen_before_wait == {"device": 1, "capture": 1}
