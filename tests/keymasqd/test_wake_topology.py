"""Wake checks both current discovery and the old physical input fd."""

import asyncio
import errno
import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from keymasq.common.ipc import CommandType
from keymasq.keymasqd.device_manager import DeviceManager, _topology_runtime_deps
from keymasq.keymasqd.runtime import topology


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["present", "missing", "replaced", "dead_fd", "stopped"])
async def test_wake_reconciles_reader_even_when_discovery_path_is_unchanged(state):
    manager = DeviceManager()
    info = topology.LiveInterfaceInfo(
        hardware_id="1234:5678",
        vendor_id="1234",
        product_id="5678",
        stable_path="/dev/input/by-id/keyboard",
        path="/dev/input/event5",
        interface_id="kbd",
    )
    physical = SimpleNamespace(active_keys=Mock(return_value=[]))
    if state == "dead_fd":
        physical.active_keys.side_effect = OSError(errno.ENODEV, "device removed")
    device = SimpleNamespace(
        path=info.stable_path,
        resolved_event_path=info.path,
        running=state != "stopped",
        task=SimpleNamespace(done=lambda: False),
        device=physical,
    )
    manager.grabbed_devices[info.hardware_id] = [device]
    manager.grab_state.desired_grabs[info.hardware_id] = object()
    manager.topology_state.reconciled_snapshot = {info.stable_path: info}
    snapshot = (
        {}
        if state == "missing"
        else {
            info.stable_path: replace(info, path="/dev/input/event6")
            if state == "replaced"
            else info
        }
    )
    release = AsyncMock()
    manager.broadcast_callback = AsyncMock()
    deps = replace(_topology_runtime_deps(), release_interface_fn=release)
    await topology.reconcile_topology(
        manager, snapshot, log=logging.getLogger("test"), deps=deps, validate_readers=True
    )
    if state == "present":
        release.assert_not_awaited()
    else:
        release.assert_awaited_once_with(manager, info.hardware_id, info.stable_path)
    event_types = [call.args[0] for call in manager.broadcast_callback.await_args_list]
    assert (CommandType.DEVICE_CONNECTED in event_types) == (state != "missing")


@pytest.mark.asyncio
async def test_wake_forces_reconcile_with_unchanged_snapshot(monkeypatch):
    manager = DeviceManager(topology_poll_s=0.05)
    manager.resume_runtime_input()
    reconciled = asyncio.Event()

    async def reconcile(*_args, **kwargs):
        assert kwargs["validate_readers"]
        reconciled.set()

    monkeypatch.setattr(topology, "_scan_live_interfaces", AsyncMock(return_value={}))
    monkeypatch.setattr(topology, "reconcile_topology", reconcile)
    task = asyncio.create_task(
        topology.topology_watch_loop(
            manager, log=logging.getLogger("test"), deps=_topology_runtime_deps()
        )
    )
    try:
        await asyncio.wait_for(reconciled.wait(), 1)
        assert not manager.topology_state.wake_reconcile_pending
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
