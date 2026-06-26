import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

import evdev

from keymasq.common.devices import (
    hardware_model_id_key,
    input_classes_include_gamepad,
    is_keymasq_device_path,
    resolve_evdev_code,
    resolve_evdev_event_type,
)
from keymasq.common.models import MappingAction
from keymasq.common.types import JsonObject
from keymasq.keymasqd.combo_engine import ComboDecision
from keymasq.keymasqd.output_helpers import resolve_output_code
from keymasq.keymasqd.permission_hints import (
    has_permission_hint,
    input_device_permission_message,
    is_permission_error,
)
from keymasq.keymasqd.runtime import actions as runtime_actions
from keymasq.keymasqd.runtime import adapters as runtime_adapters
from keymasq.keymasqd.runtime import combos as runtime_combos
from keymasq.keymasqd.runtime import device_path_resolver, source_hiding
from keymasq.keymasqd.runtime import outputs as runtime_outputs

log = logging.getLogger("keymasqd.devices")
type JsonObjectFn = Callable[[object], JsonObject | None]
type StrValueFn = Callable[..., str]
type IntValueFn = Callable[..., int]
type ResolveStablePathFn = Callable[[str], str]
type GetInterfaceIdFn = Callable[[str], str | None]
type FireAndObserve = Callable[[Awaitable[object], str], asyncio.Task[object]]
type DesiredGrabConfigFactory = Callable[..., object]
type GrabbedDeviceFactory = Callable[..., Any]
type _ManagedGrabbedDevice = Any
type _GrabManager = Any


ASYNCIO_RUNTIME = runtime_adapters.ASYNCIO_RUNTIME
COMBO_EVDEV_RUNTIME = runtime_adapters.COMBO_EVDEV_RUNTIME


@dataclass(frozen=True)
class GrabDeviceDeps:
    desired_grab_config_cls: DesiredGrabConfigFactory
    clear_device_path_cache_fn: Callable[[], None]
    resolve_stable_path_fn: ResolveStablePathFn
    device_path_resolver_deps: device_path_resolver.DevicePathResolverDeps
    grabbed_device_cls: GrabbedDeviceFactory
    get_interface_id_fn: GetInterfaceIdFn
    str_value_fn: StrValueFn
    int_value_fn: IntValueFn
    fire_and_observe_fn: FireAndObserve
    errno_mod: runtime_adapters.ErrnoModule


@dataclass(frozen=True)
class GrabRequest:
    hardware_id: str
    evdev_paths: list[str]
    button_map: dict[str, str]
    button_codes: dict[str, int] | None = None
    button_values: dict[str, int] | None = None
    analog_inputs: dict[str, object] | None = None
    force_grab_unmapped: bool = False
    evdev_interfaces: list[JsonObject] | None = None
    update_desired: bool = True


@dataclass(frozen=True)
class GrabPlan:
    hardware_id: str
    raw_interfaces: list[JsonObject]
    evdev_interfaces_provided: bool
    resolved_interfaces: list[device_path_resolver.ResolvedInterface]
    requested_paths: set[str]
    requested_claim_paths: set[str]
    resolved_by_claim_path: dict[str, device_path_resolver.ResolvedInterface]
    desired_paths: set[str]
    mapped_evdev_names: set[str]
    resolved_button_codes: dict[str, int]
    resolved_button_values: dict[str, int]
    button_mapped_bindings: set[tuple[int, int]]
    mapped_bindings: set[tuple[int, int]]
    analog_inputs: dict[str, object]
    existing_devices: list[_ManagedGrabbedDevice]
    existing_by_claim_path: dict[str, _ManagedGrabbedDevice]
    previous_desired_paths: set[str] | None
    previous_desired_config: object | None
    requests_gamepad_source_hiding: bool


@dataclass(frozen=True)
class _RuntimeCallbacks:
    combo_deps: Any
    event_callback: Callable[..., Awaitable[ComboDecision | bool | None]]
    runtime_cleanup_callback: Callable[[str, str | None], Awaitable[None]]
    runtime_disconnect_callback: Callable[[str, str], Awaitable[None]]


@dataclass
class _GrabLoopState:
    devices: list[_ManagedGrabbedDevice]
    grabbed_count: int = 0
    skipped_count: int = 0
    available_count: int = 0
    created_global_uinputs: bool = False


def _normalize_evdev_name(value: object, default: str) -> str:
    if isinstance(value, (tuple, list)):
        items = cast(Sequence[object], value)
        return default if not items else str(items[0])
    return str(value)


def _fire_and_forget(coro: Awaitable[object], _label: str) -> asyncio.Task[object]:
    return asyncio.ensure_future(coro)


def combo_runtime_deps(
    *,
    resolve_code_fn: runtime_combos.ResolveCodeFn = resolve_output_code,
    fire_and_observe_fn: runtime_combos.FireAndObserve = _fire_and_forget,
) -> runtime_combos.ComboRuntimeDeps:
    return runtime_combos.ComboRuntimeDeps(
        asyncio_mod=ASYNCIO_RUNTIME,
        evdev_mod=COMBO_EVDEV_RUNTIME,
        uinput_writer=runtime_adapters.identity_uinput_writer,
        resolve_code_fn=resolve_code_fn,
        fire_and_observe_fn=fire_and_observe_fn,
    )


async def stop_device_event_loop(device: object) -> None:
    stopper = getattr(device, "stop_event_loop", None)
    if callable(stopper):
        result = cast(Callable[[], object], stopper)()
        if inspect.isawaitable(result):
            await cast(Awaitable[None], result)
        return

    if hasattr(device, "_running"):
        cast(Any, device)._running = False
    task = getattr(device, "task", None)
    if task is None or task is asyncio.current_task():
        return
    cancel = getattr(task, "cancel", None)
    if callable(cancel):
        cancel()
    try:
        await asyncio.wait_for(cast(Awaitable[object], task), timeout=1.0)
    except (TimeoutError, asyncio.CancelledError):
        pass


