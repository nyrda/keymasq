import asyncio
import time
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock, call

import pytest

import keyforge.session.manager as session_manager_module
import keyforge.session.manager_compositor as session_compositor_module
import keyforge.session.manager_events as session_events_module
import keyforge.session.manager_payloads as session_payloads_module
import keyforge.session.manager_profiles as session_profiles_module
import keyforge.session.manager_recording as session_recording_module
import keyforge.session.manager_session_commands as session_commands_module
from keyforge.common.ipc import Command, CommandType, Response
from keyforge.common.models import (
    ActionType,
    ComboEvent,
    ComboStep,
    DeviceProfileLayer,
    MappingAction,
    ProfileConfig,
)
from keyforge.common.security import PeerCredentials, SecurityPolicy
from keyforge.session.listeners.hyprland import HyprlandListener
from keyforge.session.listeners.kde import KDEListener
from keyforge.session.manager import SessionManager
from keyforge.session.profiles import ResolvedCombo, ResolvedDeviceProfile, ResolvedProfiles


class _FakeKeyforgedClient:
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


@pytest.mark.asyncio
async def test_connect_loop_reconnect_reapplies_profiles_after_restart() -> None:
    manager = SessionManager()
    manager.client = _FakeKeyforgedClient()
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
    manager._broadcast_keyforged_status = lambda connected: status_events.append(connected)  # type: ignore[assignment]

    try:
        await manager._connect_loop()
    finally:
        monkeypatch.undo()

    assert activations == ["apply", "apply"]
    assert status_events[:4] == [True, False, True, False]
    assert manager.profile_state.grabbed_devices == set()
    assert manager.profile_state.active_profile_names == []


@pytest.mark.asyncio
async def test_window_churn_conflict_then_fallback_keeps_deterministic_active_profile() -> None:
    manager = SessionManager()
    hardware_id = "1234:5678"

    profile_game = ProfileConfig(
        name="Game",
        enabled=True,
        device_layers={hardware_id: DeviceProfileLayer(hardware_id=hardware_id)},
    )
    profile_base = ProfileConfig(
        name="Base",
        enabled=True,
        is_permanent=True,
        device_layers={hardware_id: DeviceProfileLayer(hardware_id=hardware_id)},
    )

    manager.hardware.list_hardware_ids = lambda: [hardware_id]  # type: ignore[assignment]

    def _resolve_active_profiles(
        window_info: dict | None, _caps: list[str], hardware_ids: list[str]
    ) -> ResolvedProfiles:
        assert hardware_ids == [hardware_id]
        title = str((window_info or {}).get("title", ""))
        if title == "game":
            return ResolvedProfiles(
                active_profiles=[profile_game],
                devices={
                    hardware_id: ResolvedDeviceProfile(
                        hardware_id=hardware_id,
                        active_profile_names=["Game"],
                        mappings={},
                    )
                },
            )
        return ResolvedProfiles(
            active_profiles=[profile_base],
            devices={
                hardware_id: ResolvedDeviceProfile(
                    hardware_id=hardware_id,
                    active_profile_names=["Base"],
                    mappings={},
                )
            },
        )

    manager.profiles.resolve_active_profiles = _resolve_active_profiles  # type: ignore[assignment]

    actions: list[tuple[str, str]] = []

    async def _apply_resolved_device_profile(
        _manager: SessionManager,
        hwid: str,
        resolved: ResolvedDeviceProfile,
    ) -> None:
        actions.append(
            (
                "activate",
                resolved.active_profile_names[-1] if resolved.active_profile_names else "",
            )
        )
        manager.profile_state.resolved_devices[hwid] = resolved

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        session_profiles_module,
        "apply_resolved_device_profile",
        _apply_resolved_device_profile,
    )
    monkeypatch.setattr(session_profiles_module, "update_combos", AsyncMock())
    manager._send_notification = lambda _title, _message: None  # type: ignore[assignment]

    try:
        await manager.on_window_change("app", "game", [])
        await manager.on_window_change("app", "browser", [])
    finally:
        monkeypatch.undo()

    assert actions == [
        ("activate", "Game"),
        ("activate", "Base"),
    ]
    assert manager.profile_state.resolved_devices[hardware_id].active_profile_names == ["Base"]


@pytest.mark.asyncio
async def test_sensitive_command_requires_active_recording_owner() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=111, uid=1000, gid=1000)
    owner_writer = object()
    other_writer = object()

    lock_recording_unlock = AsyncMock(return_value={"status": "ok"})
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(session_recording_module, "lock_recording_unlock", lock_recording_unlock)
    manager.unlock_state.refresh_owner = {
        "uid": 1000,
        "pid": 111,
        "writer_id": id(owner_writer),
        "lease_id": "lease-1",
    }

    denied = await manager._handle_session_request(
        {"command": "lock_recording_unlock", "lease_id": "lease-1"},
        "client",
        peer,
        other_writer,  # type: ignore[arg-type]
    )
    assert denied["status"] == "error"
    assert denied.get("error_code") == "sensitive_command_denied"

    allowed = await manager._handle_session_request(
        {"command": "lock_recording_unlock", "lease_id": "lease-1"},
        "client",
        peer,
        owner_writer,  # type: ignore[arg-type]
    )
    assert allowed["status"] == "ok"
    lock_recording_unlock.assert_awaited_once()
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_session_request_uses_single_policy_snapshot_for_acl_and_sensitivity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=111, uid=1000, gid=1000)
    writer = object()
    start_recording = AsyncMock(return_value={"status": "ok"})
    manager._security_policy = SecurityPolicy(
        session_command_acl={"client": []},
        daemon_command_acl={"session": []},
        recording_unlock_required=True,
    )

    def fake_command_allowed(command: str, acl: dict[str, list[str]], client_class: str) -> bool:
        assert command == "start_recording"
        assert acl is manager._security_policy.session_command_acl
        assert client_class == "client"
        manager._security_policy = SecurityPolicy(
            session_command_acl={"client": []},
            daemon_command_acl={"session": []},
            recording_unlock_required=False,
        )
        return True

    monkeypatch.setattr(session_commands_module, "command_allowed", fake_command_allowed)
    monkeypatch.setattr(session_recording_module, "start_recording", start_recording)

    result = await manager._handle_session_request(
        {"command": "start_recording"},
        "client",
        peer,
        writer,  # type: ignore[arg-type]
    )

    assert result["status"] == "error"
    assert result["error_code"] == "sensitive_command_denied"
    start_recording.assert_not_awaited()


