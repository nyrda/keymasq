import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

import keymasq.session.manager.core as session_manager_core_module
import keymasq.session.manager.events as session_events_module
import keymasq.session.manager.profiles as session_profiles_module
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


@pytest.mark.asyncio
async def test_connect_loop_reconnect_reapplies_profiles_after_restart() -> None:
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

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        session_profiles_module,
        "activate_initial_profiles",
        lambda _manager: _activate_initial_profiles(),
    )
    manager._broadcast_keymasqd_status = lambda connected: status_events.append(connected)  # type: ignore[assignment]

    try:
        await manager.connect_loop()
    finally:
        monkeypatch.undo()

    assert activations == ["apply", "apply"]
    assert status_events[:4] == [True, False, True, False]
    assert manager.profile_state.grabbed_devices == set()
    assert manager.profile_state.active_profile_names == []


def test_signal_handler_only_sets_shutdown_state() -> None:
    manager = SessionManager()
    manager.running = True

    manager._signal_handler()

    assert manager.running is True
    assert manager._shutdown_event.is_set()
    assert manager._retry_event.is_set()


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