async def stop_device_event_loops(devices: Sequence[object]) -> None:
    for device in devices:
        await stop_device_event_loop(device)


async def grab_device_unlocked(
    manager: _GrabManager,
    request: GrabRequest,
    deps: GrabDeviceDeps,
) -> dict[str, object]:
    deps.clear_device_path_cache_fn()
    device_path_resolver.clear_cached_devices()
    cancel_pending_hardware_release(manager, request.hardware_id)

    plan = _build_grab_plan(manager, request, deps)
    if request.update_desired:
        _persist_desired_grab(manager, request, plan, deps)
    _log_grab_request(plan)
    _update_existing_devices(plan, request, deps)
    _reconcile_existing_interface_releases(manager, request.hardware_id, plan, deps)
    callbacks = _build_runtime_callbacks(manager, deps)
    state = _GrabLoopState(devices=list(plan.existing_devices))

    for path in sorted(plan.requested_paths):
        await _grab_one_interface(manager, request, plan, deps, callbacks, state, path)

    return await _finalize_grab(manager, request, plan, deps, state)


def _build_grab_plan(
    manager: _GrabManager,
    request: GrabRequest,
    deps: GrabDeviceDeps,
) -> GrabPlan:
    raw_interfaces = (
        list(request.evdev_interfaces)
        if request.evdev_interfaces
        else device_path_resolver.interface_descriptors_from_paths(request.evdev_paths)
    )
    requests_gamepad_source_hiding = _interfaces_request_gamepad_source_hiding(
        raw_interfaces
    )

    existing_devices = list(manager.grabbed_devices.get(request.hardware_id, []))
    existing_by_claim_path = grabbed_devices_by_claim_path(
        existing_devices,
        resolve_stable_path_fn=deps.resolve_stable_path_fn,
    )
    previous_desired_paths_raw = manager.grab_state.desired_paths.get(request.hardware_id)
    previous_desired_paths = (
        set(previous_desired_paths_raw) if previous_desired_paths_raw is not None else None
    )
    previous_desired_config = manager.grab_state.desired_grabs.get(request.hardware_id)
    excluded_paths = grabbed_paths_for_other_hardware(
        manager,
        request.hardware_id,
        resolve_stable_path_fn=deps.resolve_stable_path_fn,
    )
    resolved_interfaces = device_path_resolver.resolve_evdev_interfaces(
        raw_interfaces,
        deps=deps.device_path_resolver_deps,
        hardware_id=request.hardware_id,
        excluded_paths=excluded_paths,
        preferred_paths=grabbed_paths_for_hardware(
            manager,
            request.hardware_id,
            resolve_stable_path_fn=deps.resolve_stable_path_fn,
        ),
        match_model_gamepads=True,
    )
    requested_interface_paths = [
        deps.resolve_stable_path_fn(interface.path) for interface in resolved_interfaces
    ]
    requested_paths = set(requested_interface_paths)
    requested_claim_paths: set[str] = set()
    resolved_by_claim_path: dict[str, device_path_resolver.ResolvedInterface] = {}
    for interface in resolved_interfaces:
        aliases = path_claim_aliases(
            interface.path,
            resolve_stable_path_fn=deps.resolve_stable_path_fn,
        )
        requested_claim_paths.update(aliases)
        for alias in aliases:
            resolved_by_claim_path.setdefault(alias, interface)
    raw_interface_paths = {
        path
        for descriptor in raw_interfaces
        if (path := str(descriptor.get("path", "") or "").strip())
    }
    desired_paths = requested_paths | raw_interface_paths
    mapped_evdev_names = {name.lower() for name in request.button_map.values()}
    resolved_button_codes = {
        button_id: int(code) for button_id, code in (request.button_codes or {}).items()
    }
    resolved_button_values = {
        button_id: int(value) for button_id, value in (request.button_values or {}).items()
    }
    button_mapped_bindings = {
        (int(event_type), int(code))
        for button_id, code in resolved_button_codes.items()
        if (event_type := resolve_evdev_event_type(request.button_map.get(button_id)))
        is not None
    }
    analog_bindings = analog_input_bindings(request.analog_inputs or {})
    mapped_bindings = button_mapped_bindings | analog_bindings

    return GrabPlan(
        hardware_id=request.hardware_id,
        raw_interfaces=raw_interfaces,
        evdev_interfaces_provided=request.evdev_interfaces is not None,
        resolved_interfaces=resolved_interfaces,
        requested_paths=requested_paths,
        requested_claim_paths=requested_claim_paths,
        resolved_by_claim_path=resolved_by_claim_path,
        desired_paths=desired_paths,
        mapped_evdev_names=mapped_evdev_names,
        resolved_button_codes=resolved_button_codes,
        resolved_button_values=resolved_button_values,
        button_mapped_bindings=button_mapped_bindings,
        mapped_bindings=mapped_bindings,
        analog_inputs=dict(request.analog_inputs or {}),
        existing_devices=existing_devices,
        existing_by_claim_path=existing_by_claim_path,
        previous_desired_paths=previous_desired_paths,
        previous_desired_config=previous_desired_config,
        requests_gamepad_source_hiding=requests_gamepad_source_hiding,
    )