@pytest.mark.asyncio
async def test_compositor_dispatch_calls_active_listener_even_when_unsupported() -> None:
    manager = SessionManager()
    listener = SimpleNamespace(
        supports_compositor_dispatch=False,
        dispatch=AsyncMock(return_value=(False, "x11 does not implement compositor dispatch")),
    )
    manager._window_listener = listener  # type: ignore[assignment]
    manager._compositor_id = "x11"

    await session_compositor_module.handle_compositor_dispatch_trigger(
        manager,
        {"dispatcher": "workspace", "args": "2"}
    )

    listener.dispatch.assert_awaited_once_with("workspace", "2")


@pytest.mark.asyncio
async def test_compositor_dispatch_ignores_mismatched_target_compositor() -> None:
    manager = SessionManager()
    listener = SimpleNamespace(
        supports_compositor_dispatch=True,
        dispatch=AsyncMock(return_value=(True, "ok")),
    )
    manager._window_listener = listener  # type: ignore[assignment]
    manager._compositor_id = "gnome"

    await session_compositor_module.handle_compositor_dispatch_trigger(
        manager,
        {"compositor": "hyprland", "dispatcher": "workspace", "args": "2"}
    )

    listener.dispatch.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_last_client_disconnect_cleans_runtime_unlock_without_owner() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=222, uid=1000, gid=1000)
    writer = object()
    manager._session_clients.add(writer)  # type: ignore[arg-type]
    manager._session_client_peers[writer] = peer  # type: ignore[index]
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


def test_signal_handler_only_sets_shutdown_state() -> None:
    manager = SessionManager()
    manager.running = True

    manager._signal_handler()

    assert manager.running is True
    assert manager._shutdown_event.is_set()
    assert manager._retry_event.is_set()


@pytest.mark.asyncio
async def test_compositor_degraded_mode_retries_when_unsupported_or_listener_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager._listener_retry_interval_s = 0.01

    async def _unsupported(_compositor_id: str | None, _dbus=None) -> bool:
        return False

    monkeypatch.setattr("keyforge.session.manager_compositor.is_compositor_supported", _unsupported)

    await session_compositor_module.switch_compositor(manager, "wayland")
    assert manager._compositor_id == "wayland"
    assert manager._window_listener is None
    assert "wayland" in manager._listener_retry_after

    async def _supported(_compositor_id: str | None, _dbus=None) -> bool:
        return True

    monkeypatch.setattr("keyforge.session.manager_compositor.is_compositor_supported", _supported)

    async def _fail_listener_start(_manager: SessionManager) -> None:
        manager._window_listener = None
        manager._last_listener_start_error = "listener boot failed"

    monkeypatch.setattr(session_compositor_module, "start_window_listener", _fail_listener_start)
    await session_compositor_module.switch_compositor(manager, "x11")

    assert manager._compositor_id == "x11"
    assert manager._window_listener is None
    assert "x11" in manager._listener_retry_after


@pytest.mark.asyncio
async def test_reload_handler_debounces_burst_updates() -> None:
    manager = SessionManager()
    calls = 0

    async def _fake_reload_profiles() -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        manager._reload_pending = False

    manager._reload_profiles = _fake_reload_profiles  # type: ignore[assignment]

    manager._reload_handler()
    first_task = manager._reload_task
    assert first_task is not None

    manager._reload_handler()
    manager._reload_handler()
    assert manager._reload_task is first_task

    await first_task
    assert calls == 1

    manager._reload_handler()
    second_task = manager._reload_task
    assert second_task is not None
    assert second_task is not first_task

    await second_task
    assert calls == 2


@pytest.mark.asyncio
async def test_recording_settings_persistence_applies_latest_snapshot_last() -> None:
    manager = SessionManager()
    manager.recording_state.settings = {
        "include_mouse_movement": False,
        "include_mouse_clicks": False,
        "record_start_position": False,
    }
    persisted: dict[str, bool] = {}
    writes: list[dict[str, bool]] = []

    def _fake_save(_manager, settings: dict | None = None) -> None:
        state = dict(settings or {})
        # Simulate an older snapshot that takes longer to persist.
        if state.get("include_mouse_movement", False):
            time.sleep(0.05)
        else:
            time.sleep(0.005)
        persisted.clear()
        persisted.update(state)
        writes.append(state)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(session_recording_module, "save_recording_settings_to_disk", _fake_save)

    session_recording_module.update_recording_settings(manager, {"include_mouse_movement": True})
    session_recording_module.update_recording_settings(
        manager,
        {"include_mouse_movement": False, "include_mouse_clicks": True}
    )

    for _ in range(100):
        save_task = manager.recording_state.settings_save_task
        if save_task is None or save_task.done():
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("recording settings save task did not complete")

    assert writes
    assert persisted == {
        "include_mouse_movement": False,
        "include_mouse_clicks": True,
        "record_start_position": False,
    }
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_handle_session_request_returns_unknown_command_error() -> None:
    manager = SessionManager()

    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    result = await manager._handle_session_request(
        {"command": "does_not_exist"},
        "client",
        peer,
        object(),
    )

    assert result == {"error": "Unknown command: does_not_exist"}


