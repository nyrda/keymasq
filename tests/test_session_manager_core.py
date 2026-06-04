import asyncio
import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

import keymasq.session.manager.core as session_manager_core_module
import keymasq.session.manager.events as session_events_module
import keymasq.session.manager.profiles as session_profiles_module
from keymasq.common.ipc import Response
from keymasq.common.models import ProfileConfig, SuperkeyConfig, SuperkeyMode
from keymasq.common.security import PeerCredentials
from keymasq.session.manager import SessionManager
from keymasq.session.profiles import ProfileInfo


class _FakeKeymasqdClient:
    def __init__(self) -> None:
        self.connect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def wait_disconnected(self) -> None:
        raise ConnectionResetError("simulated restart")

    async def disconnect(self) -> None:
        return

    async def send_command(self, _command: object) -> Response:
        return Response(status="ok", data={"count": 1})


class _FakeSessionReader:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def read(self, _size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _FakeSessionWriter:
    def __init__(self) -> None:
        self.closed = False
        self.wait_closed_calls = 0
        self.writes: list[bytes] = []

    def get_extra_info(self, name: str):
        if name == "socket":
            return object()
        return None

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.wait_closed_calls += 1


class _HangingSessionWriter(_FakeSessionWriter):
    def __init__(self) -> None:
        super().__init__()
        self.abort_calls = 0
        self.transport = self

    async def wait_closed(self) -> None:
        self.wait_closed_calls += 1
        await asyncio.Event().wait()

    def abort(self) -> None:
        self.abort_calls += 1


class _FakeSessionServer:
    def __init__(self) -> None:
        self.closed = False
        self.wait_closed_calls = 0

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.wait_closed_calls += 1


def _inotify_event(wd: int, mask: int, name: str) -> bytes:
    raw_name = name.encode() + b"\0"
    return (
        session_manager_core_module.INOTIFY_EVENT_STRUCT.pack(
            wd,
            mask,
            0,
            len(raw_name),
        )
        + raw_name
    )


@pytest.mark.asyncio
async def test_connect_loop_reconnect_reapplies_profiles_after_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.client = _FakeKeymasqdClient()
    manager.running = True
    manager._retry_event.set()
    manager.profile_state.grabbed_devices = {"1234:5678"}
    manager.profile_state.active_profile_names = ["base"]

    activations: list[str] = []
    status_events: list[bool] = []

    async def _activate_initial_profiles() -> None:
        activations.append("apply")
        if len(activations) >= 2:
            manager.running = False

    monkeypatch.setattr(
        session_profiles_module,
        "activate_initial_profiles",
        lambda _manager: _activate_initial_profiles(),
    )
    manager._broadcast_keymasqd_status = lambda connected: status_events.append(connected)  # type: ignore[assignment]

    await manager.connect_loop()

    assert activations == ["apply", "apply"]
    assert status_events[:4] == [True, False, True, False]
    assert manager.profile_state.grabbed_devices == set()
    assert manager.profile_state.active_profile_names == []


@pytest.mark.asyncio
async def test_connect_loop_logs_unexpected_runtime_failures(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FakeClient:
        async def connect(self) -> None:
            return None

        async def wait_disconnected(self) -> None:
            return None

        async def send_command(self, _command: object) -> Response:
            return Response(status="ok", data={"count": 1})

    manager = SessionManager()
    manager.client = FakeClient()  # type: ignore[assignment]
    manager.running = True
    manager._broadcast_keymasqd_status = Mock()  # type: ignore[method-assign]

    async def _sync_pending_macro_slots(_manager: SessionManager) -> None:
        manager.running = False
        raise RuntimeError("sync bug")

    monkeypatch.setattr(
        session_profiles_module,
        "activate_initial_profiles",
        AsyncMock(),
    )
    monkeypatch.setattr(
        session_manager_core_module.runtime_recording,
        "sync_pending_macro_slots_from_daemon",
        _sync_pending_macro_slots,
    )

    with caplog.at_level("ERROR", logger="keymasq-session"):
        await manager.connect_loop()

    assert "Unexpected keymasqd connection loop failure" in caplog.text
    assert "sync bug" in caplog.text
    assert manager.connected is False


@pytest.mark.asyncio
async def test_sync_virtual_gamepads_ignores_malformed_daemon_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SessionManager()
    manager.connected = True
    manager.virtual_gamepad_count = 2
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"count": "not-a-number"})
    )

    with caplog.at_level("WARNING", logger="keymasq-session"):
        await manager._sync_virtual_gamepads_to_daemon()

    assert manager.virtual_gamepad_count == 2
    assert "Ignoring malformed virtual gamepad count from keymasqd" in caplog.text