def _persist_desired_grab(
    manager: _GrabManager,
    request: GrabRequest,
    plan: GrabPlan,
    deps: GrabDeviceDeps,
) -> None:
    if not request.update_desired:
        return
    manager.grab_state.desired_paths[request.hardware_id] = set(plan.desired_paths)
    manager.grab_state.desired_grabs[request.hardware_id] = deps.desired_grab_config_cls(
        paths=set(plan.desired_paths),
        button_map=dict(request.button_map),
        button_codes=dict(plan.resolved_button_codes),
        button_values=dict(plan.resolved_button_values),
        analog_inputs=dict(plan.analog_inputs),
        force_grab_unmapped=bool(request.force_grab_unmapped),
        evdev_interfaces=list(plan.raw_interfaces) if plan.evdev_interfaces_provided else [],
    )


def _log_grab_request(plan: GrabPlan) -> None:
    log.info(
        "Grab request for %s: paths=%d mapped_evdev_names=%d mapped_bindings=%d",
        plan.hardware_id,
        len(plan.requested_paths),
        len(plan.mapped_evdev_names),
        len(plan.mapped_bindings),
    )


def _update_existing_devices(
    plan: GrabPlan,
    request: GrabRequest,
    deps: GrabDeviceDeps,
) -> None:
    for device in plan.existing_devices:
        device_claim_paths = grabbed_device_claim_paths(
            device,
            resolve_stable_path_fn=deps.resolve_stable_path_fn,
        )
        resolved_interface = next(
            (
                plan.resolved_by_claim_path[path]
                for path in device_claim_paths
                if path in plan.resolved_by_claim_path
            ),
            None,
        )
        interface_id = str(
            (resolved_interface.interface_id if resolved_interface is not None else "")
            or deps.get_interface_id_fn(str(getattr(device, "path", "") or ""))
            or ""
        ).lower()
        if interface_id:
            device.interface_id = interface_id
        device.update_button_map(
            request.button_map,
            plan.resolved_button_codes,
            plan.resolved_button_values,
        )
        update_analog_inputs = getattr(device, "update_analog_inputs", None)
        if callable(update_analog_inputs):
            update_analog_inputs(dict(plan.analog_inputs))


def _reconcile_existing_interface_releases(
    manager: _GrabManager,
    hardware_id: str,
    plan: GrabPlan,
    deps: GrabDeviceDeps,
) -> None:
    for device in plan.existing_devices:
        device_claim_paths = grabbed_device_claim_paths(
            device,
            resolve_stable_path_fn=deps.resolve_stable_path_fn,
        )
        if device_claim_paths & plan.requested_claim_paths:
            cancel_pending_interface_release(manager, hardware_id, device.path)

    for device in plan.existing_devices:
        device_claim_paths = grabbed_device_claim_paths(
            device,
            resolve_stable_path_fn=deps.resolve_stable_path_fn,
        )
        if device_claim_paths & plan.requested_claim_paths:
            continue
        schedule_interface_release(
            manager,
            hardware_id,
            device.path,
            asyncio_mod=ASYNCIO_RUNTIME,
            log=log,
        )


def _build_runtime_callbacks(
    manager: _GrabManager,
    deps: GrabDeviceDeps,
) -> _RuntimeCallbacks:
    combo_deps = combo_runtime_deps(fire_and_observe_fn=deps.fire_and_observe_fn)

    async def event_callback(
        callback_hardware_id: str,
        evdev_path: str,
        event_type: int,
        event_code: int,
        event_value: int,
        stable_path: str | None = None,
        source: str | None = None,
    ) -> ComboDecision | bool | None:
        return await runtime_combos.on_device_event(
            manager,
            callback_hardware_id,
            evdev_path,
            event_type,
            event_code,
            event_value,
            stable_path,
            source,
            resolve_stable_path_fn=deps.resolve_stable_path_fn,
            get_interface_id_fn=deps.get_interface_id_fn,
            int_value_fn=deps.int_value_fn,
            str_value_fn=deps.str_value_fn,
            deps=combo_deps,
        )

    async def runtime_cleanup_callback(
        cleanup_hardware_id: str,
        cleanup_source: str | None,
    ) -> None:
        await runtime_combos.clear_combo_runtime_for_binding_scope(
            manager,
            cleanup_hardware_id,
            cleanup_source,
            deps=combo_deps,
        )

    async def runtime_disconnect_callback(
        disconnected_hardware_id: str,
        disconnected_path: str,
    ) -> None:
        await release_interface(manager, disconnected_hardware_id, disconnected_path)

    return _RuntimeCallbacks(
        combo_deps=combo_deps,
        event_callback=event_callback,
        runtime_cleanup_callback=runtime_cleanup_callback,
        runtime_disconnect_callback=runtime_disconnect_callback,
    )