@pytest.mark.asyncio
async def test_handle_session_request_get_active_window_uses_listener() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    class _Listener:
        name = "fake"

        async def get_active_window(self) -> tuple[str, str, list[str]]:
            return "steam", "Counter-Strike 2", ["game", "fullscreen"]

    manager._window_listener = _Listener()  # type: ignore[assignment]

    result = await manager._handle_session_request(
        {"command": "get_active_window"},
        "client",
        peer,
        object(),
    )

    assert result == {
        "status": "ok",
        "class": "steam",
        "title": "Counter-Strike 2",
        "tags": ["game", "fullscreen"],
    }
    assert manager._current_window == {
        "class": "steam",
        "title": "Counter-Strike 2",
        "tags": ["game", "fullscreen"],
    }


@pytest.mark.asyncio
async def test_handle_session_request_get_active_window_falls_back_to_cached_window() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    manager._current_window = {
        "class": "firefox",
        "title": "Docs",
        "tags": ["work"],
    }

    result = await manager._handle_session_request(
        {"command": "get_active_window"},
        "client",
        peer,
        object(),
    )

    assert result == {
        "status": "ok",
        "class": "firefox",
        "title": "Docs",
        "tags": ["work"],
    }


@pytest.mark.asyncio
async def test_handle_session_request_get_compositor_reports_compositor_dispatch_availability(
) -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    listener = HyprlandListener(AsyncMock())
    listener.running = True
    manager._window_listener = listener
    manager._compositor_id = "hyprland"

    result = await manager._handle_session_request(
        {"command": "get_compositor"},
        "client",
        peer,
        object(),
    )

    assert result["compositor_id"] == "hyprland"
    assert result["listener_active"] is True
    assert result["listener_name"] == "hyprland"
    assert result["compositor_dispatch_available"] is True


@pytest.mark.asyncio
async def test_handle_session_request_get_compositor_merges_listener_runtime_warning() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    class _Listener:
        name = "gnome"
        running = True
        supports_compositor_dispatch = True
        compositor_dispatch_available = False

        def runtime_support_details(self) -> dict[str, bool | str | int]:
            return {
                "warning": "GNOME bridge update detected. Log out and back in.",
                "bridge_protocol": 1,
                "bridge_protocol_expected": 2,
            }

    manager._window_listener = _Listener()  # type: ignore[assignment]
    manager._compositor_id = "gnome"

    async def _support_details(_compositor_id: str | None, _dbus=None) -> dict[str, bool | str]:
        return {"supported": True, "warning": ""}

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        session_compositor_module,
        "get_compositor_support_details",
        _support_details,
    )

    try:
        result = await manager._handle_session_request(
            {"command": "get_compositor"},
            "client",
            peer,
            object(),
        )
    finally:
        monkeypatch.undo()

    assert result["supported"] is True
    assert result["compositor_dispatch_available"] is False
    details = cast(dict[str, object], result["details"])
    assert details["bridge_protocol"] == 1
    assert "Log out and back in" in str(details["warning"])


async def test_handle_session_request_get_compositor_reports_kde_dispatch_availability() -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)

    listener = KDEListener(AsyncMock())
    listener.running = True
    manager._window_listener = listener
    manager._compositor_id = "kde"

    result = await manager._handle_session_request(
        {"command": "get_compositor"},
        "client",
        peer,
        object(),
    )

    assert result["compositor_id"] == "kde"
    assert result["listener_active"] is True
    assert result["listener_name"] == "kde"
    assert result["compositor_dispatch_available"] is True


@pytest.mark.asyncio
async def test_handle_event_compositor_dispatch_uses_listener() -> None:
    manager = SessionManager()
    manager.action_handler = AsyncMock()

    listener = HyprlandListener(AsyncMock())
    listener.running = True
    listener.dispatch = AsyncMock(return_value=(True, "ok"))  # type: ignore[method-assign]
    manager._window_listener = listener
    manager._compositor_id = "hyprland"

    await session_events_module.handle_event(
        manager,
        CommandType.ACTION_TRIGGER,
        {
            "action_type": "compositor_dispatch",
            "dispatcher": "workspace",
            "args": "e+1",
            "source_device": "1234:5678",
            "source_button": "btn_back",
        },
    )

    await asyncio.sleep(0)
    listener.dispatch.assert_awaited_once_with("workspace", "e+1")


@pytest.mark.asyncio
async def test_handle_event_exec_ref_runs_command_once() -> None:
    manager = SessionManager()
    manager.exec_state.exec_refs[7] = "echo once"
    manager.action_handler.execute_command = AsyncMock(return_value=0)

    await session_events_module.handle_event(
        manager,
        CommandType.ACTION_TRIGGER,
        {
            "action_type": "exec",
            "exec_ref": 7,
            "source_device": "1234:5678",
            "source_button": "btn_side",
        },
    )

    await asyncio.sleep(0)
    manager.action_handler.execute_command.assert_awaited_once_with("echo once")


