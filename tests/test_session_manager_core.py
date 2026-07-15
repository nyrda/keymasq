import asyncio
import json
import threading
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

import keymasq.session.manager.core as session_manager_core_module
import keymasq.session.manager.events as session_events_module
import keymasq.session.manager.profile.application as profile_application
import keymasq.session.manager.recording_lifecycle as recording_lifecycle_module
import keymasq.session.manager.service.server as session_server_module
import keymasq.session.manager.service.watcher as config_watcher_module
from keymasq.common.ipc import Response
from keymasq.common.model.core import SuperkeyMode
from keymasq.common.model.hardware import ButtonDefinition
from keymasq.common.model.profiles import ProfileConfig
from keymasq.common.model.superkeys import SuperkeyConfig
from keymasq.common.security import PeerCredentials
from keymasq.session.manager.core import SessionManager
from keymasq.session.manager.profile import coordinator
from keymasq.session.profile.types import ProfileInfo
from tests.async_fakes import (
    FakeStreamReader as _FakeSessionReader,
)
from tests.async_fakes import (
    FakeStreamWriter as _FakeSessionWriter,
)
from tests.async_fakes import (
    HangingStreamWriter as _HangingSessionWriter,
)


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
        config_watcher_module.INOTIFY_EVENT_STRUCT.pack(
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
        coordinator,
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
        coordinator,
        "activate_initial_profiles",
        AsyncMock(),
    )
    monkeypatch.setattr(
        recording_lifecycle_module,
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


@pytest.mark.asyncio
async def test_sync_virtual_gamepads_clamps_negative_daemon_count() -> None:
    manager = SessionManager()
    manager.connected = True
    manager.virtual_gamepad_count = 2
    manager.client.send_command = AsyncMock(return_value=Response(status="ok", data={"count": -3}))

    await manager._sync_virtual_gamepads_to_daemon()

    assert manager.virtual_gamepad_count == 0


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
    mpris_controller = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())

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
        session_manager_core_module.compositor,
        "compositor_supervisor_loop",
        compositor_supervisor_loop,
    )
    manager._start_session_server = AsyncMock(side_effect=start_session_server)  # type: ignore[method-assign]
    manager.connect_loop = connect_loop  # type: ignore[method-assign]
    manager._start_config_watcher = Mock()  # type: ignore[method-assign]
    manager.mpris_controller = mpris_controller  # type: ignore[assignment]
    manager.stop = AsyncMock()  # type: ignore[method-assign]

    await manager.start()

    assert manager.running is True
    assert len(added_signals) == 3
    manager._start_session_server.assert_awaited_once()  # type: ignore[attr-defined]
    mpris_controller.start.assert_awaited_once()
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

    class FakeSocketPath:
        def __init__(self) -> None:
            self.exists_called = False
            self.unlinked = False

        def exists(self) -> bool:
            self.exists_called = True
            return not self.unlinked

        def unlink(self) -> None:
            self.unlinked = True

    socket_path = FakeSocketPath()
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
    assert socket_path.exists_called is True
    assert socket_path.unlinked is True
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
    monkeypatch.setattr(coordinator, "reevaluate_profiles", reevaluate_profiles)

    coordinator.schedule_grab_retry(manager, "1234:5678", delay_s=0.05)
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
    monkeypatch.setattr(coordinator, "reevaluate_profiles", reevaluate_profiles)

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
    monkeypatch.setattr(coordinator, "reevaluate_profiles", reevaluate_profiles)

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
                path=config_watcher_module.CONFIG_DIR / "old.toml",
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