def _construct_grabbed_device(
    manager: _GrabManager,
    request: GrabRequest,
    plan: GrabPlan,
    deps: GrabDeviceDeps,
    callbacks: _RuntimeCallbacks,
    path: str,
    interface_id: str,
    probe_device: object,
) -> _ManagedGrabbedDevice:
    detected_types = manager._detect_device_types(probe_device)
    detected_type = deps.device_path_resolver_deps.primary_input_class_fn(detected_types)

    def mapping_getter(hid: str = request.hardware_id) -> dict[str, MappingAction]:
        return manager.active_mappings.get(hid, {})

    def diagnostics_recorder(label: str, duration_us: float) -> None:
        manager._record_diagnostic(label, duration_us)

    def gamepad_output_resolver(
        output_id: str | None,
        context: str,
    ) -> object | None:
        return manager.resolve_gamepad_output(output_id, context=context)

    return deps.grabbed_device_cls(
        path=path,
        hardware_id=request.hardware_id,
        button_map=request.button_map,
        button_codes=plan.resolved_button_codes,
        button_values=plan.resolved_button_values,
        analog_inputs=dict(plan.analog_inputs),
        mapping_getter=mapping_getter,
        event_callback=callbacks.event_callback,
        device_type=detected_type,
        device_types=detected_types,
        verbosity=manager.verbosity,
        keyboard_uinput=manager.output_state.keyboard_uinput,
        mouse_uinput=manager.output_state.mouse_uinput,
        gamepad_uinput=manager.output_state.gamepad_uinput,
        gamepad_output_resolver=gamepad_output_resolver,
        broadcast_callback=manager.broadcast_callback,
        cursor_position_setter=manager.set_cursor_position,
        natural_mouse_mover=getattr(manager, "move_cursor_natural", None),
        recording_manager=manager.recording_manager,
        macro_player=manager.play_macro,
        emergency_resetter=manager.emergency_reset,
        inspector_event_callback=manager.broadcast_device_inspector_event,
        inspector_active_getter=manager.device_inspector_active,
        inspector_suppression_getter=manager.device_inspector_suppressed,
        inspector_suppressed_ids_getter=(
            manager.device_inspector_suppressed_hardware_ids_snapshot
        ),
        inspector_suppression_disabler=manager.disable_device_inspector_suppression,
        profile_activation_recorder=manager.record_profile_action,
        profile_activation_trigger_start_observer=(
            manager.observe_profile_trigger_start
        ),
        profile_activation_trigger_end_observer=manager.observe_profile_trigger_end,
        suppress_rel_getter=lambda: manager.macro_state.mouse_rel_suppressed,
        mouse_rel_suppression_start_callback=lambda: None,
        diagnostics_recorder=diagnostics_recorder,
        runtime_cleanup_callback=callbacks.runtime_cleanup_callback,
        runtime_disconnect_callback=callbacks.runtime_disconnect_callback,
        repeat_state=manager.repeat_state,
        interface_id=interface_id,
    )


async def _grab_one_interface(
    manager: _GrabManager,
    request: GrabRequest,
    plan: GrabPlan,
    deps: GrabDeviceDeps,
    callbacks: _RuntimeCallbacks,
    state: _GrabLoopState,
    path: str,
) -> None:
    if path in plan.existing_by_claim_path:
        return

    raw_device: Any | None = None
    try:
        probe_device = manager._device_input(path)
        raw_device = probe_device
        state.available_count += 1
        caps = cast(dict[int, Sequence[object]], probe_device.capabilities())
        resolved_interface = plan.resolved_by_claim_path.get(path)
        interface_id = str(
            (resolved_interface.interface_id if resolved_interface is not None else "")
            or deps.get_interface_id_fn(path)
            or ""
        ).lower()
        interface_mapped_bindings = plan.button_mapped_bindings | analog_input_bindings(
            plan.analog_inputs,
            source=interface_id,
        )
        has_mapped_buttons = device_has_mapped_buttons(
            caps,
            plan.mapped_evdev_names,
            interface_mapped_bindings,
            evdev_mod=evdev,
        )

        if has_mapped_buttons or request.force_grab_unmapped:
            needs_global_uinputs = request.hardware_id not in manager.grabbed_devices
            if needs_global_uinputs and not state.created_global_uinputs:
                runtime_outputs.create_global_uinputs(
                    manager,
                    evdev_mod=evdev,  # pyright: ignore[reportArgumentType]
                    log=log,
                    uinput_writer=runtime_adapters.identity_uinput_writer,
                )
                state.created_global_uinputs = True
            device = _construct_grabbed_device(
                manager,
                request,
                plan,
                deps,
                callbacks,
                path,
                interface_id,
                probe_device,
            )
            state.devices.append(device)
            _store_grabbed_devices(manager, request.hardware_id, state.devices)
            try:
                await grab_with_retry(
                    device,
                    path,
                    asyncio_mod=ASYNCIO_RUNTIME,
                    log=log,
                    errno_mod=deps.errno_mod,
                )
            except (asyncio.CancelledError, Exception):
                state.devices.remove(device)
                _store_grabbed_devices(manager, request.hardware_id, state.devices)
                raise
            state.grabbed_count += 1
            if manager.verbosity >= 1:
                reason = "mapped buttons" if has_mapped_buttons else "forced for combos"
                log.debug("  %s - grabbed (%s)", path, reason)
        else:
            state.skipped_count += 1
            if manager.verbosity >= 1:
                log.debug("  %s - skipped (no matching mapped button names/codes)", path)
    except OSError as exc:
        if raw_device is not None:
            runtime_adapters.close_device(raw_device)
            raw_device = None
        if exc.errno in {deps.errno_mod.ENOENT, deps.errno_mod.ENODEV}:
            log.info("Skipping unavailable interface for %s: %s", request.hardware_id, path)
            return
        reported_exc = await _rollback_failed_grab(manager, request, plan, deps, state, path, exc)
        raise reported_exc from exc
    except Exception as exc:
        if raw_device is not None:
            runtime_adapters.close_device(raw_device)
            raw_device = None
        reported_exc = await _rollback_failed_grab(manager, request, plan, deps, state, path, exc)
        raise reported_exc from exc
    finally:
        if raw_device is not None:
            runtime_adapters.close_device(raw_device)


