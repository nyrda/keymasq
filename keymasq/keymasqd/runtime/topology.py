import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, cast

from keymasq.common.ipc import CommandType
from keymasq.common.types import JsonObject
from keymasq.keymasqd.runtime import adapters as runtime_adapters
from keymasq.keymasqd.runtime import device_path_resolver

type Snapshot = dict[str, Any]
type _TopologyManager = Any
type ClearDevicePathCacheFn = Callable[[], None]
type DevicePathsFn = Callable[[], list[str]]
type DeviceInputFn = Callable[[str], Any]
type ResolveStablePathFn = Callable[[str], str]
type GetInterfaceIdFn = Callable[[str], str | None]
type ReleaseInterfaceFn = Callable[[Any, str, str], Awaitable[None]]
type DetectInputClassesFn = Callable[[Any], list[str]]
type PrimaryInputClassFn = Callable[[Any], Any]


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
    detect_input_classes_fn: DetectInputClassesFn
    primary_input_class_fn: PrimaryInputClassFn
    resolve_stable_path_fn: ResolveStablePathFn
    get_interface_id_fn: GetInterfaceIdFn
    release_interface_fn: ReleaseInterfaceFn


async def _scan_live_interfaces(
    *,
    log: logging.Logger,
    deps: TopologyRuntimeDeps,
) -> Snapshot:
    return await deps.asyncio_mod.to_thread(
        scan_live_interfaces_sync,
        clear_device_path_cache_fn=deps.clear_device_path_cache_fn,
        device_paths_fn=deps.device_paths_fn,
        device_input_fn=deps.device_input_fn,
        detect_input_classes_fn=deps.detect_input_classes_fn,
        primary_input_class_fn=deps.primary_input_class_fn,
        resolve_stable_path_fn=deps.resolve_stable_path_fn,
        get_interface_id_fn=deps.get_interface_id_fn,
        log=log,
    )


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
    snapshot = await _scan_live_interfaces(log=log, deps=deps)
    manager.topology_state.live_snapshot = dict(snapshot)
    async with manager._op_lock:
        await reconcile_topology_unlocked(manager, snapshot, deps=deps)
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
                snapshot = await _scan_live_interfaces(log=log, deps=deps)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Topology scan failed: %s", exc)
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
            log.exception("Topology reconcile failed: %s", exc)
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
        hidden_source_paths = hidden_grabbed_source_paths(manager)
        events = build_topology_events(
            manager,
            previous,
            snapshot,
            desired_hardware_ids,
            hidden_source_paths=hidden_source_paths,
        )
        await reconcile_topology_unlocked(manager, snapshot, deps=deps)
        manager.topology_state.reconciled_snapshot = dict(snapshot)

    for event_type, payload in events:
        if manager.broadcast_callback is None:
            continue
        try:
            await manager.broadcast_callback(event_type, payload)
        except Exception:
            log.exception("Failed to broadcast topology event %s", event_type.value)


async def reconcile_topology_unlocked(
    manager: _TopologyManager, snapshot: Snapshot, *, deps: TopologyRuntimeDeps
) -> None:
    removed: list[tuple[str, str]] = []

    for hardware_id, devices in manager.grabbed_devices.items():
        for device in devices:
            hidden_source = is_hidden_grabbed_source(device)
            live_info = live_info_for_grabbed_device(
                snapshot,
                device,
                hidden_source=hidden_source,
            )
            if live_info is None:
                removed.append((hardware_id, device.path))
                continue

            if hidden_source:
                hardware_matches = live_interface_matches_hardware_base(
                    live_info,
                    hardware_id,
                )
            else:
                hardware_matches = live_interface_matches_desired(
                    live_info,
                    {hardware_id},
                )
            if not hardware_matches:
                removed.append((hardware_id, device.path))
                continue

            live_path = str(getattr(live_info, "path", "") or "")
            grabbed_path = str(
                getattr(device, "resolved_event_path", "") or device.path
            )
            if live_path != grabbed_path:
                removed.append((hardware_id, device.path))

    for hardware_id, path in removed:
        await deps.release_interface_fn(manager, hardware_id, path)