@pytest.mark.asyncio
async def test_handle_event_macro_async_exec_uses_exec_trigger_path() -> None:
    manager = SessionManager()
    manager.action_handler.handle_action = AsyncMock()
    manager.action_handler.execute_command_sync = Mock()

    await session_events_module.handle_event(
        manager,
        CommandType.ACTION_TRIGGER,
        {
            "action_type": "exec",
            "cmd": "echo macro",
            "macro_exec_async": True,
        },
    )

    await asyncio.sleep(0)
    manager.action_handler.handle_action.assert_not_awaited()
    manager.action_handler.execute_command_sync.assert_called_once_with("echo macro")


@pytest.mark.asyncio
async def test_device_disconnect_event_invalidates_cached_grabs_and_reevaluates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        name="Test Mouse"
    )
    manager.profile_state.grabbed_devices = {"1234:5678"}
    manager.profile_state.grabbed_interfaces = {"1234:5678": {"mouse": "/dev/input/event10"}}
    manager.profile_state.last_sent_mapping_signatures = {"1234:5678": "sig"}
    manager.profile_state.last_sent_combo_signature = "combo"
    manager.exec_state.device_exec_refs = {"1234:5678": {7}}
    manager.exec_state.combo_exec_refs = {8}
    manager.exec_state.exec_refs = {7: "echo device", 8: "echo combo"}
    monkeypatch.setattr(session_profiles_module, "reevaluate_profiles", AsyncMock())

    async def _instant_sleep(_delay: float) -> None:
        return

    monkeypatch.setattr(session_manager_module.asyncio, "sleep", _instant_sleep)

    await session_events_module.handle_event(
        manager,
        CommandType.DEVICE_DISCONNECTED,
        {"hardware_id": "1234:5678", "vendor_id": "1234", "product_id": "5678"},
    )

    task = manager.profile_state.topology_refresh_task
    assert task is not None
    await task

    assert manager.profile_state.grabbed_devices == set()
    assert manager.profile_state.grabbed_interfaces == {}
    assert manager.profile_state.last_sent_mapping_signatures == {}
    assert manager.profile_state.last_sent_combo_signature == ""
    assert manager.exec_state.exec_refs == {}
    session_profiles_module.reevaluate_profiles.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_topology_refresh_retries_after_reevaluate_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SessionManager()
    sleep_calls: list[float] = []
    reevaluate_profiles = AsyncMock(side_effect=[RuntimeError("refresh boom"), None])
    monkeypatch.setattr(session_profiles_module, "reevaluate_profiles", reevaluate_profiles)

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        return

    monkeypatch.setattr(session_manager_module.asyncio, "sleep", fake_sleep)

    with caplog.at_level("WARNING", logger="keyforge-session"):
        session_profiles_module.schedule_topology_refresh(
            manager,
            session_manager_module.TOPOLOGY_REFRESH_DEBOUNCE_S,
            session_manager_module.TOPOLOGY_REFRESH_RETRY_S,
        )
        task = manager.profile_state.topology_refresh_task
        assert task is not None
        await task

    assert sleep_calls == [
        session_manager_module.TOPOLOGY_REFRESH_DEBOUNCE_S,
        session_manager_module.TOPOLOGY_REFRESH_RETRY_S,
    ]
    assert "Topology refresh failed: refresh boom" in caplog.text
    assert reevaluate_profiles.await_count == 2
    assert manager.profile_state.topology_refresh_task is None


@pytest.mark.asyncio
async def test_get_status_uses_async_unlock_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = SessionManager()
    manager._security_policy.recording_unlock_required = True
    manager._security_policy.gui_allow_left_right_click_remap = True
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    resolve_unlock_status_async = AsyncMock(
        return_value={"unlocked": True, "source": "runtime", "expires_at": 1234}
    )
    monkeypatch.setattr(
        session_recording_module,
        "resolve_unlock_status_async",
        resolve_unlock_status_async,
    )

    async def _support_details(_compositor_id: str | None, _dbus=None) -> dict[str, bool | str]:
        return {"supported": False, "warning": ""}

    monkeypatch.setattr(
        session_compositor_module,
        "get_compositor_support_details",
        _support_details,
    )

    result = await manager._handle_session_request(
        {"command": "get_status"},
        "client",
        peer,
        writer,  # type: ignore[arg-type]
    )

    assert result["status"] == "ok"
    assert result["recording_unlocked"] is True
    assert result["recording_unlock_required"] is True
    assert result["gui_allow_left_right_click_remap"] is True
    assert result["recording_unlock_source"] == "runtime"
    assert result["recording_unlock_expires_at"] == 1234
    resolve_unlock_status_async.assert_awaited_once_with(manager, peer.uid)