def test_signal_handler_only_sets_shutdown_state() -> None:
    manager = SessionManager()
    manager.running = True

    manager._signal_handler()

    assert manager.running is True
    assert manager._shutdown_event.is_set()
    assert manager._retry_event.is_set()


@pytest.mark.asyncio
async def test_start_wires_runtime_tasks_and_stops_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    added_signals = []

    async def start_session_server() -> None:
        asyncio.get_running_loop().call_soon(manager._shutdown_event.set)

    async def connect_loop() -> None:
        await asyncio.sleep(0)

    async def compositor_supervisor_loop(_manager: SessionManager) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(
        session_manager_core_module.asyncio,
        "get_event_loop",
        lambda: SimpleNamespace(
            add_signal_handler=lambda *args: added_signals.append(args),
        ),
    )
    monkeypatch.setattr(
        session_manager_core_module.runtime_compositor,
        "compositor_supervisor_loop",
        compositor_supervisor_loop,
    )
    manager._start_session_server = AsyncMock(side_effect=start_session_server)  # type: ignore[method-assign]
    manager.connect_loop = connect_loop  # type: ignore[method-assign]
    manager._start_config_watcher = Mock()  # type: ignore[method-assign]
    manager.stop = AsyncMock()  # type: ignore[method-assign]

    await manager.start()

    assert manager.running is True
    assert len(added_signals) == 3
    manager._start_session_server.assert_awaited_once()  # type: ignore[attr-defined]
    manager._start_config_watcher.assert_called_once()  # type: ignore[attr-defined]
    manager.stop.assert_awaited_once()  # type: ignore[attr-defined]
    assert manager.connect_task is not None
    assert manager.compositor_state.supervisor_task is not None