def test_reload_config_from_disk_serializes_profile_writes_until_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.profiles.save_profile(ProfileConfig(name="Nav", enabled=False, is_permanent=True))
    original_snapshot = manager.profiles.snapshot_profiles_for_reload
    writer_started = threading.Event()
    writer_done = threading.Event()
    writer_errors: list[object] = []
    writer: threading.Thread | None = None

    def enable_profile() -> None:
        writer_started.set()
        try:
            manager.profiles.set_profile_enabled("Nav", True)
        except (AssertionError, OSError, RuntimeError, ValueError) as exc:
            writer_errors.append(exc)
        finally:
            writer_done.set()

    def snapshot_profiles_for_reload() -> dict[str, ProfileInfo]:
        nonlocal writer
        snapshot = original_snapshot()
        writer = threading.Thread(target=enable_profile)
        writer.start()
        assert writer_started.wait(timeout=5)
        assert not writer_done.wait(timeout=0.2)
        return snapshot

    def reload_hardware() -> None:
        raise ValueError("bad hardware TOML")

    monkeypatch.setattr(
        manager.profiles,
        "snapshot_profiles_for_reload",
        snapshot_profiles_for_reload,
    )
    monkeypatch.setattr(manager.hardware, "reload", reload_hardware)

    with pytest.raises(ValueError, match="bad hardware TOML"):
        manager.reload_config_from_disk()

    assert writer is not None
    writer.join(timeout=5)
    assert not writer.is_alive()
    assert writer_errors == []
    profile = manager.profiles.get_profile("Nav")
    assert profile is not None
    assert profile.config.enabled is True


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
        session_server_module,
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
        session_server_module,
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
        session_server_module,
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
        session_server_module,
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
        session_server_module,
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
        session_server_module,
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
        manager._close_session_writer = AsyncMock()  # type: ignore[method-assign]

        manager.broadcast_to_session_clients({"event": "update"})
        drain_task = manager.session_client_drain_tasks[writer]  # type: ignore[index]
        await asyncio.gather(drain_task, return_exceptions=True)

        assert writer not in manager.session_clients
        assert drain_task.done()
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
        if path == config_watcher_module.HARDWARE_DIR:
            raise OSError("missing")
        return next(watch_ids)

    monkeypatch.setattr(
        config_watcher_module.asyncio,
        "get_running_loop",
        lambda: fake_loop,
    )
    monkeypatch.setattr(config_watcher_module, "_inotify_init", lambda: 42)
    monkeypatch.setattr(config_watcher_module, "_inotify_add_watch", add_watch)
    monkeypatch.setattr(config_watcher_module.os, "close", lambda fd: closed_fds.append(fd))

    manager._start_config_watcher()

    assert manager.config_watch_fd == 42
    assert added_readers == [(42, manager._handle_config_watch_events)]
    assert config_watcher_module.HARDWARE_DIR not in manager.config_watch_watches.values()
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
        config_watcher_module,
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
        1: config_watcher_module.PROFILES_DIR,
        2: config_watcher_module.CONFIG_DIR,
    }
    manager._refresh_config_watches = Mock()  # type: ignore[method-assign]
    manager._schedule_config_reload = Mock()  # type: ignore[method-assign]
    data = b"".join(
        [
            _inotify_event(99, config_watcher_module.IN_CLOSE_WRITE, "ignored.toml"),
            _inotify_event(2, config_watcher_module.IN_IGNORED, "profiles"),
            _inotify_event(1, config_watcher_module.IN_CLOSE_WRITE, "Base.toml"),
        ]
    )
    monkeypatch.setattr(config_watcher_module.os, "read", lambda _fd, _size: data)

    manager._handle_config_watch_events()

    assert 2 not in manager.config_watch_watches
    manager._refresh_config_watches.assert_called_once()  # type: ignore[attr-defined]
    manager._schedule_config_reload.assert_called_once()  # type: ignore[attr-defined]

    manager.config_watch_fd = None
    manager._handle_config_watch_events()

    manager.config_watch_fd = 9
    monkeypatch.setattr(
        config_watcher_module.os,
        "read",
        Mock(side_effect=BlockingIOError),
    )
    manager._handle_config_watch_events()

    manager._stop_config_watcher = Mock()  # type: ignore[method-assign]
    monkeypatch.setattr(
        config_watcher_module.os,
        "read",
        Mock(side_effect=OSError("dead fd")),
    )
    manager._handle_config_watch_events()

    manager._stop_config_watcher.assert_called_once()  # type: ignore[attr-defined]