@pytest.mark.asyncio
async def test_get_recording_settings_uses_unlock_and_owner_state_only() -> None:
    manager = SessionManager()
    manager._security_policy.recording_unlock_required = True
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    resolve_unlock_status_async = AsyncMock(
        return_value={"unlocked": True, "source": "runtime", "expires_at": 4321}
    )
    manager.unlock_state.refresh_owner = {
        "uid": peer.uid,
        "pid": peer.pid,
        "writer_id": id(writer),
        "lease_id": "lease-test",
    }
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        session_recording_module,
        "resolve_unlock_status_async",
        resolve_unlock_status_async,
    )

    result = await manager._handle_session_request(
        {"command": "get_recording_settings"},
        "client",
        peer,
        writer,  # type: ignore[arg-type]
    )

    assert result["status"] == "ok"
    assert result["recording_unlocked"] is True
    assert result["recording_unlock_required"] is True
    assert result["recording_refresh_owner"] is True
    assert "authorized" not in result
    resolve_unlock_status_async.assert_awaited_once_with(manager, peer.uid)
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_sensitive_recording_commands_do_not_require_owner_when_unlock_not_required() -> None:
    manager = SessionManager()
    manager._security_policy.recording_unlock_required = False
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    start_recording = AsyncMock(return_value={"status": "ok"})
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(session_recording_module, "start_recording", start_recording)

    result = await manager._handle_session_request(
        {"command": "start_recording"},
        "client",
        peer,
        writer,  # type: ignore[arg-type]
    )

    assert result == {"status": "ok"}
    start_recording.assert_awaited_once_with(manager, reset_if_active=False)
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_get_status_reports_effective_unlock_when_unlock_not_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager._security_policy.recording_unlock_required = False
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    resolve_unlock_status_async = AsyncMock(
        return_value={"unlocked": False, "source": "none", "expires_at": 0}
    )
    monkeypatch.setattr(
        session_recording_module,
        "resolve_unlock_status_async",
        resolve_unlock_status_async,
    )

    async def _support_details(_compositor_id: str | None, _dbus=None) -> dict[str, bool | str]:
        return {"supported": False, "warning": ""}

    monkeypatch.setattr(
        session_compositor_module,
        "get_compositor_support_details",
        _support_details,
    )

    result = await manager._handle_session_request(
        {"command": "get_status"},
        "client",
        peer,
        writer,  # type: ignore[arg-type]
    )

    assert result["status"] == "ok"
    assert result["recording_unlock_required"] is False
    assert result["recording_unlocked"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    ["begin_capture", "capture_read", "end_capture"],
)
async def test_capture_commands_with_owner_return_error_on_missing_hardware_id(
    command: str,
) -> None:
    manager = SessionManager()
    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    manager.unlock_state.refresh_owner = {
        "uid": peer.uid,
        "pid": peer.pid,
        "writer_id": id(writer),
        "lease_id": "lease-test",
    }

    result = await manager._handle_session_request(
        {"command": command},
        "client",
        peer,
        writer,
    )

    assert result == {"error": "missing hardware_id"}


@pytest.mark.asyncio
async def test_session_client_drops_connection_when_buffer_exceeds_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager.running = True
    reader = _FakeSessionReader([b"x" * (manager._MAX_SESSION_CLIENT_BUFFER_BYTES + 1)])
    writer = _FakeSessionWriter()
    manager._handle_session_request = AsyncMock(return_value={"status": "ok"})  # type: ignore[method-assign]

    monkeypatch.setattr(
        session_manager_module,
        "get_peer_credentials",
        lambda _sock: PeerCredentials(pid=321, uid=1000, gid=1000),
    )

    await manager._handle_session_client(reader, writer)  # type: ignore[arg-type]

    manager._handle_session_request.assert_not_awaited()
    assert writer.closed is True
    assert writer.writes == []


@pytest.mark.asyncio
async def test_handle_session_request_create_macro_broadcasts_saved_event() -> None:
    manager = SessionManager()
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"macro": {"name": "Speedrun"}})
    )
    manager._broadcast_to_session_clients = Mock()

    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    result = await manager._handle_session_request(
        {"command": "create_macro", "macro": {"name": "Speedrun"}},
        "client",
        peer,
        object(),
    )

    assert result == {"status": "ok", "macro": {"name": "Speedrun"}}
    manager._broadcast_to_session_clients.assert_called_once_with(
        {"event": "macro_saved", "name": "Speedrun"}
    )


def test_handle_device_grab_status_waiting_notifies_once_and_broadcasts() -> None:
    manager = SessionManager()
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        name="Test Keyboard"
    )
    manager._send_notification = Mock()
    manager._broadcast_to_session_clients = Mock()

    event = {
        "hardware_id": "1234:5678",
        "state": "waiting",
        "active_keys": ["key_l"],
        "waited_s": 1.2,
    }

    session_events_module.handle_device_grab_status_event(manager, event)
    session_events_module.handle_device_grab_status_event(manager, event)

    manager._send_notification.assert_called_once_with(
        "Keyforge: Grab Pending",
        "Test Keyboard: waiting for keys to be released (key_l).",
    )
    assert manager._broadcast_to_session_clients.call_args_list == [
        call({"event": "device_grab_status", **event}),
        call({"event": "device_grab_status", **event}),
    ]
    assert manager.profile_state.grab_waiting_devices == {"1234:5678"}


def test_handle_device_grab_status_timeout_notifies_and_schedules_retry() -> None:
    manager = SessionManager()
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        name="Test Keyboard"
    )
    manager._send_notification = Mock()
    manager._broadcast_to_session_clients = Mock()
    monkeypatch = pytest.MonkeyPatch()
    schedule_grab_retry = Mock()
    monkeypatch.setattr(session_profiles_module, "schedule_grab_retry", schedule_grab_retry)
    manager.profile_state.grab_waiting_devices.add("1234:5678")

    event = {
        "hardware_id": "1234:5678",
        "state": "timed_out",
        "active_keys": ["key_l"],
        "waited_s": 300.0,
    }

    try:
        session_events_module.handle_device_grab_status_event(manager, event)
    finally:
        monkeypatch.undo()

    manager._send_notification.assert_called_once_with(
        "Keyforge: Grab Timed Out",
        "Test Keyboard: keys stayed down too long (key_l). Retrying automatically.",
    )
    schedule_grab_retry.assert_called_once_with(
        manager,
        "1234:5678",
        session_events_module.GRAB_RETRY_DELAY_S,
    )
    manager._broadcast_to_session_clients.assert_called_once_with(
        {"event": "device_grab_status", **event}
    )
    assert manager.profile_state.grab_waiting_devices == set()