async def _rollback_failed_grab(
    manager: _GrabManager,
    request: GrabRequest,
    plan: GrabPlan,
    deps: GrabDeviceDeps,
    state: _GrabLoopState,
    path: str,
    exc: BaseException,
) -> BaseException:
    reported_exc = _permission_aware_grab_exception(path, exc)
    log.error("Failed to grab %s: %s", path, reported_exc)
    for device in list(state.devices):
        if any(device is existing for existing in plan.existing_devices):
            continue
        await device.release()
    _store_grabbed_devices(manager, request.hardware_id, plan.existing_devices)
    if state.created_global_uinputs:
        runtime_outputs.destroy_global_uinputs(manager, log=log)
    cancel_pending_interface_releases_for_hardware(manager, request.hardware_id)
    if request.update_desired:
        restore_desired_grab_state(
            manager,
            request.hardware_id,
            plan.previous_desired_paths,
            plan.previous_desired_config,
        )
    return reported_exc


async def _finalize_grab(
    manager: _GrabManager,
    request: GrabRequest,
    plan: GrabPlan,
    deps: GrabDeviceDeps,
    state: _GrabLoopState,
) -> dict[str, object]:
    waiting_for_device = bool(
        (plan.requested_paths or plan.raw_interfaces)
        and state.available_count == 0
        and not state.devices
    )
    if (
        not waiting_for_device
        and request.hardware_id not in manager.grabbed_devices
        and plan.requested_paths
        and (plan.mapped_evdev_names or plan.mapped_bindings)
        and state.grabbed_count == 0
    ):
        if state.created_global_uinputs:
            runtime_outputs.destroy_global_uinputs(manager, log=log)
        raise ValueError(
            f"No interfaces for {request.hardware_id} matched mapped buttons "
            f"(paths={len(plan.requested_paths)}, mapped_names={len(plan.mapped_evdev_names)}, "
            f"mapped_bindings={len(plan.mapped_bindings)})"
        )

    if state.devices:
        manager.grabbed_devices[request.hardware_id] = state.devices
    else:
        manager.grabbed_devices.pop(request.hardware_id, None)

    log.info(
        "Configured device %s: total_interfaces=%d newly_grabbed=%d skipped=%d",
        request.hardware_id,
        len(state.devices),
        state.grabbed_count,
        state.skipped_count,
    )
    if request.update_desired:
        if plan.requests_gamepad_source_hiding and waiting_for_device:
            await _enable_hardware_hotplug_hiding_best_effort(manager, request.hardware_id)
        else:
            await _disable_hardware_hotplug_hiding_if_unused_best_effort(
                manager,
                request.hardware_id,
            )

    return {
        "grabbed": True,
        "hardware_id": request.hardware_id,
        "grabbed_count": len(state.devices),
        "skipped_count": state.skipped_count,
        "waiting_for_device": waiting_for_device,
    }


def _permission_aware_grab_exception(path: str, exc: BaseException) -> BaseException:
    if not is_permission_error(exc) or has_permission_hint(exc):
        return exc
    return PermissionError(
        input_device_permission_message(
            f"Permission denied while opening or grabbing {path}: {exc}"
        )
    )


def grabbed_paths_for_other_hardware(
    manager: _GrabManager,
    hardware_id: str,
    *,
    resolve_stable_path_fn: ResolveStablePathFn | None = None,
) -> set[str]:
    requested_hardware_id = str(hardware_id or "").strip().lower()
    paths: set[str] = set()
    for grabbed_hardware_id, devices in manager.grabbed_devices.items():
        if str(grabbed_hardware_id or "").strip().lower() == requested_hardware_id:
            continue
        for device in devices:
            paths.update(
                grabbed_device_claim_paths(
                    device,
                    resolve_stable_path_fn=resolve_stable_path_fn,
                )
            )
    return paths


def grabbed_paths_for_hardware(
    manager: _GrabManager,
    hardware_id: str,
    *,
    resolve_stable_path_fn: ResolveStablePathFn | None = None,
) -> set[str]:
    paths: set[str] = set()
    for device in manager.grabbed_devices.get(hardware_id, []):
        paths.update(
            grabbed_device_claim_paths(
                device,
                resolve_stable_path_fn=resolve_stable_path_fn,
            )
        )
    return paths


def grabbed_devices_by_claim_path(
    devices: Sequence[_ManagedGrabbedDevice],
    *,
    resolve_stable_path_fn: ResolveStablePathFn | None = None,
) -> dict[str, _ManagedGrabbedDevice]:
    by_path: dict[str, _ManagedGrabbedDevice] = {}
    for device in devices:
        for path in grabbed_device_claim_paths(
            device,
            resolve_stable_path_fn=resolve_stable_path_fn,
        ):
            by_path.setdefault(path, device)
    return by_path


def grabbed_device_claim_paths(
    device: _ManagedGrabbedDevice,
    *,
    resolve_stable_path_fn: ResolveStablePathFn | None = None,
) -> set[str]:
    paths: set[str] = set()
    for attr in ("path", "stable_path", "resolved_event_path"):
        paths.update(
            path_claim_aliases(
                getattr(device, attr, ""),
                resolve_stable_path_fn=resolve_stable_path_fn,
            )
        )
    return paths


def path_claim_aliases(
    path: object,
    *,
    resolve_stable_path_fn: ResolveStablePathFn | None = None,
) -> set[str]:
    path_text = str(path or "").strip()
    if not path_text:
        return set()
    paths = {path_text}
    if resolve_stable_path_fn is None:
        return paths
    try:
        stable_path = resolve_stable_path_fn(path_text)
    except OSError as exc:
        log.debug("Unable to resolve current stable path for grabbed %s: %s", path_text, exc)
        return paths
    except Exception:
        log.exception("Unexpected failure resolving current stable path for grabbed %s", path_text)
        return paths
    if stable_path:
        paths.add(stable_path)
    return paths