@pytest.mark.asyncio
async def test_stop_cancels_tracked_event_tasks() -> None:
    manager = SessionManager()
    manager.running = True
    manager.client.disconnect = AsyncMock()  # type: ignore[method-assign]
    manager.dbus.disconnect = AsyncMock()  # type: ignore[method-assign]
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _wait_forever() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = session_events_module.create_event_task(
        manager,
        _wait_forever(),
        name="stop-test",
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await manager.stop()

    assert cancelled.is_set()
    assert task.cancelled()
    assert manager.event_state.tasks == set()
    manager.client.disconnect.assert_awaited_once()  # type: ignore[attr-defined]
    manager.dbus.disconnect.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_stop_cleans_runtime_tasks_server_capture_and_owned_socket(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.running = True
    manager.client.disconnect = AsyncMock()  # type: ignore[method-assign]
    manager.client.send_command = AsyncMock(return_value=Response(status="ok"))
    manager.dbus.disconnect = AsyncMock()  # type: ignore[method-assign]
    manager.action_handler = SimpleNamespace(cancel_background_tasks=AsyncMock())  # type: ignore[assignment]
    manager.session_server = _FakeSessionServer()  # type: ignore[assignment]
    manager.capture_state.tokens["1234:5678"] = "capture-token"
    socket_path = tmp_path / "session.sock"
    socket_path.write_text("")
    manager._session_socket_owned = True
    monkeypatch.setattr(session_manager_core_module, "SESSION_SOCKET_PATH", socket_path)

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    manager.compositor_state.supervisor_task = asyncio.create_task(wait_forever())
    manager.profile_state.apply_task = asyncio.create_task(wait_forever())
    manager.profile_state.topology_refresh_task = asyncio.create_task(wait_forever())
    manager.recording_state.settings_save_task = asyncio.create_task(asyncio.sleep(0))
    manager.connect_task = asyncio.create_task(wait_forever())

    await manager.stop()

    assert manager.compositor_state.supervisor_task is None
    assert manager.profile_state.apply_task is None
    assert manager.profile_state.topology_refresh_task is None
    assert manager.recording_state.settings_save_task is None
    assert manager.connect_task is None
    assert manager.capture_state.tokens == {}
    assert socket_path.exists() is False
    assert manager._session_socket_owned is False
    assert manager.session_server.closed is True  # type: ignore[union-attr]
    assert manager.session_server.wait_closed_calls == 1  # type: ignore[union-attr]
    manager.client.send_command.assert_awaited_once()  # type: ignore[attr-defined]
    manager.client.disconnect.assert_awaited_once()  # type: ignore[attr-defined]
    assert manager.action_handler.cancel_background_tasks.await_count == 2


@pytest.mark.asyncio
async def test_stop_cancels_grab_retry_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = SessionManager()
    manager.running = True
    manager.client.disconnect = AsyncMock()  # type: ignore[method-assign]
    manager.dbus.disconnect = AsyncMock()  # type: ignore[method-assign]
    reevaluate_profiles = AsyncMock()
    monkeypatch.setattr(session_profiles_module, "reevaluate_profiles", reevaluate_profiles)

    session_profiles_module.schedule_grab_retry(manager, "1234:5678", delay_s=0.05)
    retry_task = manager.profile_state.grab_retry_tasks["1234:5678"]

    await manager.stop()
    await asyncio.sleep(0.1)

    assert manager.profile_state.grab_retry_tasks == {}
    assert retry_task.done()
    reevaluate_profiles.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_times_out_hanging_session_client_wait_closed() -> None:
    manager = SessionManager()
    manager.running = True
    manager.SESSION_CLIENT_CLOSE_TIMEOUT_S = 0.01
    manager.client.disconnect = AsyncMock()  # type: ignore[method-assign]
    manager.dbus.disconnect = AsyncMock()  # type: ignore[method-assign]
    writer = _HangingSessionWriter()
    manager.session_clients.add(writer)  # type: ignore[arg-type]

    await asyncio.wait_for(manager.stop(), timeout=1.0)

    assert writer.closed is True
    assert writer.wait_closed_calls == 1
    assert writer.abort_calls == 1
    assert writer not in manager.session_clients


@pytest.mark.asyncio
async def test_reload_handler_debounces_burst_updates() -> None:
    manager = SessionManager()
    calls = 0

    async def fake_reload_profiles() -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        manager.reload_pending = False

    manager.reload_profiles = fake_reload_profiles  # type: ignore[method-assign]

    manager._reload_handler()
    first_task = manager.reload_task
    assert first_task is not None

    manager._reload_handler()
    manager._reload_handler()
    assert manager.reload_task is first_task

    await first_task
    assert calls == 1

    manager._reload_handler()
    second_task = manager.reload_task
    assert second_task is not None
    assert second_task is not first_task

    await second_task
    assert calls == 2


@pytest.mark.asyncio
async def test_reload_profiles_invalidates_runtime_payload_signatures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.profile_state.last_sent_mapping_signatures = {"1234:5678": "sig"}
    manager.profile_state.last_sent_combo_signature = "combo-sig"
    reevaluate_profiles = AsyncMock()

    monkeypatch.setattr(manager, "reload_config_from_disk", lambda: None)
    monkeypatch.setattr(session_profiles_module, "reevaluate_profiles", reevaluate_profiles)

    await manager.reload_profiles()

    assert manager.profile_state.last_sent_mapping_signatures == {}
    assert manager.profile_state.last_sent_combo_signature == ""
    reevaluate_profiles.assert_awaited_once_with(manager, reason="config reload")


@pytest.mark.asyncio
async def test_reload_profiles_failure_keeps_previous_config_and_notifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.send_notification = Mock()  # type: ignore[method-assign]
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    reevaluate_profiles = AsyncMock()

    monkeypatch.setattr(
        manager,
        "reload_config_from_disk",
        lambda: (_ for _ in ()).throw(ValueError("bad profile TOML")),
    )
    monkeypatch.setattr(session_profiles_module, "reevaluate_profiles", reevaluate_profiles)

    await manager.reload_profiles()

    manager.send_notification.assert_called_once_with(  # type: ignore[attr-defined]
        "Keymasq Config Error",
        "Failed to reload config; keeping the previous active config. See logs.",
    )
    manager.broadcast_to_session_clients.assert_called_once()  # type: ignore[attr-defined]
    reevaluate_profiles.assert_not_awaited()


def test_reload_config_from_disk_rolls_back_user_config_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    old_profile = ProfileConfig(name="Old", enabled=True, is_permanent=True)
    manager.profiles.restore_profiles(
        {
            "Old": ProfileInfo(
                path=session_manager_core_module.CONFIG_DIR / "old.toml",
                config=old_profile,
            )
        }
    )
    old_superkey = SuperkeyConfig(name="OldSuper", mode=SuperkeyMode.PATTERN)
    manager.superkeys.restore_superkeys({"OldSuper": old_superkey})
    manager.virtual_gamepad_count = 2
    reload_calls: list[str] = []

    def reload_superkeys() -> None:
        reload_calls.append("superkeys")
        manager.superkeys.restore_superkeys(
            {"NewSuper": SuperkeyConfig(name="NewSuper", mode=SuperkeyMode.PATTERN)}
        )

    def reload_profiles() -> None:
        reload_calls.append("profiles")
        raise ValueError("bad profile TOML")

    monkeypatch.setattr(manager.superkeys, "reload", reload_superkeys)
    monkeypatch.setattr(manager.analog_controls, "reload", lambda: None)
    monkeypatch.setattr(manager.profiles, "reload", reload_profiles)
    monkeypatch.setattr(manager.hardware, "reload", lambda: None)

    with pytest.raises(ValueError, match="bad profile TOML"):
        manager.reload_config_from_disk()

    assert reload_calls == ["superkeys", "profiles"]
    assert manager.profiles.get_profile("Old") is not None
    assert manager.superkeys.get_superkey("OldSuper") is old_superkey
    assert manager.superkeys.get_superkey("NewSuper") is None
    assert manager.virtual_gamepad_count == 2


@pytest.mark.asyncio
async def test_session_client_drops_connection_when_buffer_exceeds_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.running = True
    reader = _FakeSessionReader([b"x" * (manager.MAX_SESSION_CLIENT_BUFFER_BYTES + 1)])
    writer = _FakeSessionWriter()
    manager._handle_session_request = AsyncMock(return_value={"status": "ok"})  # type: ignore[method-assign]

    monkeypatch.setattr(
        session_manager_core_module,
        "get_peer_credentials",
        lambda _sock: PeerCredentials(pid=321, uid=1000, gid=1000),
    )

    await manager._handle_session_client(reader, writer)  # type: ignore[arg-type]

    manager._handle_session_request.assert_not_awaited()
    assert writer.closed is True
    assert writer.writes == []


@pytest.mark.asyncio
async def test_session_client_cleanup_times_out_hanging_wait_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.running = True
    manager.SESSION_CLIENT_CLOSE_TIMEOUT_S = 0.01
    reader = _FakeSessionReader([])
    writer = _HangingSessionWriter()

    monkeypatch.setattr(
        session_manager_core_module,
        "get_peer_credentials",
        lambda _sock: PeerCredentials(pid=321, uid=1000, gid=1000),
    )

    await asyncio.wait_for(
        manager._handle_session_client(reader, writer),  # type: ignore[arg-type]
        timeout=1.0,
    )

    assert writer.closed is True
    assert writer.wait_closed_calls == 1
    assert writer.abort_calls == 1
    assert writer not in manager.session_clients


@pytest.mark.asyncio
async def test_session_client_request_error_keeps_connection_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.running = True
    reader = _FakeSessionReader(
        [
            json.dumps({"command": "bad"}).encode() + b"\n",
            json.dumps({"command": "ping"}).encode() + b"\n",
        ]
    )
    writer = _FakeSessionWriter()
    manager._handle_session_request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[ValueError("bad payload"), {"status": "ok"}]
    )

    monkeypatch.setattr(
        session_manager_core_module,
        "get_peer_credentials",
        lambda _sock: PeerCredentials(pid=321, uid=1000, gid=1000),
    )

    await manager._handle_session_client(reader, writer)  # type: ignore[arg-type]

    assert manager._handle_session_request.await_count == 2
    responses = [json.loads(payload) for payload in b"".join(writer.writes).splitlines()]
    assert responses == [
        {"status": "error", "message": "bad payload"},
        {"status": "ok"},
    ]


@pytest.mark.asyncio
async def test_session_client_rejects_missing_or_denied_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.running = True

    missing_peer_writer = _FakeSessionWriter()
    monkeypatch.setattr(
        session_manager_core_module,
        "get_peer_credentials",
        lambda _sock: None,
    )
    await manager._handle_session_client(  # type: ignore[arg-type]
        _FakeSessionReader([]),
        missing_peer_writer,
    )

    denied_writer = _FakeSessionWriter()
    manager.security_policy.session_allowed_uids = {1000}
    monkeypatch.setattr(
        session_manager_core_module,
        "get_peer_credentials",
        lambda _sock: PeerCredentials(pid=321, uid=2000, gid=2000),
    )
    await manager._handle_session_client(  # type: ignore[arg-type]
        _FakeSessionReader([]),
        denied_writer,
    )

    assert missing_peer_writer.closed is True
    assert denied_writer.closed is True
    assert denied_writer.writes == []


@pytest.mark.asyncio
async def test_session_client_handles_invalid_json_and_unexpected_request_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.running = True
    reader = _FakeSessionReader(
        [
            b"{not-json}\n",
            json.dumps({"command": "boom"}).encode() + b"\n",
        ]
    )
    writer = _FakeSessionWriter()
    manager._handle_session_request = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("request failed")
    )
    monkeypatch.setattr(
        session_manager_core_module,
        "get_peer_credentials",
        lambda _sock: PeerCredentials(pid=321, uid=1000, gid=1000),
    )

    await manager._handle_session_client(reader, writer)  # type: ignore[arg-type]

    responses = [json.loads(payload) for payload in b"".join(writer.writes).splitlines()]
    assert responses == [
        {"error": "invalid json"},
        {"status": "error", "message": "request failed"},
    ]