@pytest.mark.asyncio
async def test_send_notification_logs_even_when_dbus_notification_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SessionManager()
    manager.dbus.notify = AsyncMock(side_effect=RuntimeError("notification unavailable"))  # type: ignore[method-assign]

    with caplog.at_level("INFO", logger="keyforge-session"):
        manager._send_notification("Keyforge: Grab Pending", "Test Keyboard is waiting.")
        await asyncio.sleep(0)

    assert "Notification: Keyforge: Grab Pending: Test Keyboard is waiting." in caplog.text
    manager.dbus.notify.assert_awaited_once_with(  # type: ignore[attr-defined]
        "Keyforge: Grab Pending",
        "Test Keyboard is waiting.",
        app_name="keyforge",
        timeout_ms=2000,
    )


@pytest.mark.asyncio
async def test_capture_combo_session_command_round_trip() -> None:
    manager = SessionManager()
    manager.hardware.list_hardware_ids = lambda: ["1234:5678"]  # type: ignore[assignment]
    manager.profiles.get_profile = Mock(
        return_value=SimpleNamespace(
            config=SimpleNamespace(device_layers={"1234:5678": object()})
        )
    )
    manager.client.send_command = AsyncMock(
        return_value=Response(
            status="ok",
            data={
                "events": [
                    {
                        "evdev": "key_a",
                        "hardware_id": "1234:5678",
                        "source": "kbd",
                    }
                ],
                "warnings": [],
            },
        )
    )

    peer = PeerCredentials(pid=1, uid=1000, gid=1000)
    writer = object()
    manager.unlock_state.refresh_owner = {
        "uid": peer.uid,
        "pid": peer.pid,
        "writer_id": id(writer),
        "lease_id": "lease-test",
    }

    capture = await manager._handle_session_request(
        {"command": "capture_combo", "profile_name": "Desktop"},
        "client",
        peer,
        writer,
    )

    assert capture == {
        "status": "ok",
        "events": [{"evdev": "key_a", "hardware_id": "1234:5678", "source": "kbd"}],
        "warnings": [],
    }


