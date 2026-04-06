from typing import Any

from keyforge.keyforged.runtime import grab_lifecycle


async def start_topology_watcher(
    manager: Any,
    *,
    asyncio_mod: Any,
    log: Any,
    live_interface_info_cls: Any,
    clear_device_path_cache_fn: Any,
    device_paths_fn: Any,
    device_input_fn: Any,
    resolve_stable_path_fn: Any,
    get_interface_id_fn: Any,
) -> None:
    if (
        manager.topology_state.watcher_task is not None
        and not manager.topology_state.watcher_task.done()
    ):
        return
    snapshot = await asyncio_mod.to_thread(
        scan_live_interfaces_sync,
        live_interface_info_cls=live_interface_info_cls,
        clear_device_path_cache_fn=clear_device_path_cache_fn,
        device_paths_fn=device_paths_fn,
        device_input_fn=device_input_fn,
        resolve_stable_path_fn=resolve_stable_path_fn,
        get_interface_id_fn=get_interface_id_fn,
        log=log,
    )
    manager.topology_state.live_snapshot = dict(snapshot)
    manager.topology_state.reconciled_snapshot = dict(snapshot)
    manager.topology_state.watcher_task = asyncio_mod.create_task(
        topology_watch_loop(
            manager,
            asyncio_mod=asyncio_mod,
            log=log,
            live_interface_info_cls=live_interface_info_cls,
            clear_device_path_cache_fn=clear_device_path_cache_fn,
            device_paths_fn=device_paths_fn,
            device_input_fn=device_input_fn,
            resolve_stable_path_fn=resolve_stable_path_fn,
            get_interface_id_fn=get_interface_id_fn,
        )
    )


async def stop_topology_watcher(manager: Any, *, asyncio_mod: Any, contextlib_mod: Any) -> None:
    task = manager.topology_state.watcher_task
    manager.topology_state.watcher_task = None
    if task is not None and not task.done():
        task.cancel()
        with contextlib_mod.suppress(asyncio_mod.CancelledError):
            await task

    reconcile_task = manager.topology_state.reconcile_task
    manager.topology_state.reconcile_task = None
    if reconcile_task is not None and not reconcile_task.done():
        reconcile_task.cancel()
        with contextlib_mod.suppress(asyncio_mod.CancelledError):
            await reconcile_task


async def topology_watch_loop(
    manager: Any,
    *,
    asyncio_mod: Any,
    log: Any,
    live_interface_info_cls: Any,
    clear_device_path_cache_fn: Any,
    device_paths_fn: Any,
    device_input_fn: Any,
    resolve_stable_path_fn: Any,
    get_interface_id_fn: Any,
) -> None:
    try:
        while True:
            await asyncio_mod.sleep(manager.topology_state.poll_s)
            try:
                snapshot = await asyncio_mod.to_thread(
                    scan_live_interfaces_sync,
                    live_interface_info_cls=live_interface_info_cls,
                    clear_device_path_cache_fn=clear_device_path_cache_fn,
                    device_paths_fn=device_paths_fn,
                    device_input_fn=device_input_fn,
                    resolve_stable_path_fn=resolve_stable_path_fn,
                    get_interface_id_fn=get_interface_id_fn,
                    log=log,
                )
            except asyncio_mod.CancelledError:
                raise
            except Exception as exc:
                log.warning("Topology scan failed: %s", exc)
                continue

            if snapshot != manager.topology_state.live_snapshot:
                manager.topology_state.live_snapshot = dict(snapshot)
                schedule_topology_reconcile(manager, snapshot, asyncio_mod=asyncio_mod, log=log)
                continue

            if snapshot != manager.topology_state.reconciled_snapshot and (
                manager.topology_state.reconcile_task is None
                or manager.topology_state.reconcile_task.done()
            ):
                schedule_topology_reconcile(manager, snapshot, asyncio_mod=asyncio_mod, log=log)
    except asyncio_mod.CancelledError:
        raise


def schedule_topology_reconcile(
    manager: Any,
    snapshot: dict[str, Any],
    *,
    asyncio_mod: Any,
    log: Any,
) -> None:
    task = manager.topology_state.reconcile_task
    if task is not None and not task.done():
        task.cancel()

    async def _run() -> None:
        try:
            await asyncio_mod.sleep(manager.topology_state.debounce_s)
            await reconcile_topology(manager, snapshot, log=log)
        except asyncio_mod.CancelledError:
            raise
        except Exception as exc:
            log.warning("Topology reconcile failed: %s", exc)
        finally:
            current = manager.topology_state.reconcile_task
            if current is asyncio_mod.current_task():
                manager.topology_state.reconcile_task = None

    manager.topology_state.reconcile_task = asyncio_mod.create_task(_run())


async def reconcile_topology(manager: Any, snapshot: dict[str, Any], *, log: Any) -> None:
    async with manager._op_lock:
        previous = dict(manager.topology_state.reconciled_snapshot)
        desired_hardware_ids = set(manager.grab_state.desired_grabs)
        events = build_topology_events(manager, previous, snapshot, desired_hardware_ids)
        await reconcile_topology_unlocked(manager, snapshot)
        manager.topology_state.reconciled_snapshot = dict(snapshot)

    for event_type, payload in events:
        if manager.broadcast_callback is None:
            continue
        try:
            await manager.broadcast_callback(event_type, payload)
        except Exception as exc:
            log.warning("Failed to broadcast topology event %s: %s", event_type.value, exc)


async def reconcile_topology_unlocked(manager: Any, snapshot: dict[str, Any]) -> None:
    live_paths = set(snapshot)
    removed: list[tuple[str, str]] = []

    for hardware_id, devices in manager.grabbed_devices.items():
        for device in devices:
            stable_path = str(getattr(device, "stable_path", "") or device.path)
            if stable_path not in live_paths:
                removed.append((hardware_id, device.path))

    for hardware_id, path in removed:
        await grab_lifecycle.release_interface_unlocked(manager, hardware_id, path)


def build_topology_events(
    manager: Any,
    previous: dict[str, Any],
    current: dict[str, Any],
    desired_hardware_ids: set[str],
) -> list[tuple[Any, dict[str, object]]]:
    events: list[tuple[Any, dict[str, object]]] = []

    for stable_path in sorted(previous.keys() - current.keys()):
        info = previous[stable_path]
        if info.hardware_id not in desired_hardware_ids:
            continue
        events.append((manager._command_type.DEVICE_DISCONNECTED, live_interface_payload(info)))

    for stable_path in sorted(current.keys() - previous.keys()):
        info = current[stable_path]
        if info.hardware_id not in desired_hardware_ids:
            continue
        events.append((manager._command_type.DEVICE_CONNECTED, live_interface_payload(info)))

    return events


def live_interface_payload(info: Any) -> dict[str, object]:
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
    live_interface_info_cls: Any,
    clear_device_path_cache_fn: Any,
    device_paths_fn: Any,
    device_input_fn: Any,
    resolve_stable_path_fn: Any,
    get_interface_id_fn: Any,
    log: Any,
) -> dict[str, Any]:
    clear_device_path_cache_fn()
    snapshot: dict[str, Any] = {}

    for path in device_paths_fn():
        try:
            device = device_input_fn(path)
            info = device.info
            vendor_id = f"{info.vendor:04x}"
            product_id = f"{info.product:04x}"
            hardware_id = f"{vendor_id}:{product_id}"
            stable_path = resolve_stable_path_fn(path)
            snapshot[stable_path] = live_interface_info_cls(
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