def restore_desired_grab_state(
    manager: _GrabManager,
    hardware_id: str,
    previous_desired_paths: set[str] | None,
    previous_desired_config: object | None,
) -> None:
    if previous_desired_paths is None:
        manager.grab_state.desired_paths.pop(hardware_id, None)
    else:
        manager.grab_state.desired_paths[hardware_id] = set(previous_desired_paths)

    if previous_desired_config is None:
        manager.grab_state.desired_grabs.pop(hardware_id, None)
    else:
        manager.grab_state.desired_grabs[hardware_id] = previous_desired_config


def _store_grabbed_devices(
    manager: _GrabManager,
    hardware_id: str,
    devices: Sequence[_ManagedGrabbedDevice],
) -> None:
    if devices:
        manager.grabbed_devices[hardware_id] = list(devices)
    else:
        manager.grabbed_devices.pop(hardware_id, None)


def _interfaces_request_gamepad_source_hiding(raw_interfaces: Sequence[JsonObject]) -> bool:
    if not raw_interfaces:
        return False
    return any(
        input_classes_include_gamepad(primary=descriptor.get("type"))
        and is_keymasq_device_path(str(descriptor.get("path", "") or "").strip())
        for descriptor in raw_interfaces
    )


def _desired_grab_requests_gamepad_source_hiding(desired_config: object | None) -> bool:
    raw_interfaces = getattr(desired_config, "evdev_interfaces", None)
    if not isinstance(raw_interfaces, list):
        return False
    return _interfaces_request_gamepad_source_hiding(cast(Sequence[JsonObject], raw_interfaces))


async def _disable_hardware_hotplug_hiding_if_unused(
    manager: _GrabManager,
    hardware_id: str,
) -> None:
    flag_name = hardware_model_id_key(hardware_id)
    if flag_name is None:
        return

    for other_hardware_id, desired_config in manager.grab_state.desired_grabs.items():
        if str(other_hardware_id or "").strip().lower() == str(hardware_id or "").strip().lower():
            continue
        if not _desired_grab_requests_gamepad_source_hiding(desired_config):
            continue
        if not _hardware_waiting_for_grab(manager, other_hardware_id):
            continue
        if hardware_model_id_key(other_hardware_id) == flag_name:
            return

    await source_hiding.disable_hardware_hotplug_hiding(hardware_id)


async def _enable_hardware_hotplug_hiding_best_effort(
    manager: _GrabManager,
    hardware_id: str,
) -> None:
    try:
        await source_hiding.enable_hardware_hotplug_hiding(hardware_id)
    except Exception:
        log.exception(
            "Failed to enable source-hiding hotplug state hardware_id=%s manager=%s",
            hardware_id,
            _manager_log_context(manager),
        )


async def _disable_hardware_hotplug_hiding_if_unused_best_effort(
    manager: _GrabManager,
    hardware_id: str,
) -> None:
    try:
        await _disable_hardware_hotplug_hiding_if_unused(manager, hardware_id)
    except Exception:
        log.exception(
            "Failed to disable source-hiding hotplug state hardware_id=%s manager=%s",
            hardware_id,
            _manager_log_context(manager),
        )


def _manager_log_context(manager: _GrabManager) -> str:
    return f"{type(manager).__name__}@0x{id(manager):x}"


def _hardware_waiting_for_grab(manager: _GrabManager, hardware_id: str) -> bool:
    return not bool(manager.grabbed_devices.get(hardware_id))


async def grab_with_retry(
    device: _ManagedGrabbedDevice,
    path: str,
    *,
    asyncio_mod: runtime_adapters.AsyncioRuntimeAdapter,
    log: logging.Logger,
    errno_mod: runtime_adapters.ErrnoModule,
) -> None:
    delays = [0.05, 0.10, 0.20, 0.40, 0.80]
    last_error: Exception | None = None
    for attempt, delay in enumerate(delays, start=1):
        try:
            await device.grab()
            return
        except OSError as exc:
            last_error = exc
            if exc.errno != errno_mod.EBUSY:
                raise
            if attempt >= len(delays):
                break
            log.warning(
                "Device %s busy during grab (attempt %d/%d), retrying in %.2fs",
                path,
                attempt,
                len(delays),
                delay,
            )
            await asyncio_mod.sleep(delay)
        except Exception as exc:
            last_error = exc
            raise

    if last_error is not None:
        raise last_error


def device_has_mapped_buttons(
    caps: dict[int, Sequence[object]],
    mapped_evdev_names: set[str],
    mapped_bindings: set[tuple[int, int]] | None,
    *,
    evdev_mod: Any,
) -> bool:
    mapped_binding_set = {
        (int(event_type), int(code)) for event_type, code in (mapped_bindings or set())
    }
    for ev_type, codes in caps.items():
        if ev_type == evdev_mod.ecodes.EV_SYN:
            continue
        for code in codes:
            if isinstance(code, tuple):
                if not code or not isinstance(code[0], int):
                    continue
                code_val = code[0]
            elif isinstance(code, int):
                code_val = code
            else:
                continue

            if (int(ev_type), int(code_val)) in mapped_binding_set:
                return True

            try:
                code_name = _normalize_evdev_name(
                    evdev_mod.ecodes.bytype[ev_type].get(code_val, str(code_val)),
                    str(code_val),
                )
                if code_name.lower() in mapped_evdev_names:
                    return True
            except (KeyError, TypeError):
                log.debug("Unable to resolve evdev capability name", exc_info=True)
    return False


