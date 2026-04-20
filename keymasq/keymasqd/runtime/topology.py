import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from keymasq.common.ipc import CommandType
from keymasq.keymasqd.runtime import adapters as runtime_adapters

type JsonObject = dict[str, object]
type Snapshot = dict[str, Any]
type _TopologyManager = Any
type ClearDevicePathCacheFn = Callable[[], None]
type DevicePathsFn = Callable[[], list[str]]
type DeviceInputFn = Callable[[str], Any]
type ResolveStablePathFn = Callable[[str], str]
type GetInterfaceIdFn = Callable[[str], str | None]
type ReleaseInterfaceFn = Callable[[Any, str, str], Awaitable[None]]


@dataclass(frozen=True)
class LiveInterfaceInfo:
    hardware_id: str
    vendor_id: str
    product_id: str
    stable_path: str
    path: str
    interface_id: str


@dataclass(frozen=True)
class TopologyRuntimeDeps:
    asyncio_mod: runtime_adapters.AsyncioRuntimeAdapter
    clear_device_path_cache_fn: ClearDevicePathCacheFn
    device_paths_fn: DevicePathsFn
    device_input_fn: DeviceInputFn
    resolve_stable_path_fn: ResolveStablePathFn
    get_interface_id_fn: GetInterfaceIdFn
    release_interface_fn: ReleaseInterfaceFn


async def start_topology_watcher(
    manager: _TopologyManager,
    *,
    log: logging.Logger,
    deps: TopologyRuntimeDeps,
) -> None:
    asyncio_mod = deps.asyncio_mod
    if (
        manager.topology_state.watcher_task is not None
        and not manager.topology_state.watcher_task.done()
    ):
        return
    snapshot = await asyncio_mod.to_thread(
        scan_live_interfaces_sync,
        clear_device_path_cache_fn=deps.clear_device_path_cache_fn,
        device_paths_fn=deps.device_paths_fn,
        device_input_fn=deps.device_input_fn,
        resolve_stable_path_fn=deps.resolve_stable_path_fn,
        get_interface_id_fn=deps.get_interface_id_fn,
        log=log,
    )
    manager.topology_state.live_snapshot = dict(snapshot)
    manager.topology_state.reconciled_snapshot = dict(snapshot)
    manager.topology_state.watcher_task = asyncio_mod.create_task(
        topology_watch_loop(
            manager,
            log=log,
            deps=deps,
        )
    )


async def stop_topology_watcher(
    manager: _TopologyManager,
    *,
    deps: TopologyRuntimeDeps,
) -> None:
    task = manager.topology_state.watcher_task
    manager.topology_state.watcher_task = None
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    reconcile_task = manager.topology_state.reconcile_task
    manager.topology_state.reconcile_task = None
    if reconcile_task is not None and not reconcile_task.done():
        reconcile_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reconcile_task