@pytest.mark.asyncio
async def test_broadcast_drain_and_close_error_paths() -> None:
    manager = SessionManager()
    ok_writer = _FakeSessionWriter()
    skipped_writer = _FakeSessionWriter()
    os_error_writer = _FakeSessionWriter()
    generic_error_writer = _FakeSessionWriter()
    os_error_writer.write = Mock(side_effect=OSError("closed"))  # type: ignore[method-assign]
    generic_error_writer.write = Mock(side_effect=RuntimeError("broken"))  # type: ignore[method-assign]
    manager.session_clients.update(
        [ok_writer, skipped_writer, os_error_writer, generic_error_writer]  # type: ignore[list-item]
    )
    manager._close_session_writer = AsyncMock()  # type: ignore[method-assign]

    manager.broadcast_to_session_client_ids(
        {"event": "update"},
        {id(ok_writer), id(os_error_writer), id(generic_error_writer)},
    )
    await asyncio.sleep(0)

    assert ok_writer.writes == [b'{"event": "update"}\n']
    assert skipped_writer.writes == []
    assert os_error_writer not in manager.session_clients
    assert generic_error_writer not in manager.session_clients
    assert manager._close_session_writer.await_count == 2


@pytest.mark.asyncio
async def test_drain_session_writer_drops_clients_on_drain_errors() -> None:
    manager = SessionManager()

    for exc in (OSError("closed"), RuntimeError("broken")):
        writer = _FakeSessionWriter()
        writer.drain = AsyncMock(side_effect=exc)  # type: ignore[method-assign]
        manager.session_clients.add(writer)  # type: ignore[arg-type]
        manager.session_client_drain_tasks[writer] = asyncio.current_task()  # type: ignore[assignment]
        manager._close_session_writer = AsyncMock()  # type: ignore[method-assign]

        await manager._drain_session_writer(writer)  # type: ignore[arg-type]

        assert writer not in manager.session_clients
        manager._close_session_writer.assert_awaited_once()  # type: ignore[attr-defined]
        assert writer not in manager.session_client_drain_tasks


