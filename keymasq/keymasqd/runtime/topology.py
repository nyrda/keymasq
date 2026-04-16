import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Protocol, TypeVar, cast

from keymasq.common.ipc import CommandType
from keymasq.keymasqd.runtime import grab_lifecycle

type JsonObject = dict[str, object]
_T = TypeVar("_T")


class _DeviceInfo(Protocol):
    @property
    def vendor(self) -> int: ...

    @property
    def product(self) -> int: ...


class _InputDevice(Protocol):
    @property
    def info(self) -> _DeviceInfo: ...


class _LiveInterfaceInfo(Protocol):
    @property
    def hardware_id(self) -> str: ...

    @property
    def vendor_id(self) -> str: ...

    @property
    def product_id(self) -> str: ...

    @property
    def stable_path(self) -> str: ...

    @property
    def path(self) -> str: ...

    @property
    def interface_id(self) -> str: ...


type Snapshot = dict[str, _LiveInterfaceInfo]


class _LiveInterfaceInfoFactory(Protocol):
    def __call__(
        self,
        *,
        hardware_id: str,
        vendor_id: str,
        product_id: str,
        stable_path: str,
        path: str,
        interface_id: str,
    ) -> _LiveInterfaceInfo: ...


class _GrabbedDevice(Protocol):
    @property
    def path(self) -> str: ...

    @property
    def stable_path(self) -> str: ...


class _TopologyState(Protocol):
    poll_s: float
    debounce_s: float
    watcher_task: asyncio.Task[None] | None
    reconcile_task: asyncio.Task[None] | None
    live_snapshot: Snapshot
    reconciled_snapshot: Snapshot


class _GrabState(Protocol):
    @property
    def desired_grabs(self) -> Mapping[str, object]: ...


type BroadcastCallback = Callable[[CommandType, JsonObject], Awaitable[None]]


class _TopologyManager(Protocol):
    @property
    def topology_state(self) -> _TopologyState: ...

    @property
    def grab_state(self) -> _GrabState: ...

    @property
    def grabbed_devices(self) -> Mapping[str, Sequence[_GrabbedDevice]]: ...

    @property
    def broadcast_callback(self) -> BroadcastCallback | None: ...

    @property
    def _command_type(self) -> type[CommandType]: ...

    @property
    def _op_lock(self) -> asyncio.Lock: ...


class _AsyncioModule(Protocol):
    async def sleep(self, delay: float, /) -> None: ...

    def create_task(self, coro: Awaitable[_T], /) -> asyncio.Task[_T]: ...

    def current_task(self) -> asyncio.Task[None] | None: ...

    def to_thread(
        self,
        func: Callable[..., _T],
        /,
        *args: object,
        **kwargs: object,
    ) -> Awaitable[_T]: ...


class _ContextlibModule(Protocol):
    def suppress(self, *exceptions: type[BaseException]) -> AbstractContextManager[None]: ...


type ClearDevicePathCacheFn = Callable[[], None]
type DevicePathsFn = Callable[[], list[str]]
type DeviceInputFn = Callable[[str], _InputDevice]
type ResolveStablePathFn = Callable[[str], str]
type GetInterfaceIdFn = Callable[[str], str | None]
type ReleaseInterfaceFn = Callable[[_TopologyManager, str, str], Awaitable[None]]

_release_interface_unlocked = cast(
    ReleaseInterfaceFn,
    grab_lifecycle.release_interface_unlocked,
)


def _manager_op_lock(manager: _TopologyManager) -> asyncio.Lock:
    return manager._op_lock  # pyright: ignore[reportPrivateUsage]


def _manager_command_type(manager: _TopologyManager) -> type[CommandType]:
    return manager._command_type  # pyright: ignore[reportPrivateUsage]


async def start_topology_watcher(
    manager: _TopologyManager,
    *,
    asyncio_mod: _AsyncioModule,
    cancelled_error: type[BaseException],
    log: logging.Logger,
    live_interface_info_cls: _LiveInterfaceInfoFactory,
    clear_device_path_cache_fn: ClearDevicePathCacheFn,
    device_paths_fn: DevicePathsFn,
    device_input_fn: DeviceInputFn,
    resolve_stable_path_fn: ResolveStablePathFn,
    get_interface_id_fn: GetInterfaceIdFn,
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
            cancelled_error=cancelled_error,
            log=log,
            live_interface_info_cls=live_interface_info_cls,
            clear_device_path_cache_fn=clear_device_path_cache_fn,
            device_paths_fn=device_paths_fn,
            device_input_fn=device_input_fn,
            resolve_stable_path_fn=resolve_stable_path_fn,
            get_interface_id_fn=get_interface_id_fn,
        )
    )


