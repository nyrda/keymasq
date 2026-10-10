import asyncio
import errno
import logging
from dataclasses import replace
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from keymasq.common.ipc import CommandType
from keymasq.common.model.core import DeviceType
from keymasq.keymasqd import device_manager
from keymasq.keymasqd.device_manager import DeviceManager, _topology_runtime_deps
from keymasq.keymasqd.runtime import device_path_resolver, topology
from keymasq.keymasqd.runtime.grab import acquisition
from keymasq.keymasqd.runtime.grab.state import GrabDeviceDeps

INFO = topology.LiveInterfaceInfo(
    hardware_id="28de:1205",
    vendor_id="28de",
    product_id="1205",
    stable_path="/dev/input/by-id/deck-event-joystick",
    path="/dev/input/event5",
    interface_id="gamepad",
    instance="input5",
)


def grabbed(info: topology.LiveInterfaceInfo) -> SimpleNamespace:
    return SimpleNamespace(
        path=info.stable_path,
        stable_path=info.stable_path,
        resolved_event_path=info.path,
        interface_id=info.interface_id,
        running=True,
        task=None,
        device=None,
        stop_event_loop=AsyncMock(),
        release_tracked_outputs=Mock(),
        release=AsyncMock(),
    )


def runtime_disconnect(manager: DeviceManager):
    deps = GrabDeviceDeps(
        desired_grab_config_cls=Mock(),
        clear_device_path_cache_fn=lambda: None,
        resolve_stable_path_fn=lambda path: path,
        device_path_resolver_deps=device_path_resolver.DevicePathResolverDeps(
            device_paths_fn=lambda: [],
            device_input_fn=Mock(),
            detect_input_classes_fn=lambda _device: [],
            primary_input_class_fn=lambda _types: DeviceType.GAMEPAD,
        ),
        grabbed_device_cls=Mock(),
        get_interface_id_fn=lambda _path: "",
        str_value_fn=str,
        int_value_fn=int,
        fire_and_observe_fn=lambda coro, _label: asyncio.ensure_future(coro),
        errno_mod=errno,
    )
    return acquisition.build_runtime_callbacks(manager, deps).runtime_disconnect_callback


async def broadcasts_while_watching(
    manager: DeviceManager,
    monkeypatch: pytest.MonkeyPatch,
    snapshot: dict[str, topology.LiveInterfaceInfo],
    duration_s: float,
) -> list[tuple[CommandType, dict]]:
    events: list[tuple[CommandType, dict]] = []

    async def broadcast(event_type, payload):
        events.append((event_type, payload))

    manager.broadcast_callback = broadcast
    monkeypatch.setattr(topology, "_scan_live_interfaces", AsyncMock(return_value=snapshot))
    task = asyncio.create_task(
        topology.topology_watch_loop(
            manager,
            log=logging.getLogger("test"),
            deps=replace(_topology_runtime_deps(), fingerprint_fn=None),
        )
    )
    try:
        await asyncio.sleep(duration_s)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    return events


def steady_manager(desired: bool = True) -> DeviceManager:
    manager = DeviceManager(topology_poll_s=0.01, topology_debounce_s=0.05)
    manager.grabbed_devices[INFO.hardware_id] = [grabbed(INFO)]
    if desired:
        manager.grab_state.desired_grabs[INFO.hardware_id] = None
        manager.grab_state.desired_paths[INFO.hardware_id] = {INFO.stable_path}
    manager.topology_state.live_snapshot = {INFO.stable_path: INFO}
    manager.topology_state.reconciled_snapshot = {INFO.stable_path: INFO}
    return manager


@pytest.mark.parametrize("desired", [True, False])
async def test_reader_failure_announces_unchanged_device_once(monkeypatch, desired):
    manager = steady_manager(desired)
    await runtime_disconnect(manager)(INFO.hardware_id, INFO.stable_path)
    assert INFO.hardware_id not in manager.grabbed_devices

    events = await broadcasts_while_watching(manager, monkeypatch, {INFO.stable_path: INFO}, 0.2)

    expected = [(CommandType.DEVICE_CONNECTED, topology.live_interface_payload(INFO))]
    assert events == (expected if desired else [])


async def test_reader_failure_of_an_unplugged_device_only_reports_the_disconnect(monkeypatch):
    manager = steady_manager()
    await runtime_disconnect(manager)(INFO.hardware_id, INFO.stable_path)

    events = await broadcasts_while_watching(manager, monkeypatch, {}, 0.2)

    assert events == [(CommandType.DEVICE_DISCONNECTED, topology.live_interface_payload(INFO))]


async def test_repeated_reader_failures_back_off(monkeypatch):
    monkeypatch.setattr(topology, "REGRAB_RETRY_S", 0.3)
    manager = steady_manager()
    disconnect = runtime_disconnect(manager)
    await disconnect(INFO.hardware_id, INFO.stable_path)
    first = await broadcasts_while_watching(manager, monkeypatch, {INFO.stable_path: INFO}, 0.1)
    manager.grabbed_devices[INFO.hardware_id] = [grabbed(INFO)]
    await disconnect(INFO.hardware_id, INFO.stable_path)

    early = await broadcasts_while_watching(manager, monkeypatch, {INFO.stable_path: INFO}, 0.1)
    late = await broadcasts_while_watching(manager, monkeypatch, {INFO.stable_path: INFO}, 0.4)

    assert len(first) == 1
    assert early == []
    assert late == first


@pytest.mark.parametrize(
    ("instance", "expected"),
    [
        ("input9", [CommandType.DEVICE_DISCONNECTED, CommandType.DEVICE_CONNECTED]),
        ("input5", []),
        ("", []),
    ],
)
async def test_replug_at_the_same_node_reconnects_desired_hardware(instance, expected):
    manager = DeviceManager()
    manager.grab_state.desired_grabs[INFO.hardware_id] = None
    manager.topology_state.reconciled_snapshot = {INFO.stable_path: INFO}
    manager.broadcast_callback = AsyncMock()

    await topology.reconcile_topology(
        manager,
        {INFO.stable_path: replace(INFO, instance=instance)},
        log=logging.getLogger("test"),
        deps=_topology_runtime_deps(),
    )

    assert [call.args[0] for call in manager.broadcast_callback.await_args_list] == expected


def test_live_scan_records_the_kernel_input_registration(monkeypatch, tmp_path):
    class _Pad:
        name = "Steam Deck"
        phys = "usb-1"
        info = SimpleNamespace(vendor=0x28DE, product=0x1205)

        def capabilities(self):
            return {}

    node = tmp_path / "class/input/event5"
    node.mkdir(parents=True)
    (node / "device").symlink_to("../../input42")
    monkeypatch.setattr(
        topology, "input_instance", partial(topology.input_instance, sys_root=str(tmp_path))
    )
    native = "/dev/keymasq-sources/deck/hid-bpf"

    snapshot = topology.scan_live_interfaces_sync(
        clear_device_path_cache_fn=lambda: None,
        device_paths_fn=lambda: [INFO.path, "/dev/input/event6", native],
        device_input_fn=lambda _path: _Pad(),
        detect_input_classes_fn=lambda _device: ["gamepad"],
        primary_input_class_fn=lambda _classes: DeviceType.GAMEPAD,
        resolve_stable_path_fn=lambda path: path,
        get_interface_id_fn=lambda _stable_path: "",
        log=device_manager.log,
    )

    assert {path: info.instance for path, info in snapshot.items()} == {
        INFO.path: "input42",
        "/dev/input/event6": "",
        native: "",
    }