def analog_input_bindings(
    analog_inputs: dict[str, object],
    *,
    source: str | None = None,
) -> set[tuple[int, int]]:
    bindings: set[tuple[int, int]] = set()
    normalized_source = str(source or "").strip().lower()
    for raw_input in analog_inputs.values():
        if not isinstance(raw_input, dict):
            continue
        input_data = cast(dict[str, object], raw_input)
        input_source = str(input_data.get("source", "") or "").strip().lower()
        if normalized_source and input_source and input_source != normalized_source:
            continue
        raw_axes = input_data.get("axes")
        if not isinstance(raw_axes, list):
            continue
        for raw_axis in cast(list[object], raw_axes):
            if not isinstance(raw_axis, dict):
                continue
            axis_data = cast(dict[str, object], raw_axis)
            code = _axis_code(axis_data)
            if code is not None:
                bindings.add((int(evdev.ecodes.EV_ABS), int(code)))
    return bindings


def _axis_code(axis: dict[str, object]) -> int | None:
    evdev_code = axis.get("evdev_code")
    if isinstance(evdev_code, int):
        return evdev_code
    if isinstance(evdev_code, str):
        try:
            return int(evdev_code, 0)
        except ValueError:
            return None
    return resolve_evdev_code(str(axis.get("evdev", "") or ""))


async def release_device_unlocked(
    manager: _GrabManager, hardware_id: str, *, log: logging.Logger
) -> dict[str, object]:
    cancel_pending_hardware_release(manager, hardware_id)
    cancel_pending_interface_releases_for_hardware(manager, hardware_id)
    await stop_device_event_loops(manager.grabbed_devices.get(hardware_id, []))
    await runtime_combos.clear_combo_runtime_for_binding_scope(
        manager,
        hardware_id,
        None,
        deps=combo_runtime_deps(),
    )
    desired_config = manager.grab_state.desired_grabs.pop(hardware_id, None)
    if _desired_grab_requests_gamepad_source_hiding(desired_config):
        await _disable_hardware_hotplug_hiding_if_unused_best_effort(
            manager,
            hardware_id,
        )
    devices = manager.grabbed_devices.pop(hardware_id, [])

    for device in devices:
        await device.release()

    if devices:
        runtime_outputs.destroy_global_uinputs(manager, log=log)
    manager.active_mappings.pop(hardware_id, None)
    manager.grab_state.desired_paths.pop(hardware_id, None)
    log.info("Released device %s", hardware_id)
    return {"released": True, "hardware_id": hardware_id}


async def schedule_hardware_release_unlocked(
    manager: _GrabManager,
    hardware_id: str,
    grace_s: float | None,
    *,
    asyncio_mod: runtime_adapters.AsyncioRuntimeAdapter,
    log: logging.Logger,
) -> dict[str, object]:
    devices = manager.grabbed_devices.get(hardware_id, [])
    desired_config = manager.grab_state.desired_grabs.get(hardware_id)
    if not devices:
        manager.grab_state.desired_grabs.pop(hardware_id, None)
        manager.active_mappings.pop(hardware_id, None)
        manager.grab_state.desired_paths.pop(hardware_id, None)
        if _desired_grab_requests_gamepad_source_hiding(desired_config):
            await _disable_hardware_hotplug_hiding_if_unused_best_effort(
                manager,
                hardware_id,
            )
        return {"released": True, "hardware_id": hardware_id}

    manager.active_mappings[hardware_id] = {}
    manager.grab_state.desired_paths[hardware_id] = set()
    if _desired_grab_requests_gamepad_source_hiding(desired_config):
        await _disable_hardware_hotplug_hiding_if_unused_best_effort(
            manager,
            hardware_id,
        )

    delay = max(
        0.01,
        float(manager.grab_state.release_grace_s if grace_s is None else grace_s),
    )
    cancel_pending_hardware_release(manager, hardware_id)
    manager.grab_state.pending_hardware_release[hardware_id] = asyncio_mod.create_task(
        delayed_hardware_release(manager, hardware_id, delay, asyncio_mod=asyncio_mod, log=log)
    )
    log.info("Scheduled hardware release for %s in %.1fs", hardware_id, delay)
    return {
        "released": False,
        "scheduled": True,
        "hardware_id": hardware_id,
        "grace_s": delay,
    }


async def delayed_hardware_release(
    manager: _GrabManager,
    hardware_id: str,
    delay: float,
    *,
    asyncio_mod: runtime_adapters.AsyncioRuntimeAdapter,
    log: logging.Logger,
) -> None:
    next_delay = float(delay)
    try:
        while True:
            await asyncio_mod.sleep(next_delay)
            async with manager._op_lock:
                task = manager.grab_state.pending_hardware_release.get(hardware_id)
                if task is not asyncio_mod.current_task():
                    return
                if manager.grab_state.desired_paths.get(hardware_id):
                    return
                if hardware_has_held_inputs(manager, hardware_id):
                    next_delay = manager.grab_state.held_release_retry_s
                    log.info(
                        "Deferred release for %s: source button still held, retrying in %.1fs",
                        hardware_id,
                        next_delay,
                    )
                    continue
                await release_device_unlocked(manager, hardware_id, log=log)
                return
    except asyncio.CancelledError:
        pass
    finally:
        task = manager.grab_state.pending_hardware_release.get(hardware_id)
        if task is asyncio_mod.current_task():
            manager.grab_state.pending_hardware_release.pop(hardware_id, None)


def hardware_has_held_inputs(manager: _GrabManager, hardware_id: str) -> bool:
    return any(
        device.has_held_source_inputs() for device in manager.grabbed_devices.get(hardware_id, [])
    )


def cancel_pending_hardware_release(manager: _GrabManager, hardware_id: str) -> None:
    task = manager.grab_state.pending_hardware_release.pop(hardware_id, None)
    if task and not task.done():
        task.cancel()