async def stop_topology_watcher(
    manager: _TopologyManager,
    *,
    asyncio_mod: _AsyncioModule,
    cancelled_error: type[BaseException],
    contextlib_mod: _ContextlibModule,
) -> None:
    task = manager.topology_state.watcher_task
    manager.topology_state.watcher_task = None
    if task is not None and not task.done():
        task.cancel()
        with contextlib_mod.suppress(cancelled_error):
            await task

    reconcile_task = manager.topology_state.reconcile_task
    manager.topology_state.reconcile_task = None
    if reconcile_task is not None and not reconcile_task.done():
        reconcile_task.cancel()
        with contextlib_mod.suppress(cancelled_error):
            await reconcile_task


async def topology_watch_loop(
    manager: _TopologyManager,
    *,
    asyncio_mod: _AsyncioModule,
    cancelled_error: type[BaseException],
    log: logging.Logger,
    live_interface_info_cls: _LiveInterfaceInfoFactory,
    clear_device_path_cache_fn: ClearDevicePathCacheFn,
    device_paths_fn: DevicePathsFn,
    device_input_fn: DeviceInputFn,
    resolve_stable_path_fn: ResolveStablePathFn,
    get_interface_id_fn: GetInterfaceIdFn,
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
            except cancelled_error:
                raise
            except Exception as exc:
                log.warning("Topology scan failed: %s", exc)
                continue

            if snapshot != manager.topology_state.live_snapshot:
                manager.topology_state.live_snapshot = dict(snapshot)
                schedule_topology_reconcile(
                    manager,
                    snapshot,
                    asyncio_mod=asyncio_mod,
                    cancelled_error=cancelled_error,
                    log=log,
                )
                continue

            if snapshot != manager.topology_state.reconciled_snapshot and (
                manager.topology_state.reconcile_task is None
                or manager.topology_state.reconcile_task.done()
            ):
                schedule_topology_reconcile(
                    manager,
                    snapshot,
                    asyncio_mod=asyncio_mod,
                    cancelled_error=cancelled_error,
                    log=log,
                )
    except cancelled_error:
        raise


def schedule_topology_reconcile(
    manager: _TopologyManager,
    snapshot: Snapshot,
    *,
    asyncio_mod: _AsyncioModule,
    cancelled_error: type[BaseException],
    log: logging.Logger,
) -> None:
    task = manager.topology_state.reconcile_task
    if task is not None and not task.done():
        task.cancel()

    async def _run() -> None:
        try:
            await asyncio_mod.sleep(manager.topology_state.debounce_s)
            await reconcile_topology(manager, snapshot, log=log)
        except cancelled_error:
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
) -> None:
    async with _manager_op_lock(manager):
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


async def reconcile_topology_unlocked(manager: _TopologyManager, snapshot: Snapshot) -> None:
    live_paths = set(snapshot)
    removed: list[tuple[str, str]] = []

    for hardware_id, devices in manager.grabbed_devices.items():
        for device in devices:
            stable_path = str(getattr(device, "stable_path", "") or device.path)
            if stable_path not in live_paths:
                removed.append((hardware_id, device.path))

    for hardware_id, path in removed:
        await _release_interface_unlocked(manager, hardware_id, path)


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
                _manager_command_type(manager).DEVICE_DISCONNECTED,
                live_interface_payload(info),
            )
        )

    for stable_path in sorted(current.keys() - previous.keys()):
        info = current[stable_path]
        if info.hardware_id not in desired_hardware_ids:
            continue
        events.append(
            (
                _manager_command_type(manager).DEVICE_CONNECTED,
                live_interface_payload(info),
            )
        )

    return events


def live_interface_payload(info: _LiveInterfaceInfo) -> JsonObject:
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
    live_interface_info_cls: _LiveInterfaceInfoFactory,
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