@pytest.mark.asyncio
async def test_capture_combo_uses_all_known_hardware_ids_not_just_profile_layers() -> None:
    manager = SessionManager()
    manager.hardware.list_hardware_ids = lambda: ["1234:5678", "9999:0001"]  # type: ignore[assignment]
    manager.profiles.get_profile = Mock(
        return_value=SimpleNamespace(
            config=SimpleNamespace(device_layers={"1234:5678": object()}, combos=[])
        )
    )
    manager.client.send_command = AsyncMock(
        return_value=Response(
            status="ok",
            data={
                "events": [
                    {
                        "evdev": "alt",
                        "hardware_id": "9999:0001",
                        "source": "kbd-left",
                    },
                    {
                        "evdev": "key_7",
                        "hardware_id": "1234:5678",
                        "source": "kbd-right",
                    },
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
async def test_reevaluate_profiles_sends_combo_payload_and_forces_combo_grab() -> None:
    manager = SessionManager()
    hardware_id = "1234:5678"
    profile = ProfileConfig(name="Desktop", enabled=True, is_permanent=True)

    manager.hardware.list_hardware_ids = lambda: [hardware_id]  # type: ignore[assignment]
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        hardware_id=hardware_id,
        name="Test Mouse",
        evdev_devices=[SimpleNamespace(id="mouse", path="/dev/input/event10")],
        buttons=[SimpleNamespace(id="btn_side", evdev="btn_side", source="mouse")],
    )
    manager.profiles.resolve_active_profiles = lambda *_args, **_kwargs: ResolvedProfiles(  # type: ignore[assignment]
        active_profiles=[profile],
        devices={
            hardware_id: ResolvedDeviceProfile(
                hardware_id=hardware_id,
                active_profile_names=["Desktop"],
                combo_event_count=1,
                combo_sources={"mouse"},
            )
        },
        combos=[
            ResolvedCombo(
                id="combo-1",
                name="Quick Toggle",
                steps=[
                    ComboStep(
                        events=[
                            ComboEvent(
                                hardware_id=hardware_id,
                                source="mouse",
                                evdev="btn_side",
                            )
                        ],
                        timeout_ms=750,
                    )
                ],
                action=MappingAction(
                    action_type=ActionType.PROFILE_TOGGLE,
                    profile_name="Gaming",
                ),
                profile_name="Desktop",
            )
        ],
    )
    manager.client.send_command = AsyncMock(
        side_effect=[
            Response(status="ok", data={"grabbed_count": 1}),
            Response(status="ok", data={"updated": True}),
            Response(status="ok", data={"updated": True, "combo_count": 1}),
        ]
    )

    await session_profiles_module.reevaluate_profiles(manager)

    sent = manager.client.send_command.await_args_list
    assert [call.args[0].command for call in sent] == [
        CommandType.GRAB_DEVICE,
        CommandType.SET_MAPPING,
        CommandType.SET_COMBOS,
    ]
    assert sent[0].args[0].data["force_grab_unmapped"] is True
    assert sent[2].args[0].data["combos"][0]["action"]["profile_name"] == "Gaming"
    assert sent[2].args[0].data["combos"][0]["steps"][0]["timeout_ms"] == 750


@pytest.mark.asyncio
async def test_reevaluate_profiles_skips_unchanged_mapping_and_combos() -> None:
    manager = SessionManager()
    hardware_id = "1234:5678"
    profile = ProfileConfig(
        name="Desktop",
        enabled=True,
        is_permanent=True,
        device_layers={hardware_id: DeviceProfileLayer(hardware_id=hardware_id)},
    )

    manager.hardware.list_hardware_ids = lambda: [hardware_id]  # type: ignore[assignment]
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        hardware_id=hardware_id,
        name="Test Mouse",
        evdev_devices=[SimpleNamespace(id="mouse", path="/dev/input/event10")],
        buttons=[SimpleNamespace(id="btn_side", evdev="btn_side", source="mouse")],
    )
    manager.profiles.resolve_active_profiles = lambda *_args, **_kwargs: ResolvedProfiles(  # type: ignore[assignment]
        active_profiles=[profile],
        devices={
            hardware_id: ResolvedDeviceProfile(
                hardware_id=hardware_id,
                active_profile_names=["Desktop"],
                mappings={
                    "btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_f13")
                },
                combo_event_count=1,
                combo_sources={"mouse"},
            )
        },
        combos=[
            ResolvedCombo(
                id="combo-1",
                name="Quick Toggle",
                steps=[
                    ComboStep(
                        events=[
                            ComboEvent(
                                hardware_id=hardware_id,
                                source="mouse",
                                evdev="btn_side",
                            )
                        ],
                        timeout_ms=750,
                    )
                ],
                action=MappingAction(
                    action_type=ActionType.PROFILE_TOGGLE,
                    profile_name="Gaming",
                ),
                profile_name="Desktop",
            )
        ],
    )
    manager.client.send_command = AsyncMock(
        side_effect=[
            Response(status="ok", data={"grabbed_count": 1}),
            Response(status="ok", data={"updated": True}),
            Response(status="ok", data={"updated": True, "combo_count": 1}),
        ]
    )

    await session_profiles_module.reevaluate_profiles(manager)
    await session_profiles_module.reevaluate_profiles(manager)

    sent = manager.client.send_command.await_args_list
    assert [call.args[0].command for call in sent] == [
        CommandType.GRAB_DEVICE,
        CommandType.SET_MAPPING,
        CommandType.SET_COMBOS,
    ]


@pytest.mark.asyncio
async def test_apply_resolved_device_profile_uses_extended_grab_timeout() -> None:
    manager = SessionManager()
    hardware_id = "1234:5678"
    resolved = ResolvedDeviceProfile(
        hardware_id=hardware_id,
        active_profile_names=["Desktop"],
        mappings={
            "btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_f13")
        },
    )
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        hardware_id=hardware_id,
        name="Test Mouse",
        evdev_devices=[SimpleNamespace(id="mouse", path="/dev/input/event10")],
        buttons=[SimpleNamespace(id="btn_side", evdev="btn_side", source="mouse")],
    )
    manager.client.send_command = AsyncMock(
        side_effect=[
            Response(status="ok", data={"grabbed_count": 1}),
            Response(status="ok", data={"updated": True}),
        ]
    )

    await session_profiles_module.apply_resolved_device_profile(manager, hardware_id, resolved)

    sent = manager.client.send_command.await_args_list
    assert sent[0].args[0].command == CommandType.GRAB_DEVICE
    assert sent[0].kwargs["timeout"] == session_manager_module.GRAB_DEVICE_TIMEOUT_S


@pytest.mark.asyncio
async def test_apply_resolved_device_profile_retries_after_grab_timeout() -> None:
    manager = SessionManager()
    hardware_id = "1234:5678"
    resolved = ResolvedDeviceProfile(
        hardware_id=hardware_id,
        active_profile_names=["Desktop"],
        mappings={
            "btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_f13")
        },
    )
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        hardware_id=hardware_id,
        name="Test Mouse",
        evdev_devices=[SimpleNamespace(id="mouse", path="/dev/input/event10")],
        buttons=[SimpleNamespace(id="btn_side", evdev="btn_side", source="mouse")],
    )
    manager.client.send_command = AsyncMock(side_effect=TimeoutError())
    manager._send_notification = Mock()
    schedule_grab_retry = Mock()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(session_profiles_module, "schedule_grab_retry", schedule_grab_retry)

    try:
        await session_profiles_module.apply_resolved_device_profile(manager, hardware_id, resolved)
    finally:
        monkeypatch.undo()

    manager._send_notification.assert_called_once_with(
        "Keyforge: Grab Timed Out",
        (
            "Test Mouse: grab timed out while waiting for keys to be released. "
            "Retrying automatically."
        ),
    )
    schedule_grab_retry.assert_called_once_with(
        manager,
        hardware_id,
        delay_s=session_profiles_module.GRAB_RETRY_DELAY_S,
    )