@pytest.mark.asyncio
async def test_send_notification_logs_even_when_dbus_notification_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SessionManager()
    manager.dbus.notify = AsyncMock(side_effect=RuntimeError("notification unavailable"))  # type: ignore[method-assign]

    with caplog.at_level("INFO", logger="keymasq-session"):
        manager.send_notification("Keymasq: Grab Pending", "Test Keyboard is waiting.")
        await asyncio.sleep(0)

    assert "Notification: Keymasq: Grab Pending: Test Keyboard is waiting." in caplog.text
    manager.dbus.notify.assert_awaited_once_with(  # type: ignore[attr-defined]
        "Keymasq: Grab Pending",
        "Test Keyboard is waiting.",
        app_name="keymasq",
        timeout_ms=5000,
    )


def test_send_notification_without_running_loop_returns(caplog: pytest.LogCaptureFixture) -> None:
    manager = SessionManager()

    with caplog.at_level("INFO", logger="keymasq-session"):
        manager.send_notification("Title", "Message")

    assert "Notification: Title: Message" in caplog.text


@pytest.mark.asyncio
async def test_config_watcher_lifecycle_and_registration_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    added_readers = []
    removed_readers = []
    closed_fds = []

    class FakeLoop:
        def __init__(self) -> None:
            self.add_reader_error: Exception | None = None

        def add_reader(self, *args) -> None:
            if self.add_reader_error is not None:
                raise self.add_reader_error
            added_readers.append(args)

        def remove_reader(self, fd: int) -> None:
            removed_readers.append(fd)

    fake_loop = FakeLoop()
    watch_ids = iter([101, 102, 103, 104, 105, 201, 202, 203, 204, 205])

    def add_watch(_fd, path, _mask):
        if path == session_manager_core_module.HARDWARE_DIR:
            raise OSError("missing")
        return next(watch_ids)

    monkeypatch.setattr(
        session_manager_core_module.asyncio,
        "get_running_loop",
        lambda: fake_loop,
    )
    monkeypatch.setattr(session_manager_core_module, "_inotify_init", lambda: 42)
    monkeypatch.setattr(session_manager_core_module, "_inotify_add_watch", add_watch)
    monkeypatch.setattr(session_manager_core_module.os, "close", lambda fd: closed_fds.append(fd))

    manager._start_config_watcher()

    assert manager.config_watch_fd == 42
    assert added_readers == [(42, manager._handle_config_watch_events)]
    assert session_manager_core_module.HARDWARE_DIR not in manager.config_watch_watches.values()
    assert len(manager.config_watch_watches) == 4

    manager._stop_config_watcher()

    assert manager.config_watch_fd is None
    assert manager.config_watch_watches == {}
    assert removed_readers == [42]
    assert closed_fds == [42]

    fake_loop.add_reader_error = RuntimeError("unsupported")
    manager._start_config_watcher()

    assert manager.config_watch_fd is None
    assert closed_fds[-1] == 42

    monkeypatch.setattr(
        session_manager_core_module,
        "_inotify_init",
        Mock(side_effect=OSError("no inotify")),
    )
    manager._start_config_watcher()

    assert manager.config_watch_fd is None