async def topology_watch_loop(
    manager: _TopologyManager,
    *,
    log: logging.Logger,
    deps: TopologyRuntimeDeps,
) -> None:
    asyncio_mod = deps.asyncio_mod
    try:
        while True:
            await asyncio_mod.sleep(manager.topology_state.poll_s)
            try:
                snapshot = await asyncio_mod.to_thread(
                    scan_live_interfaces_sync,
                    clear_device_path_cache_fn=deps.clear_device_path_cache_fn,
                    device_paths_fn=deps.device_paths_fn,
                    device_input_fn=deps.device_input_fn,
                    resolve_stable_path_fn=deps.resolve_stable_path_fn,
                    get_interface_id_fn=deps.get_interface_id_fn,
                    log=log,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Topology scan failed: %s", exc)
                continue

            if snapshot != manager.topology_state.live_snapshot:
                manager.topology_state.live_snapshot = dict(snapshot)
                schedule_topology_reconcile(
                    manager,
                    snapshot,
                    log=log,
                    deps=deps,
                )
                continue

            if snapshot != manager.topology_state.reconciled_snapshot and (
                manager.topology_state.reconcile_task is None
                or manager.topology_state.reconcile_task.done()
            ):
                schedule_topology_reconcile(
                    manager,
                    snapshot,
                    log=log,
                    deps=deps,
                )
    except asyncio.CancelledError:
        raise


def schedule_topology_reconcile(
    manager: _TopologyManager,
    snapshot: Snapshot,
    *,
    log: logging.Logger,
    deps: TopologyRuntimeDeps,
) -> None:
    asyncio_mod = deps.asyncio_mod
    task = manager.topology_state.reconcile_task
    if task is not None and not task.done():
        task.cancel()

    async def _run() -> None:
        try:
            await asyncio_mod.sleep(manager.topology_state.debounce_s)
            await reconcile_topology(manager, snapshot, log=log, deps=deps)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Topology reconcile failed: %s", exc)
        finally:
            current = manager.topology_state.reconcile_task
            if current is asyncio_mod.current_task():
                manager.topology_state.reconcile_task = None

    manager.topology_state.reconcile_task = asyncio_mod.create_task(_run())


async def reconcile_topology(
    manager: _TopologyManager,
    snapshot: Snapshot,
    *,
    log: logging.Logger,
    deps: TopologyRuntimeDeps,
) -> None:
    async with manager._op_lock:
        previous = dict(manager.topology_state.reconciled_snapshot)
        desired_hardware_ids = set(manager.grab_state.desired_grabs)
        events = build_topology_events(manager, previous, snapshot, desired_hardware_ids)
        await reconcile_topology_unlocked(manager, snapshot, deps=deps)
        manager.topology_state.reconciled_snapshot = dict(snapshot)

    for event_type, payload in events:
        if manager.broadcast_callback is None:
            continue
        try:
            await manager.broadcast_callback(event_type, payload)
        except Exception as exc:
            log.warning("Failed to broadcast topology event %s: %s", event_type.value, exc)


async def reconcile_topology_unlocked(
    manager: _TopologyManager, snapshot: Snapshot, *, deps: TopologyRuntimeDeps
) -> None:
    live_paths = set(snapshot)
    removed: list[tuple[str, str]] = []

    for hardware_id, devices in manager.grabbed_devices.items():
        for device in devices:
            stable_path = str(getattr(device, "stable_path", "") or device.path)
            if stable_path not in live_paths:
                removed.append((hardware_id, device.path))

    for hardware_id, path in removed:
        await deps.release_interface_fn(manager, hardware_id, path)


def build_topology_events(
    manager: _TopologyManager,
    previous: Snapshot,
    current: Snapshot,
    desired_hardware_ids: set[str],
) -> list[tuple[CommandType, JsonObject]]:
    events: list[tuple[CommandType, JsonObject]] = []

    for stable_path in sorted(previous.keys() - current.keys()):
        info = previous[stable_path]
        if info.hardware_id not in desired_hardware_ids:
            continue
        events.append(
            (
                manager._command_type.DEVICE_DISCONNECTED,
                live_interface_payload(info),
            )
        )

    for stable_path in sorted(current.keys() - previous.keys()):
        info = current[stable_path]
        if info.hardware_id not in desired_hardware_ids:
            continue
        events.append(
            (
                manager._command_type.DEVICE_CONNECTED,
                live_interface_payload(info),
            )
        )

    return events


def live_interface_payload(info: Any) -> JsonObject:
    return {
        "hardware_id": info.hardware_id,
        "vendor_id": info.vendor_id,
        "product_id": info.product_id,
        "path": info.path,
        "stable_path": info.stable_path,
        "interface_id": info.interface_id,
    }


def scan_live_interfaces_sync(
    *,
    clear_device_path_cache_fn: ClearDevicePathCacheFn,
    device_paths_fn: DevicePathsFn,
    device_input_fn: DeviceInputFn,
    resolve_stable_path_fn: ResolveStablePathFn,
    get_interface_id_fn: GetInterfaceIdFn,
    log: logging.Logger,
) -> Snapshot:
    clear_device_path_cache_fn()
    snapshot: Snapshot = {}

    for path in device_paths_fn():
        try:
            device = device_input_fn(path)
            info = device.info
            vendor_id = f"{info.vendor:04x}"
            product_id = f"{info.product:04x}"
            hardware_id = f"{vendor_id}:{product_id}"
            stable_path = resolve_stable_path_fn(path)
            snapshot[stable_path] = LiveInterfaceInfo(
                hardware_id=hardware_id,
                vendor_id=vendor_id,
                product_id=product_id,
                stable_path=stable_path,
                path=path,
                interface_id=str(get_interface_id_fn(stable_path) or "").lower(),
            )
        except Exception as exc:
            log.debug("Could not read live topology device %s: %s", path, exc)

    return snapshot
