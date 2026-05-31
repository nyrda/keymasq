# pyright: reportUnusedImport=false, reportUnusedFunction=false, reportUnusedClass=false
# ruff: noqa: F401, I001
from __future__ import annotations

import asyncio
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

import keymasq.keymasqd.daemon as daemon_module
import keymasq.keymasqd.daemon_capture_commands as daemon_capture_commands
import keymasq.keymasqd.daemon_macro_commands as daemon_macro_commands
from keymasq.common.ipc import CommandType
from keymasq.common.security import SecurityPolicy
from keymasq.keymasqd.socket_server import ClientContext


@pytest.fixture
def daemon_testbed(monkeypatch):
    device_manager = SimpleNamespace(
        grab_device=AsyncMock(return_value={"grabbed": True}),
        release_device=AsyncMock(return_value={"released": True}),
        set_mapping=AsyncMock(return_value={"updated": True}),
        set_combos=AsyncMock(return_value={"updated": True, "combo_count": 0}),
        set_cursor_position=AsyncMock(return_value={"status": "ok", "x": 0, "y": 0}),
        list_devices=AsyncMock(return_value={"devices": []}),
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
        release_all_devices=AsyncMock(return_value=None),
    )
    recording_manager = SimpleNamespace(
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


__all__ = [
    "asyncio",
    "Enum",
    "Path",
    "SimpleNamespace",
    "cast",
    "AsyncMock",
    "Mock",
    "pytest",
    "daemon_module",
    "daemon_capture_commands",
    "daemon_macro_commands",
    "CommandType",
    "SecurityPolicy",
    "ClientContext",
    "daemon_testbed",
    "_client",
]