@pytest.mark.asyncio
async def test_config_watch_event_parsing_and_reload_scheduling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.config_watch_fd = 9
    manager.config_watch_watches = {
        1: session_manager_core_module.PROFILES_DIR,
        2: session_manager_core_module.CONFIG_DIR,
    }
    manager._refresh_config_watches = Mock()  # type: ignore[method-assign]
    manager._schedule_config_reload = Mock()  # type: ignore[method-assign]
    data = b"".join(
        [
            _inotify_event(99, session_manager_core_module.IN_CLOSE_WRITE, "ignored.toml"),
            _inotify_event(2, session_manager_core_module.IN_IGNORED, "profiles"),
            _inotify_event(1, session_manager_core_module.IN_CLOSE_WRITE, "Base.toml"),
        ]
    )
    monkeypatch.setattr(session_manager_core_module.os, "read", lambda _fd, _size: data)

    manager._handle_config_watch_events()

    assert 2 not in manager.config_watch_watches
    manager._refresh_config_watches.assert_called_once()  # type: ignore[attr-defined]
    manager._schedule_config_reload.assert_called_once()  # type: ignore[attr-defined]

    manager.config_watch_fd = None
    manager._handle_config_watch_events()

    manager.config_watch_fd = 9
    monkeypatch.setattr(
        session_manager_core_module.os,
        "read",
        Mock(side_effect=BlockingIOError),
    )
    manager._handle_config_watch_events()

    manager._stop_config_watcher = Mock()  # type: ignore[method-assign]
    monkeypatch.setattr(
        session_manager_core_module.os,
        "read",
        Mock(side_effect=OSError("dead fd")),
    )
    manager._handle_config_watch_events()

    manager._stop_config_watcher.assert_called_once()  # type: ignore[attr-defined]