@pytest.mark.asyncio
async def test_apply_resolved_device_profile_waits_when_daemon_reports_no_live_interfaces() -> None:
    manager = SessionManager()
    hardware_id = "1234:5678"
    resolved = ResolvedDeviceProfile(
        hardware_id=hardware_id,
        active_profile_names=["Desktop"],
        mappings={
            "btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_f13")
        },
    )
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        hardware_id=hardware_id,
        name="Test Mouse",
        evdev_devices=[SimpleNamespace(id="mouse", path="/dev/input/by-id/test-mouse")],
        buttons=[SimpleNamespace(id="btn_side", evdev="btn_side", source="mouse")],
    )
    manager.client.send_command = AsyncMock(
        return_value=Response(status="ok", data={"grabbed_count": 0, "waiting_for_device": True})
    )

    await session_profiles_module.apply_resolved_device_profile(manager, hardware_id, resolved)

    manager.client.send_command.assert_awaited_once()
    assert hardware_id not in manager.profile_state.grabbed_devices
    assert hardware_id not in manager.profile_state.grabbed_interfaces
    assert manager.profile_state.last_sent_mapping_signatures == {}


@pytest.mark.asyncio
async def test_apply_resolved_device_profile_skips_same_interface_noop_without_mapping_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SessionManager()
    hardware_id = "1234:5678"
    resolved = ResolvedDeviceProfile(
        hardware_id=hardware_id,
        active_profile_names=["Desktop"],
        mappings={
            "btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_f13")
        },
    )
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        hardware_id=hardware_id,
        name="Test Mouse",
        evdev_devices=[SimpleNamespace(id="mouse", path="/dev/input/event10")],
        buttons=[SimpleNamespace(id="btn_side", evdev="btn_side", source="mouse")],
    )
    manager.profile_state.resolved_devices[hardware_id] = ResolvedDeviceProfile(
        hardware_id=hardware_id,
        active_profile_names=["Desktop"],
        mappings=dict(resolved.mappings),
    )
    manager.profile_state.grabbed_devices.add(hardware_id)
    manager.profile_state.grabbed_interfaces[hardware_id] = {"mouse": "/dev/input/event10"}
    manager.profile_state.last_sent_mapping_signatures[
        hardware_id
    ] = session_payloads_module.resolved_mapping_signature(
        manager,
        resolved,
        hardware_id,
    )
    update_mapping = AsyncMock(return_value=True)
    maybe_notify = Mock()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(session_profiles_module, "update_mapping", update_mapping)
    monkeypatch.setattr(session_profiles_module, "maybe_notify_profile_activation", maybe_notify)

    try:
        with caplog.at_level("INFO", logger="keyforge-session"):
            await session_profiles_module.apply_resolved_device_profile(
                manager,
                hardware_id,
                resolved,
            )
    finally:
        monkeypatch.undo()

    update_mapping.assert_not_awaited()
    maybe_notify.assert_called_once_with(
        manager,
        "Test Mouse",
        ["Desktop"],
        resolved,
    )
    assert "Same interfaces for 1234:5678, updating mapping only" not in caplog.text


@pytest.mark.asyncio
async def test_apply_resolved_device_profile_skips_profile_only_change_without_mapping_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SessionManager()
    hardware_id = "1234:5678"
    resolved = ResolvedDeviceProfile(
        hardware_id=hardware_id,
        active_profile_names=["Desktop", "Games"],
        mappings={
            "btn_side": MappingAction(action_type=ActionType.KEYBOARD, target="key_f13")
        },
        notify_profiles=["Games"],
    )
    manager.hardware.get_hardware = lambda _hardware_id: SimpleNamespace(  # type: ignore[assignment]
        hardware_id=hardware_id,
        name="Test Mouse",
        evdev_devices=[SimpleNamespace(id="mouse", path="/dev/input/event10")],
        buttons=[SimpleNamespace(id="btn_side", evdev="btn_side", source="mouse")],
    )
    manager.profile_state.resolved_devices[hardware_id] = ResolvedDeviceProfile(
        hardware_id=hardware_id,
        active_profile_names=["Desktop"],
        mappings=dict(resolved.mappings),
    )
    manager.profile_state.grabbed_devices.add(hardware_id)
    manager.profile_state.grabbed_interfaces[hardware_id] = {"mouse": "/dev/input/event10"}
    manager.profile_state.last_sent_mapping_signatures[
        hardware_id
    ] = session_payloads_module.resolved_mapping_signature(
        manager,
        resolved,
        hardware_id,
    )
    update_mapping = AsyncMock(return_value=True)
    maybe_notify = Mock()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(session_profiles_module, "update_mapping", update_mapping)
    monkeypatch.setattr(session_profiles_module, "maybe_notify_profile_activation", maybe_notify)

    try:
        with caplog.at_level("INFO", logger="keyforge-session"):
            await session_profiles_module.apply_resolved_device_profile(
                manager,
                hardware_id,
                resolved,
            )
    finally:
        monkeypatch.undo()

    update_mapping.assert_not_awaited()
    maybe_notify.assert_called_once_with(
        manager,
        "Test Mouse",
        ["Desktop"],
        resolved,
    )
    assert "Same interfaces for 1234:5678, updating mapping only" not in caplog.text


@pytest.mark.asyncio
async def test_claim_recording_unlock_refresh_creates_runtime_lease(
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

    def _resolve(_uid: int) -> dict:
        return {"unlocked": True, "source": "runtime", "expires_at": 5000}

    monkeypatch.setattr(session_recording_module, "resolve_unlock_status", _resolve)

    result = await session_recording_module.claim_recording_unlock_refresh(manager, peer, writer)

    assert result == {
        "status": "error",
        "error_code": "recording_refresh_reclaim_denied",
        "message": (
            "recording_refresh_denied: runtime lease already claimed; "
            "unlock again to re-establish owner"
        ),
    }
