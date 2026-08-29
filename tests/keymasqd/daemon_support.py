from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from keymasq.keymasqd import daemon
from keymasq.keymasqd.socket_server import ClientContext


def macro_meta(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "events": [{"type": 1, "code": 30, "value": 1, "t_us": 0}],
        "loop_mode": "count",
        "loop_count": 3,
        "loop_stop_behavior": "cancel_run",
        "move_to_start": True,
        "start_x": 111,
        "start_y": 222,
        "block_mouse_movement": True,
    }
    payload.update(overrides)
    return payload


def make_daemon_testbed(monkeypatch):
    device_manager = SimpleNamespace(
        grab_device=AsyncMock(return_value={"grabbed": True}),
        release_device=AsyncMock(return_value={"released": True}),
        set_mapping=AsyncMock(return_value={"updated": True}),
        set_combos=AsyncMock(return_value={"updated": True, "combo_count": 0}),
        set_cursor_position=AsyncMock(return_value={"status": "ok", "x": 0, "y": 0}),
        list_devices=AsyncMock(return_value={"devices": []}),
        device_runtime_status=AsyncMock(
            return_value={"status": "ok", "interfaces": [], "grabbed_interfaces": []}
        ),
        begin_combo_capture=Mock(return_value={"token": "combo-token"}),
        read_combo_capture=Mock(return_value={"event": None}),
        end_combo_capture=Mock(return_value={"status": "ok", "ended": True}),
        grabbed_devices={},
        play_macro=AsyncMock(return_value={"played": True}),
        cancel_macro_playback=AsyncMock(return_value={"canceled": True}),
        emergency_reset=AsyncMock(return_value={"reset": True}),
        set_diagnostics=AsyncMock(return_value={"status": "ok"}),
        start_device_inspector=AsyncMock(return_value={"status": "ok", "active": True}),
        stop_device_inspector=AsyncMock(return_value={"status": "ok", "active": False}),
        enable_device_inspector_suppression=AsyncMock(
            return_value={"status": "ok", "suppressed": True}
        ),
        disable_device_inspector_suppression=AsyncMock(
            return_value={"status": "ok", "suppressed": False}
        ),
        complete_macro_exec_wait=Mock(return_value={"completed": True}),
        initialize_output_devices=Mock(return_value=None),
        shutdown_output_devices=Mock(return_value=None),
        start_topology_watcher=AsyncMock(return_value=None),
        stop_topology_watcher=AsyncMock(return_value=None),
        prepare_for_sleep=AsyncMock(return_value=None),
        resume_from_sleep=AsyncMock(return_value=None),
        release_all_devices=AsyncMock(return_value=None),
    )
    recording_manager = SimpleNamespace(
        abort=AsyncMock(return_value=None),
        start=AsyncMock(return_value={"recording": "started"}),
        stop=AsyncMock(return_value={"recording": "stopped"}),
        list_pending_recordings=AsyncMock(return_value=[]),
        claim_pending_recording=AsyncMock(),
        release_pending_recording_claim=AsyncMock(return_value=None),
        discard_pending_recording=AsyncMock(return_value=None),
        discard_all_pending_recordings=AsyncMock(return_value=None),
        load_persisted_slot_recordings=AsyncMock(return_value=None),
        cleanup_spool_dir=Mock(return_value=None),
    )
    macro_store = SimpleNamespace(
        get=Mock(return_value={"events": []}),
        get_meta=Mock(return_value={"events": []}),
        probe_revision=Mock(return_value=None),
        open_snapshot=Mock(
            return_value=SimpleNamespace(
                meta={"event_count": 0, "duration_us": 0},
                iter_events=lambda: iter(()),
                revision=None,
            )
        ),
        list_meta=Mock(return_value=[]),
        create=Mock(return_value={"name": "new"}),
        create_from_events=Mock(return_value={"name": "new"}),
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
        authorize_combo_capture=Mock(return_value=object()),
        begin_combo=Mock(return_value={"token": "combo-token", "warnings": []}),
        read_combo=Mock(return_value={"event": None}),
        read_combo_nowait=Mock(return_value={"event": None}),
        register_combo_notifier=Mock(return_value=None),
        close_all=Mock(return_value=0),
    )

    monkeypatch.setattr(daemon, "DeviceManager", lambda verbosity=0: device_manager)
    monkeypatch.setattr(daemon, "RecordingManager", lambda: recording_manager)
    monkeypatch.setattr(daemon, "MacroStore", lambda _path: macro_store)
    monkeypatch.setattr(daemon, "CaptureManager", lambda: capture_manager)
    sleep_coordinator = SimpleNamespace(
        start=AsyncMock(return_value=True),
        stop=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        daemon,
        "LogindSleepCoordinator",
        lambda _prepare, _resume: sleep_coordinator,
    )

    daemon_instance = daemon.Daemon()
    return daemon_instance, device_manager, recording_manager, macro_store, capture_manager


def client_context(
    *,
    uid: int = 1000,
    pid: int = 4321,
    connection_id: int = 77,
) -> ClientContext:
    return ClientContext(
        connection_id=connection_id,
        pid=pid,
        uid=uid,
        gid=uid,
    )