def test_config_watch_relevance_rules() -> None:
    manager = SessionManager()

    assert manager._config_watch_event_is_relevant(
        config_watcher_module.CONFIG_DIR,
        config_watcher_module.SETTINGS_PATH.name,
        0,
    )
    assert manager._config_watch_event_is_relevant(
        config_watcher_module.CONFIG_DIR,
        config_watcher_module.PROFILES_DIR.name,
        config_watcher_module.IN_ISDIR,
    )
    assert not manager._config_watch_event_is_relevant(
        config_watcher_module.CONFIG_DIR,
        "notes.txt",
        0,
    )
    assert manager._config_watch_event_is_relevant(
        config_watcher_module.PROFILES_DIR,
        "Base.toml",
        0,
    )
    assert not manager._config_watch_event_is_relevant(
        config_watcher_module.PROFILES_DIR,
        "Base.txt",
        0,
    )
    assert manager._config_watch_event_is_relevant(
        config_watcher_module.PROFILES_DIR,
        "",
        config_watcher_module.IN_MOVE_SELF,
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


@pytest.mark.asyncio
async def test_explicit_reload_coalesces_config_watcher_reload() -> None:
    manager = SessionManager()
    manager.reload_profiles = AsyncMock(return_value=True)  # type: ignore[method-assign]

    manager._schedule_config_reload()
    first_timer = manager.config_reload_timer

    assert first_timer is not None

    manager.suppress_config_watcher_reload()

    assert first_timer.cancelled()
    assert manager.config_reload_timer is None

    manager._schedule_config_reload()

    assert manager.config_reload_timer is None

    manager._schedule_config_reload()

    assert manager.config_reload_timer is None

    manager.running = True
    manager._run_scheduled_config_reload()

    manager.reload_profiles.assert_not_awaited()  # type: ignore[attr-defined]

    manager._config_reload_coalesce_until = 0.0
    manager._schedule_config_reload()

    assert manager.config_reload_timer is not None
    manager.config_reload_timer.cancel()


def test_resolved_button_codes_skips_unresolved_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    buttons = [
        SimpleNamespace(id="", evdev_code=1, evdev=None),
        SimpleNamespace(id="explicit", evdev_code=42, evdev=None),
        SimpleNamespace(id="resolved", evdev_code=None, evdev="KEY_A"),
        SimpleNamespace(id="missing", evdev_code=None, evdev="KEY_UNKNOWN"),
    ]

    monkeypatch.setattr(
        session_manager_core_module,
        "resolve_evdev_code",
        lambda evdev: 30 if evdev == "KEY_A" else None,
    )

    assert manager.resolved_button_codes(cast(list[ButtonDefinition], buttons)) == {
        "explicit": 42,
        "resolved": 30,
    }


@pytest.mark.asyncio
async def test_stop_returns_when_already_stopped() -> None:
    manager = SessionManager()
    manager.client.disconnect = AsyncMock()  # type: ignore[method-assign]

    await manager.stop()

    manager.client.disconnect.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_stop_cancels_drain_tasks_and_ignores_capture_shutdown_errors() -> None:
    manager = SessionManager()
    manager.running = True
    manager.client.disconnect = AsyncMock()  # type: ignore[method-assign]
    manager.client.send_command = AsyncMock(  # type: ignore[method-assign]
        side_effect=[OSError("daemon closed"), RuntimeError("daemon bug")]
    )
    manager.dbus.disconnect = AsyncMock()  # type: ignore[method-assign]
    manager.capture_state.tokens = {"keyboard": "token-a", "mouse": "token-b"}

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    writer_a = _FakeSessionWriter()
    writer_b = _FakeSessionWriter()
    task_a = asyncio.create_task(wait_forever())
    task_b = asyncio.create_task(wait_forever())
    manager.session_client_drain_tasks = {
        writer_a: task_a,  # type: ignore[dict-item]
        writer_b: task_b,  # type: ignore[dict-item]
    }

    await manager.stop()
    await asyncio.sleep(0)

    assert task_a.cancelled()
    assert task_b.cancelled()
    assert manager.session_client_drain_tasks == {}
    assert manager.capture_state.tokens == {}
    assert manager.client.send_command.await_count == 2  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_stop_marks_owned_socket_unowned_when_unlink_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.running = True
    manager.client.disconnect = AsyncMock()  # type: ignore[method-assign]
    manager.dbus.disconnect = AsyncMock()  # type: ignore[method-assign]
    manager._session_socket_owned = True

    class FakeSocketPath:
        def exists(self) -> bool:
            return True

        def unlink(self) -> None:
            raise OSError("permission denied")

    monkeypatch.setattr(session_manager_core_module, "SESSION_SOCKET_PATH", FakeSocketPath())

    await manager.stop()

    assert manager._session_socket_owned is False


@pytest.mark.asyncio
async def test_start_session_server_creates_owned_socket_and_warns_on_chmod_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    calls: list[object] = []
    server = _FakeSessionServer()

    class FakeSocketPath:
        def exists(self) -> bool:
            return False

        def __str__(self) -> str:
            return "/tmp/keymasq-session-test.sock"

    async def start_unix_server(handler, path: str):
        calls.append((handler, path))
        return server

    monkeypatch.setattr(
        session_server_module,
        "ensure_session_socket_dir",
        lambda: calls.append("ensure-dir"),
    )
    monkeypatch.setattr(session_server_module, "SESSION_SOCKET_PATH", FakeSocketPath())
    monkeypatch.setattr(session_server_module.asyncio, "start_unix_server", start_unix_server)
    monkeypatch.setattr(
        session_server_module.os,
        "chmod",
        Mock(side_effect=OSError("chmod failed")),
    )

    await manager._start_session_server()

    assert calls == [
        "ensure-dir",
        (manager._handle_session_client, "/tmp/keymasq-session-test.sock"),
    ]
    assert manager.session_server is server
    assert manager._session_socket_owned is True


@pytest.mark.asyncio
async def test_start_session_server_rejects_existing_live_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()

    class FakeSocketPath:
        def exists(self) -> bool:
            return True

        def __str__(self) -> str:
            return "/tmp/keymasq-session-live.sock"

    monkeypatch.setattr(session_server_module, "ensure_session_socket_dir", lambda: None)
    monkeypatch.setattr(session_server_module, "SESSION_SOCKET_PATH", FakeSocketPath())
    monkeypatch.setattr(
        session_server_module,
        "session_socket_accepts_connections",
        AsyncMock(return_value=True),
    )

    with pytest.raises(RuntimeError, match="already listening"):
        await manager._start_session_server()


@pytest.mark.asyncio
async def test_close_session_writer_error_branches() -> None:
    manager = SessionManager()
    manager.SESSION_CLIENT_CLOSE_TIMEOUT_S = 0.01

    timeout_writer = _HangingSessionWriter()
    timeout_writer.abort = Mock(side_effect=RuntimeError("abort failed"))  # type: ignore[method-assign]
    await manager._close_session_writer(timeout_writer)  # type: ignore[arg-type]

    os_error_writer = _FakeSessionWriter()
    os_error_writer.wait_closed = AsyncMock(side_effect=OSError("closed"))  # type: ignore[method-assign]
    await manager._close_session_writer(os_error_writer)  # type: ignore[arg-type]

    runtime_error_writer = _FakeSessionWriter()
    runtime_error_writer.wait_closed = AsyncMock(side_effect=RuntimeError("bug"))  # type: ignore[method-assign]
    await manager._close_session_writer(runtime_error_writer)  # type: ignore[arg-type]

    assert timeout_writer.abort.called  # type: ignore[attr-defined]
    os_error_writer.wait_closed.assert_awaited_once()  # type: ignore[attr-defined]
    runtime_error_writer.wait_closed.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_drop_session_client_writer_cancels_pending_drain_task() -> None:
    manager = SessionManager()
    writer = _FakeSessionWriter()
    task = asyncio.create_task(asyncio.Event().wait())
    manager.session_clients.add(writer)  # type: ignore[arg-type]
    manager.session_client_peers[writer] = PeerCredentials(pid=1, uid=2, gid=3)  # type: ignore[index]
    manager.session_client_drain_tasks[writer] = task  # type: ignore[index]

    manager._drop_session_client_writer(writer)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    assert writer not in manager.session_clients
    assert writer not in manager.session_client_peers
    assert writer not in manager.session_client_drain_tasks
    assert task.cancelled()


@pytest.mark.asyncio
async def test_wait_for_session_clients_to_close_logs_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SessionManager()
    manager.session_clients.add(_FakeSessionWriter())  # type: ignore[arg-type]

    with caplog.at_level("DEBUG", logger="keymasq-session"):
        await manager._wait_for_session_clients_to_close(timeout_s=0.01)

    assert "Timed out waiting for 1 session client(s) to close" in caplog.text


@pytest.mark.asyncio
async def test_reload_handler_defers_when_existing_reload_task_is_running() -> None:
    manager = SessionManager()
    running_task = asyncio.create_task(asyncio.sleep(1))
    manager.reload_task = running_task

    manager._reload_handler()

    assert manager.reload_pending is True
    assert manager.reload_task is running_task
    running_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running_task


@pytest.mark.asyncio
async def test_reload_profiles_deactivates_removed_hardware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.profile_state.grabbed_devices = {"present", "stale"}
    manager.profile_state.resolved_devices = {"stale": SimpleNamespace()}
    manager.hardware.list_hardware_ids = Mock(return_value=["present"])  # type: ignore[method-assign]
    manager.broadcast_to_session_clients = Mock()  # type: ignore[method-assign]
    manager._sync_virtual_gamepads_to_daemon = AsyncMock()  # type: ignore[method-assign]
    deactivate_profile = AsyncMock()
    reevaluate_profiles = AsyncMock()

    monkeypatch.setattr(manager, "reload_config_from_disk", lambda: None)
    monkeypatch.setattr(profile_application, "deactivate_profile", deactivate_profile)
    monkeypatch.setattr(coordinator, "reevaluate_profiles", reevaluate_profiles)

    assert await manager.reload_profiles() is True

    deactivate_profile.assert_awaited_once_with(manager, "stale", immediate=True)
    assert "stale" not in manager.profile_state.resolved_devices
    reevaluate_profiles.assert_awaited_once_with(manager, reason="config reload")


def test_reload_config_from_disk_success_updates_virtual_gamepad_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.virtual_gamepad_count = 1

    monkeypatch.setattr(manager.superkeys, "reload", Mock())
    monkeypatch.setattr(manager.analog_controls, "reload", Mock())
    monkeypatch.setattr(manager.profiles, "reload", Mock())
    monkeypatch.setattr(manager.hardware, "reload", Mock())
    monkeypatch.setattr(
        config_watcher_module,
        "load_global_settings",
        Mock(return_value=SimpleNamespace(virtual_gamepad_count=4)),
    )

    manager.reload_config_from_disk()

    assert manager.virtual_gamepad_count == 4


def test_stop_config_watcher_cancels_pending_timer() -> None:
    manager = SessionManager()
    timer = Mock()
    manager.config_reload_timer = timer

    manager._stop_config_watcher()

    timer.cancel.assert_called_once()
    assert manager.config_reload_timer is None


def test_refresh_config_watches_skips_missing_fd_and_existing_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()

    manager._refresh_config_watches()

    manager.config_watch_fd = 10
    manager.config_watch_watches = {1: config_watcher_module.CONFIG_DIR}
    watched: list[object] = []

    def add_watch(_fd: int, path: object, _mask: int) -> int:
        watched.append(path)
        return len(watched) + 1

    monkeypatch.setattr(config_watcher_module, "_inotify_add_watch", add_watch)

    manager._refresh_config_watches()

    assert config_watcher_module.CONFIG_DIR not in watched
    assert config_watcher_module.PROFILES_DIR in watched


@pytest.mark.asyncio
async def test_sync_virtual_gamepads_logs_daemon_transport_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SessionManager()
    manager.connected = True
    manager.virtual_gamepad_count = 3
    manager.client.send_command = AsyncMock(side_effect=OSError("daemon closed"))  # type: ignore[method-assign]

    with caplog.at_level("WARNING", logger="keymasq-session"):
        await manager._sync_virtual_gamepads_to_daemon()

    assert "Failed to configure virtual gamepads in keymasqd" in caplog.text
    assert manager.virtual_gamepad_count == 3


@pytest.mark.asyncio
async def test_handle_keymasqd_disconnect_clears_runtime_state_and_cancels_grab_retries() -> None:
    manager = SessionManager()
    manager.connected = True
    manager.profile_state.grabbed_devices = {"hardware"}
    manager.profile_state.active_profile_names = ["Base"]
    retry_task = asyncio.create_task(asyncio.Event().wait())
    manager.profile_state.grab_retry_tasks = {"hardware": retry_task}
    manager._broadcast_keymasqd_status = Mock()  # type: ignore[method-assign]

    manager._handle_keymasqd_disconnect()
    await asyncio.sleep(0)

    assert retry_task.cancelled()
    assert manager.connected is False
    assert manager.profile_state.grabbed_devices == set()
    assert manager.profile_state.active_profile_names == []
    manager._broadcast_keymasqd_status.assert_called_once_with(False)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_on_window_change_delegates_to_compositor_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    on_window_change = AsyncMock()
    monkeypatch.setattr(
        session_manager_core_module.compositor,
        "on_window_change",
        on_window_change,
    )

    await manager.on_window_change("app", "title", ["tag"])

    on_window_change.assert_awaited_once_with(manager, "app", "title", ["tag"])


@pytest.mark.asyncio
async def test_session_socket_probe_timeout_and_os_error_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.closed = False

        def setblocking(self, _blocking: bool) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    class TimeoutLoop:
        async def sock_connect(self, _sock: FakeSocket, _path: str) -> None:
            await asyncio.sleep(1)

    class ErrorLoop:
        async def sock_connect(self, _sock: FakeSocket, _path: str) -> None:
            raise OSError("missing")

    created: list[FakeSocket] = []

    def make_socket(*_args, **_kwargs) -> FakeSocket:
        sock = FakeSocket()
        created.append(sock)
        return sock

    monkeypatch.setattr(session_server_module.socket, "socket", make_socket)
    monkeypatch.setattr(
        session_server_module.asyncio,
        "get_running_loop",
        lambda: TimeoutLoop(),
    )
    assert await session_server_module.session_socket_accepts_connections(0.001)

    monkeypatch.setattr(
        session_server_module.asyncio,
        "get_running_loop",
        lambda: ErrorLoop(),
    )
    assert not await session_server_module.session_socket_accepts_connections()
    assert all(sock.closed for sock in created)


def test_inotify_wrappers_return_fd_and_raise_os_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    class FakeFunction:
        def __init__(self, results: list[int]) -> None:
            self.results = results

        def __call__(self, *_args) -> int:
            return self.results.pop(0)

    class FakeLibc:
        def __init__(self) -> None:
            self.inotify_init1 = FakeFunction([7, -1])
            self.inotify_add_watch = FakeFunction([11, -1])

    fake_libc = FakeLibc()
    monkeypatch.setattr(
        config_watcher_module.ctypes,
        "CDLL",
        lambda *_args, **_kwargs: fake_libc,
    )
    monkeypatch.setattr(config_watcher_module.ctypes, "get_errno", lambda: 24)

    assert config_watcher_module._inotify_init() == 7
    with pytest.raises(OSError):
        config_watcher_module._inotify_init()

    assert config_watcher_module._inotify_add_watch(7, tmp_path, 0) == 11
    with pytest.raises(OSError):
        config_watcher_module._inotify_add_watch(7, tmp_path, 0)
