from typing import Any


async def start_topology_watcher(manager: Any, *, asyncio_mod: Any) -> None:
    if manager._topology_task is not None and not manager._topology_task.done():
        return
    snapshot = await asyncio_mod.to_thread(manager._scan_live_interfaces_sync)
    manager._live_topology_snapshot = dict(snapshot)
    manager._reconciled_topology_snapshot = dict(snapshot)
    manager._topology_task = asyncio_mod.create_task(manager._topology_watch_loop())


async def stop_topology_watcher(manager: Any, *, asyncio_mod: Any, contextlib_mod: Any) -> None:
    task = manager._topology_task
    manager._topology_task = None
    if task is not None and not task.done():
        task.cancel()
        with contextlib_mod.suppress(asyncio_mod.CancelledError):
            await task

    reconcile_task = manager._topology_reconcile_task
    manager._topology_reconcile_task = None
    if reconcile_task is not None and not reconcile_task.done():
        reconcile_task.cancel()
        with contextlib_mod.suppress(asyncio_mod.CancelledError):
            await reconcile_task


async def topology_watch_loop(manager: Any, *, asyncio_mod: Any, log: Any) -> None:
    try:
        while True:
            await asyncio_mod.sleep(manager._topology_poll_s)
            try:
                snapshot = await asyncio_mod.to_thread(manager._scan_live_interfaces_sync)
            except asyncio_mod.CancelledError:
                raise
            except Exception as exc:
                log.warning("Topology scan failed: %s", exc)
                continue

            if snapshot != manager._live_topology_snapshot:
                manager._live_topology_snapshot = dict(snapshot)
                manager._schedule_topology_reconcile(snapshot)
                continue

            if snapshot != manager._reconciled_topology_snapshot and (
                manager._topology_reconcile_task is None or manager._topology_reconcile_task.done()
            ):
                manager._schedule_topology_reconcile(snapshot)
    except asyncio_mod.CancelledError:
        raise


def schedule_topology_reconcile(
    manager: Any,
    snapshot: dict[str, Any],
    *,
    asyncio_mod: Any,
    log: Any,
) -> None:
    task = manager._topology_reconcile_task
    if task is not None and not task.done():
        task.cancel()

    async def _run() -> None:
        try:
            await asyncio_mod.sleep(manager._topology_debounce_s)
            await manager._reconcile_topology(snapshot)
        except asyncio_mod.CancelledError:
            raise
        except Exception as exc:
            log.warning("Topology reconcile failed: %s", exc)
        finally:
            current = manager._topology_reconcile_task
            if current is asyncio_mod.current_task():
                manager._topology_reconcile_task = None

    manager._topology_reconcile_task = asyncio_mod.create_task(_run())


async def reconcile_topology(manager: Any, snapshot: dict[str, Any], *, log: Any) -> None:
    async with manager._op_lock:
        previous = dict(manager._reconciled_topology_snapshot)
        desired_hardware_ids = set(manager._desired_grabs)
        events = manager._build_topology_events(previous, snapshot, desired_hardware_ids)
        await manager._reconcile_topology_unlocked(snapshot)
        manager._reconciled_topology_snapshot = dict(snapshot)

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
        await manager._release_interface_unlocked(hardware_id, path)


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
        events.append(
            (manager._command_type.DEVICE_DISCONNECTED, manager._live_interface_payload(info))
        )

    for stable_path in sorted(current.keys() - previous.keys()):
        info = current[stable_path]
        if info.hardware_id not in desired_hardware_ids:
            continue
        events.append(
            (manager._command_type.DEVICE_CONNECTED, manager._live_interface_payload(info))
        )

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