def live_info_for_grabbed_device(
    snapshot: Snapshot,
    device: Any,
    *,
    hidden_source: bool,
) -> Any | None:
    stable_path = str(getattr(device, "stable_path", "") or device.path)
    live_info = snapshot.get(stable_path)
    if live_info is not None:
        return live_info

    if not hidden_source:
        return None

    grabbed_path = str(getattr(device, "resolved_event_path", "") or device.path)
    for candidate in snapshot.values():
        if str(getattr(candidate, "path", "") or "") == grabbed_path:
            return candidate
    return None


def hidden_grabbed_source_paths(manager: _TopologyManager) -> set[str]:
    hidden_paths: set[str] = set()
    for devices in manager.grabbed_devices.values():
        for device in devices:
            if not is_hidden_grabbed_source(device):
                continue
            path = str(getattr(device, "resolved_event_path", "") or device.path)
            if path:
                hidden_paths.add(path)
    return hidden_paths


def is_hidden_grabbed_source(device: Any) -> bool:
    event_name = path_basename(getattr(device, "resolved_event_path", "") or device.path)
    hidden_names = source_hidden_kernel_names(device)
    return bool(event_name and event_name in hidden_names)


def source_hidden_kernel_names(device: Any) -> set[str]:
    names = list(_kernel_name_values(getattr(device, "source_hidden_kernel_names", [])))
    names.extend(
        _kernel_name_values(getattr(device, "source_pending_hidden_kernel_names", []))
    )
    return set(names)


def _kernel_name_values(values: object) -> list[str]:
    if not isinstance(values, list | tuple | set | frozenset):
        return []
    kernel_names: list[str] = []
    for value in cast(Iterable[object], values):
        name = str(value or "").strip()
        if name:
            kernel_names.append(name)
    return kernel_names


def path_basename(path: object) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    return text.rsplit("/", 1)[-1]


def build_topology_events(
    manager: _TopologyManager,
    previous: Snapshot,
    current: Snapshot,
    desired_hardware_ids: set[str],
    *,
    hidden_source_paths: set[str] | None = None,
) -> list[tuple[CommandType, JsonObject]]:
    events: list[tuple[CommandType, JsonObject]] = []
    hidden_paths = hidden_source_paths or set()

    for stable_path in sorted(previous.keys() - current.keys()):
        info = previous[stable_path]
        if live_interface_is_hidden_source_churn(info, current, hidden_paths):
            continue
        if not live_interface_matches_desired(info, desired_hardware_ids):
            continue
        events.append(
            (
                manager._command_type.DEVICE_DISCONNECTED,
                live_interface_payload(info),
            )
        )

    for stable_path in sorted(previous.keys() & current.keys()):
        previous_info = previous[stable_path]
        current_info = current[stable_path]
        if not live_interface_changed(previous_info, current_info):
            continue
        if (
            not live_interface_is_hidden_source_churn(
                previous_info,
                current,
                hidden_paths,
            )
            and live_interface_matches_desired(previous_info, desired_hardware_ids)
        ):
            events.append(
                (
                    manager._command_type.DEVICE_DISCONNECTED,
                    live_interface_payload(previous_info),
                )
            )
        if (
            not live_interface_connect_is_hidden_source_churn(
                stable_path,
                previous,
                current_info,
                hidden_paths,
            )
            and live_interface_matches_desired(current_info, desired_hardware_ids)
        ):
            events.append(
                (
                    manager._command_type.DEVICE_CONNECTED,
                    live_interface_payload(current_info),
                )
            )

    for stable_path in sorted(current.keys() - previous.keys()):
        info = current[stable_path]
        if live_interface_connect_is_hidden_source_churn(
            stable_path,
            previous,
            info,
            hidden_paths,
        ):
            continue
        if not live_interface_matches_desired(info, desired_hardware_ids):
            continue
        events.append(
            (
                manager._command_type.DEVICE_CONNECTED,
                live_interface_payload(info),
            )
        )

    return events


def live_interface_path_is_hidden_source(
    info: Any,
    hidden_source_paths: set[str],
) -> bool:
    return str(getattr(info, "path", "") or "") in hidden_source_paths