def test_config_watch_relevance_rules() -> None:
    manager = SessionManager()

    assert manager._config_watch_event_is_relevant(
        session_manager_core_module.CONFIG_DIR,
        session_manager_core_module.SETTINGS_PATH.name,
        0,
    )
    assert manager._config_watch_event_is_relevant(
        session_manager_core_module.CONFIG_DIR,
        session_manager_core_module.PROFILES_DIR.name,
        session_manager_core_module.IN_ISDIR,
    )
    assert not manager._config_watch_event_is_relevant(
        session_manager_core_module.CONFIG_DIR,
        "notes.txt",
        0,
    )
    assert manager._config_watch_event_is_relevant(
        session_manager_core_module.PROFILES_DIR,
        "Base.toml",
        0,
    )
    assert not manager._config_watch_event_is_relevant(
        session_manager_core_module.PROFILES_DIR,
        "Base.txt",
        0,
    )
    assert manager._config_watch_event_is_relevant(
        session_manager_core_module.PROFILES_DIR,
        "",
        session_manager_core_module.IN_MOVE_SELF,
    )


@pytest.mark.asyncio
async def test_scheduled_config_reload_branches() -> None:
    manager = SessionManager()
    manager.reload_profiles = AsyncMock(return_value=True)  # type: ignore[method-assign]

    manager.running = False
    manager._run_scheduled_config_reload()

    assert manager.reload_task is None

    manager.running = True
    manager.reload_task = asyncio.create_task(asyncio.sleep(0.1))
    manager._run_scheduled_config_reload()
    running_task = manager.reload_task

    assert running_task is not None
    assert not running_task.done()
    running_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running_task

    manager.reload_task = None
    manager._schedule_config_reload()
    first_timer = manager.config_reload_timer
    manager._schedule_config_reload()

    assert first_timer is not None
    assert first_timer.cancelled()
    assert manager.config_reload_timer is not first_timer
    manager.config_reload_timer.cancel()

    manager._run_scheduled_config_reload()
    reload_task = manager.reload_task
    assert reload_task is not None
    await cast(asyncio.Task[object], reload_task)
    manager.reload_profiles.assert_awaited_once()  # type: ignore[attr-defined]