def cancel_pending_interface_release(manager: _GrabManager, hardware_id: str, path: str) -> None:
    key = (hardware_id, path)
    task = manager.grab_state.pending_interface_release.pop(key, None)
    if task and not task.done():
        task.cancel()


def cancel_pending_interface_releases_for_hardware(
    manager: _GrabManager, hardware_id: str
) -> None:
    for key in list(manager.grab_state.pending_interface_release.keys()):
        if key[0] != hardware_id:
            continue
        task = manager.grab_state.pending_interface_release.pop(key)
        if not task.done():
            task.cancel()


def schedule_interface_release(
    manager: _GrabManager,
    hardware_id: str,
    path: str,
    *,
    asyncio_mod: runtime_adapters.AsyncioRuntimeAdapter,
    log: logging.Logger,
) -> None:
    cancel_pending_interface_release(manager, hardware_id, path)
    delay = manager.grab_state.release_grace_s
    manager.grab_state.pending_interface_release[(hardware_id, path)] = asyncio_mod.create_task(
        delayed_interface_release(manager, hardware_id, path, delay, asyncio_mod=asyncio_mod)
    )
    log.info("Scheduled interface release for %s (%s) in %.1fs", hardware_id, path, delay)


async def delayed_interface_release(
    manager: _GrabManager,
    hardware_id: str,
    path: str,
    delay: float,
    *,
    asyncio_mod: runtime_adapters.AsyncioRuntimeAdapter,
) -> None:
    key = (hardware_id, path)
    try:
        await asyncio_mod.sleep(delay)
        async with manager._op_lock:
            task = manager.grab_state.pending_interface_release.get(key)
            if task is not asyncio_mod.current_task():
                return
            if path in manager.grab_state.desired_paths.get(hardware_id, set()):
                return
            await release_interface_unlocked(manager, hardware_id, path)
    except asyncio.CancelledError:
        pass
    finally:
        task = manager.grab_state.pending_interface_release.get(key)
        if task is asyncio_mod.current_task():
            manager.grab_state.pending_interface_release.pop(key, None)


async def release_interface_unlocked(
    manager: _GrabManager, hardware_id: str, path: str
) -> None:
    """Release one grabbed interface. Caller must already hold manager._op_lock."""
    devices = manager.grabbed_devices.get(hardware_id, [])
    keep: list[_ManagedGrabbedDevice] = []
    removed: _ManagedGrabbedDevice | None = None
    for device in devices:
        if removed is None and device.path == path:
            removed = device
            continue
        keep.append(device)

    if removed is None:
        return

    await stop_device_event_loop(removed)
    await runtime_combos.clear_combo_runtime_for_binding_scope(
        manager,
        hardware_id,
        str(getattr(removed, "interface_id", "") or "").lower(),
        deps=combo_runtime_deps(),
    )
    removed.release_tracked_outputs()
    await removed.release()

    if keep:
        manager.grabbed_devices[hardware_id] = keep
    else:
        manager.grabbed_devices.pop(hardware_id, None)
        desired_config = manager.grab_state.desired_grabs.get(hardware_id)
        if manager.grab_state.desired_paths.get(hardware_id):
            if _desired_grab_requests_gamepad_source_hiding(desired_config):
                await _enable_hardware_hotplug_hiding_best_effort(
                    manager,
                    hardware_id,
                )
        else:
            manager.active_mappings.pop(hardware_id, None)
            manager.grab_state.desired_paths.pop(hardware_id, None)
            manager.grab_state.desired_grabs.pop(hardware_id, None)
        runtime_outputs.destroy_global_uinputs(manager, log=log)


async def release_interface(manager: _GrabManager, hardware_id: str, path: str) -> None:
    async with manager._op_lock:
        await release_interface_unlocked(manager, hardware_id, path)


async def release_all_devices(
    manager: _GrabManager, *, fire_and_observe_fn: FireAndObserve
) -> None:
    async with manager._op_lock:
        await manager.cancel_macro_playback()
        for devices in list(manager.grabbed_devices.values()):
            await stop_device_event_loops(devices)
        await runtime_combos.clear_combo_runtime(
            manager,
            deps=combo_runtime_deps(fire_and_observe_fn=fire_and_observe_fn),
        )
        hardware_ids = set(manager.grabbed_devices) | set(manager.grab_state.desired_grabs)
        for hardware_id in list(hardware_ids):
            await release_device_unlocked(manager, hardware_id, log=log)


async def set_mapping(
    manager: _GrabManager,
    hardware_id: str,
    mapping: dict[str, object],
    *,
    json_object_fn: JsonObjectFn,
    log: logging.Logger,
) -> dict[str, object]:
    async with manager._op_lock:
        cancel_pending_hardware_release(manager, hardware_id)
        if hardware_id not in manager.grabbed_devices:
            raise ValueError(f"Device {hardware_id} not grabbed")

        parsed_mapping: dict[str, MappingAction] = {}
        for button_id, action_data in mapping.items():
            action_dict = json_object_fn(action_data)
            if isinstance(action_data, str):
                parsed_mapping[button_id] = runtime_actions.parse_action(
                    manager,
                    action_data,
                )
            elif action_dict is not None:
                parsed_mapping[button_id] = runtime_actions.parse_action(
                    manager,
                    action_dict,
                )

        previous_mapping = dict(manager.active_mappings.get(hardware_id, {}))
        manager.active_mappings[hardware_id] = parsed_mapping
        for device in manager.grabbed_devices.get(hardware_id, []):
            await device.reset_mapping_runtime_state(previous_mapping=previous_mapping)
        log.info("Updated mapping for %s (%d buttons)", hardware_id, len(parsed_mapping))
        return {"updated": True, "hardware_id": hardware_id}