def live_interface_connect_is_hidden_source_churn(
    stable_path: str,
    previous: Snapshot,
    current_info: Any,
    hidden_source_paths: set[str],
) -> bool:
    previous_info = previous.get(stable_path)
    if previous_info is None:
        return False
    return live_interface_path_is_hidden_source(
        current_info,
        hidden_source_paths,
    ) and live_interface_path_is_hidden_source(previous_info, hidden_source_paths)


def live_interface_is_hidden_source_churn(
    info: Any,
    current: Snapshot,
    hidden_source_paths: set[str],
) -> bool:
    path = str(getattr(info, "path", "") or "")
    if path not in hidden_source_paths:
        return False
    hardware_id = normalize_hardware_id(info.hardware_id)
    return any(
        str(getattr(candidate, "path", "") or "") == path
        and normalize_hardware_id(candidate.hardware_id) == hardware_id
        for candidate in current.values()
    )


def live_interface_changed(previous_info: Any, current_info: Any) -> bool:
    return (
        normalize_hardware_id(previous_info.hardware_id)
        != normalize_hardware_id(current_info.hardware_id)
        or str(previous_info.path) != str(current_info.path)
        or str(previous_info.interface_id) != str(current_info.interface_id)
    )


def normalize_hardware_id(hardware_id: object) -> str:
    return str(hardware_id or "").strip().lower()


def live_interface_matches_desired(info: Any, desired_hardware_ids: set[str]) -> bool:
    return hardware_id_matches_desired(
        info.hardware_id,
        desired_hardware_ids,
        interface_id=getattr(info, "interface_id", ""),
    )


def live_interface_matches_hardware_base(info: Any, desired_hardware_id: str) -> bool:
    desired_base, _desired_interface = split_desired_hardware_id(desired_hardware_id)
    return normalize_hardware_id(info.hardware_id) == desired_base


def hardware_id_matches_desired(
    hardware_id: str,
    desired_hardware_ids: set[str],
    *,
    interface_id: str | None = None,
) -> bool:
    normalized = normalize_hardware_id(hardware_id)
    if not normalized:
        return False
    normalized_interface = normalize_hardware_id(interface_id)
    for desired in desired_hardware_ids:
        desired_base, desired_interface = split_desired_hardware_id(desired)
        if desired_base != normalized:
            continue
        # hardware_id_matches_desired compares parts produced by
        # split_desired_hardware_id/normalize_hardware_id: numeric "@N" suffixes
        # identify a device instance and intentionally wildcard the live
        # interface, while named suffixes must match the normalized interface.
        if not desired_interface or desired_interface.isdecimal():
            return True
        if desired_interface == normalized_interface:
            return True
    return False


def split_desired_hardware_id(desired_hardware_id: object) -> tuple[str, str]:
    normalized = normalize_hardware_id(desired_hardware_id)
    desired_base, separator, desired_interface = normalized.partition("@")
    if not separator:
        return desired_base, ""
    return desired_base, desired_interface


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
    detect_input_classes_fn: DetectInputClassesFn,
    primary_input_class_fn: PrimaryInputClassFn,
    resolve_stable_path_fn: ResolveStablePathFn,
    get_interface_id_fn: GetInterfaceIdFn,
    log: logging.Logger,
) -> Snapshot:
    clear_device_path_cache_fn()
    snapshot: Snapshot = {}
    cached_devices = device_path_resolver.refresh_cached_devices_sync(
        device_paths_fn=device_paths_fn,
        device_input_fn=device_input_fn,
        detect_input_classes_fn=detect_input_classes_fn,
        primary_input_class_fn=primary_input_class_fn,
    )

    for path, device_info in cached_devices.items():
        try:
            if device_info.is_virtual:
                continue
            vendor_id = device_info.vendor_id
            product_id = device_info.product_id
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
        except OSError as exc:
            log.debug("Could not read live topology device %s: %s", path, exc)
        except Exception:
            log.exception("Unexpected failure reading live topology device %s", path)

    return snapshot
