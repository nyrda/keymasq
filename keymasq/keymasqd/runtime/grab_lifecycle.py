import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast

import evdev

from keymasq.common.devices import resolve_evdev_code, resolve_evdev_event_type
from keymasq.common.models import MappingAction
from keymasq.keymasqd.combo_engine import ComboDecision
from keymasq.keymasqd.output_helpers import resolve_output_code
from keymasq.keymasqd.runtime import actions as runtime_actions
from keymasq.keymasqd.runtime import adapters as runtime_adapters
from keymasq.keymasqd.runtime import combos as runtime_combos
from keymasq.keymasqd.runtime import device_path_resolver
from keymasq.keymasqd.runtime import outputs as runtime_outputs

log = logging.getLogger("keymasqd.devices")
type JsonObject = dict[str, object]
type JsonObjectFn = Callable[[object], JsonObject | None]
type StrValueFn = Callable[..., str]
type OptionalStrFn = Callable[..., str | None]
type IntValueFn = Callable[..., int]
type IntOrNoneFn = Callable[..., int | None]
type FloatValueFn = Callable[..., float]
type ResolveStablePathFn = Callable[[str], str]
type GetInterfaceIdFn = Callable[[str], str | None]
type FireAndObserve = Callable[[Awaitable[object], str], asyncio.Task[object]]
type DesiredGrabConfigFactory = Callable[..., object]
type GrabbedDeviceFactory = Callable[..., Any]
type _ManagedGrabbedDevice = Any
type _GrabManager = Any
type _ErrnoModule = Any


ASYNCIO_RUNTIME = runtime_adapters.ASYNCIO_RUNTIME
COMBO_EVDEV_RUNTIME = runtime_adapters.COMBO_EVDEV_RUNTIME


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
    uinput_writer: runtime_adapters.UInputWriter = runtime_adapters.identity_uinput_writer,
    emit_mouse_move_fn: Callable[..., None] = runtime_adapters.combo_emit_mouse_move,
) -> runtime_combos.ComboRuntimeDeps:
    return runtime_combos.ComboRuntimeDeps(
        asyncio_mod=ASYNCIO_RUNTIME,
        evdev_mod=COMBO_EVDEV_RUNTIME,
        uinput_writer=uinput_writer,
        emit_mouse_move_fn=emit_mouse_move_fn,
        resolve_code_fn=resolve_code_fn,
        fire_and_observe_fn=fire_and_observe_fn,
    )


async def grab_device_unlocked(
    manager: _GrabManager,
    hardware_id: str,
    evdev_paths: list[str],
    button_map: dict[str, str],
    button_codes: dict[str, int] | None,
    button_values: dict[str, int] | None,
    analog_inputs: dict[str, object] | None,
    force_grab_unmapped: bool,
    *,
    evdev_interfaces: list[JsonObject] | None = None,
    update_desired: bool,
    desired_grab_config_cls: DesiredGrabConfigFactory,
    clear_device_path_cache_fn: Callable[[], None],
    resolve_stable_path_fn: ResolveStablePathFn,
    device_path_resolver_deps: device_path_resolver.DevicePathResolverDeps,
    grabbed_device_cls: GrabbedDeviceFactory,
    get_interface_id_fn: GetInterfaceIdFn,
    str_value_fn: StrValueFn,
    optional_str_fn: OptionalStrFn,
    int_value_fn: IntValueFn,
    int_or_none_fn: IntOrNoneFn,
    float_value_fn: FloatValueFn,
    fire_and_observe_fn: FireAndObserve,
    errno_mod: _ErrnoModule,
) -> dict[str, object]:
    clear_device_path_cache_fn()
    cancel_pending_hardware_release(manager, hardware_id)

    raw_interfaces = (
        evdev_interfaces
        if evdev_interfaces
        else device_path_resolver.interface_descriptors_from_paths(evdev_paths)
    )
    excluded_paths = grabbed_paths_for_other_hardware(manager, hardware_id)
    resolved_interfaces = device_path_resolver.resolve_evdev_interfaces(
        raw_interfaces,
        deps=device_path_resolver_deps,
        hardware_id=hardware_id,
        excluded_paths=excluded_paths,
    )
    requested_interface_paths = [
        resolve_stable_path_fn(interface.path) for interface in resolved_interfaces
    ]
    requested_paths = set(requested_interface_paths)
    raw_interface_paths = {
        path
        for descriptor in raw_interfaces
        if (path := str(descriptor.get("path", "") or "").strip())
    }
    desired_paths = requested_paths | raw_interface_paths
    resolved_by_path = {
        resolve_stable_path_fn(interface.path): interface for interface in resolved_interfaces
    }
    mapped_evdev_names = {name.lower() for name in button_map.values()}
    resolved_button_codes = {
        button_id: int(code) for button_id, code in (button_codes or {}).items()
    }
    resolved_button_values = {
        button_id: int(value) for button_id, value in (button_values or {}).items()
    }
    button_mapped_bindings = {
        (int(event_type), int(code))
        for button_id, code in resolved_button_codes.items()
        if (event_type := resolve_evdev_event_type(button_map.get(button_id))) is not None
    }
    analog_bindings = analog_input_bindings(analog_inputs or {})
    mapped_bindings = button_mapped_bindings | analog_bindings
    if update_desired:
        manager.grab_state.desired_paths[hardware_id] = set(desired_paths)
        manager.grab_state.desired_grabs[hardware_id] = desired_grab_config_cls(
            paths=set(desired_paths),
            button_map=dict(button_map),
            button_codes=dict(resolved_button_codes),
            button_values=dict(resolved_button_values),
            analog_inputs=dict(analog_inputs or {}),
            force_grab_unmapped=bool(force_grab_unmapped),
            evdev_interfaces=list(raw_interfaces) if evdev_interfaces is not None else [],
        )
    log.info(
        "Grab request for %s: paths=%d mapped_evdev_names=%d mapped_bindings=%d",
        hardware_id,
        len(requested_paths),
        len(mapped_evdev_names),
        len(mapped_bindings),
    )

    existing_by_path = {
        device.path: device for device in manager.grabbed_devices.get(hardware_id, [])
    }
    for path, device in existing_by_path.items():
        resolved_interface = resolved_by_path.get(path)
        interface_id = str(
            (resolved_interface.interface_id if resolved_interface else "")
            or get_interface_id_fn(path)
            or ""
        ).lower()
        if interface_id:
            device.interface_id = interface_id
        device.update_button_map(button_map, resolved_button_codes, resolved_button_values)
        update_analog_inputs = getattr(device, "update_analog_inputs", None)
        if callable(update_analog_inputs):
            update_analog_inputs(dict(analog_inputs or {}))

    devices = list(existing_by_path.values())
    grabbed_count = 0
    skipped_count = 0
    available_count = 0
    created_global_uinputs = False

    for path in existing_by_path:
        if path in requested_paths:
            cancel_pending_interface_release(manager, hardware_id, path)

    for path in sorted(existing_by_path.keys() - requested_paths):
        schedule_interface_release(
            manager,
            hardware_id,
            path,
            asyncio_mod=ASYNCIO_RUNTIME,
            log=log,
        )

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
            resolve_stable_path_fn=resolve_stable_path_fn,
            get_interface_id_fn=get_interface_id_fn,
            int_value_fn=int_value_fn,
            str_value_fn=str_value_fn,
            deps=combo_runtime_deps(
                fire_and_observe_fn=fire_and_observe_fn,
                uinput_writer=manager.observed_uinput_writer,
                emit_mouse_move_fn=manager.emit_diagnostics_output_mouse_move,
            ),
        )

    async def runtime_cleanup_callback(
        cleanup_hardware_id: str,
        cleanup_source: str | None,
    ) -> None:
        await runtime_combos.clear_combo_runtime_for_binding_scope(
            manager,
            cleanup_hardware_id,
            cleanup_source,
            deps=combo_runtime_deps(
                fire_and_observe_fn=fire_and_observe_fn,
                uinput_writer=manager.observed_uinput_writer,
                emit_mouse_move_fn=manager.emit_diagnostics_output_mouse_move,
            ),
        )

    for path in sorted(requested_paths):
        if path in existing_by_path:
            continue
        try:
            raw_device = manager._device_input(path)
            available_count += 1
            caps = raw_device.capabilities()
            resolved_interface = resolved_by_path.get(path)
            interface_id = str(
                (resolved_interface.interface_id if resolved_interface else "")
                or get_interface_id_fn(path)
                or ""
            ).lower()
            interface_mapped_bindings = button_mapped_bindings | analog_input_bindings(
                analog_inputs or {},
                source=interface_id,
            )
            has_mapped_buttons = device_has_mapped_buttons(
                caps,
                mapped_evdev_names,
                interface_mapped_bindings,
                evdev_mod=evdev,
            )

            if has_mapped_buttons or force_grab_unmapped:
                if hardware_id not in manager.grabbed_devices and not created_global_uinputs:
                    runtime_outputs.create_global_uinputs(
                        manager,
                        evdev_mod=evdev,  # pyright: ignore[reportArgumentType]
                        log=log,
                        uinput_writer=runtime_adapters.identity_uinput_writer,
                    )
                    created_global_uinputs = True
                detected_types = manager._detect_device_types(raw_device)
                detected_type = device_path_resolver_deps.primary_input_class_fn(
                    detected_types
                )

                def mapping_getter(hid: str = hardware_id) -> dict[str, MappingAction]:
                    return manager.active_mappings.get(hid, {})

                def diagnostics_recorder(label: str, duration_us: float) -> None:
                    manager._record_diagnostic(label, duration_us)

                def gamepad_output_resolver(
                    output_id: str | None,
                    context: str,
                ) -> object | None:
                    return manager.resolve_gamepad_output(output_id, context=context)

                device = grabbed_device_cls(
                    path=path,
                    hardware_id=hardware_id,
                    button_map=button_map,
                    button_codes=resolved_button_codes,
                    button_values=resolved_button_values,
                    analog_inputs=dict(analog_inputs or {}),
                    mapping_getter=mapping_getter,
                    event_callback=event_callback,
                    device_type=detected_type,
                    device_types=detected_types,
                    verbosity=manager.verbosity,
                    keyboard_uinput=manager.output_state.keyboard_uinput,
                    mouse_uinput=manager.output_state.mouse_uinput,
                    gamepad_uinput=manager.output_state.gamepad_uinput,
                    gamepad_output_resolver=gamepad_output_resolver,
                    broadcast_callback=manager.broadcast_callback,
                    cursor_position_setter=manager.set_cursor_position,
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
                    uinput_writer=manager.observed_uinput_writer,
                    runtime_cleanup_callback=runtime_cleanup_callback,
                    interface_id=interface_id,
                )
                await grab_with_retry(
                    device,
                    path,
                    asyncio_mod=ASYNCIO_RUNTIME,
                    log=log,
                    errno_mod=errno_mod,
                )
                devices.append(device)
                grabbed_count += 1
                if manager.verbosity >= 1:
                    reason = "mapped buttons" if has_mapped_buttons else "forced for combos"
                    log.debug("  %s - grabbed (%s)", path, reason)
            else:
                skipped_count += 1
                if manager.verbosity >= 1:
                    log.debug("  %s - skipped (no matching mapped button names/codes)", path)
        except OSError as exc:
            if exc.errno in {errno_mod.ENOENT, errno_mod.ENODEV}:
                log.info("Skipping unavailable interface for %s: %s", hardware_id, path)
                continue
            log.error("Failed to grab %s: %s", path, exc)
            for device in devices:
                if device.path in existing_by_path:
                    continue
                await device.release()
            if created_global_uinputs:
                runtime_outputs.destroy_global_uinputs(manager, log=log)
            raise
        except Exception as exc:
            log.error("Failed to grab %s: %s", path, exc)
            for device in devices:
                if device.path in existing_by_path:
                    continue
                await device.release()
            if created_global_uinputs:
                runtime_outputs.destroy_global_uinputs(manager, log=log)
            raise

    waiting_for_device = bool(
        (requested_paths or raw_interfaces) and available_count == 0 and not devices
    )
    if (
        not waiting_for_device
        and hardware_id not in manager.grabbed_devices
        and requested_paths
        and (mapped_evdev_names or mapped_bindings)
        and grabbed_count == 0
    ):
        if created_global_uinputs:
            runtime_outputs.destroy_global_uinputs(manager, log=log)
        raise ValueError(
            f"No interfaces for {hardware_id} matched mapped buttons "
            f"(paths={len(requested_paths)}, mapped_names={len(mapped_evdev_names)}, "
            f"mapped_bindings={len(mapped_bindings)})"
        )

    if devices:
        manager.grabbed_devices[hardware_id] = devices
    else:
        manager.grabbed_devices.pop(hardware_id, None)

    log.info(
        "Configured device %s: total_interfaces=%d newly_grabbed=%d skipped=%d",
        hardware_id,
        len(devices),
        grabbed_count,
        skipped_count,
    )
    return {
        "grabbed": True,
        "hardware_id": hardware_id,
        "grabbed_count": len(devices),
        "skipped_count": skipped_count,
        "waiting_for_device": waiting_for_device,
    }


def grabbed_paths_for_other_hardware(manager: _GrabManager, hardware_id: str) -> set[str]:
    requested_hardware_id = str(hardware_id or "").strip().lower()
    paths: set[str] = set()
    for grabbed_hardware_id, devices in manager.grabbed_devices.items():
        if str(grabbed_hardware_id or "").strip().lower() == requested_hardware_id:
            continue
        for device in devices:
            for attr in ("path", "stable_path"):
                path = str(getattr(device, attr, "") or "").strip()
                if path:
                    paths.add(path)
    return paths


async def grab_with_retry(
    device: _ManagedGrabbedDevice,
    path: str,
    *,
    asyncio_mod: runtime_adapters.AsyncioRuntimeAdapter,
    log: logging.Logger,
    errno_mod: _ErrnoModule,
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
            except Exception:
                pass
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
    await runtime_combos.clear_combo_runtime_for_binding_scope(
        manager,
        hardware_id,
        None,
        deps=combo_runtime_deps(
            uinput_writer=manager.observed_uinput_writer,
            emit_mouse_move_fn=manager.emit_diagnostics_output_mouse_move,
        ),
    )
    manager.grab_state.desired_grabs.pop(hardware_id, None)
    devices = manager.grabbed_devices.pop(hardware_id, [])

    for device in devices:
        await device.release()

    runtime_outputs.destroy_global_uinputs(manager, log=log)
    manager.active_mappings.pop(hardware_id, None)
    manager.grab_state.desired_paths.pop(hardware_id, None)
    log.info("Released device %s", hardware_id)
    return {"released": True, "hardware_id": hardware_id}


def schedule_hardware_release_unlocked(
    manager: _GrabManager,
    hardware_id: str,
    grace_s: float | None,
    *,
    asyncio_mod: runtime_adapters.AsyncioRuntimeAdapter,
    log: logging.Logger,
) -> dict[str, object]:
    devices = manager.grabbed_devices.get(hardware_id, [])
    if not devices:
        manager.grab_state.desired_grabs.pop(hardware_id, None)
        manager.active_mappings.pop(hardware_id, None)
        manager.grab_state.desired_paths.pop(hardware_id, None)
        return {"released": True, "hardware_id": hardware_id}

    manager.active_mappings[hardware_id] = {}
    manager.grab_state.desired_paths[hardware_id] = set()

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

    await runtime_combos.clear_combo_runtime_for_binding_scope(
        manager,
        hardware_id,
        str(getattr(removed, "interface_id", "") or "").lower(),
        deps=combo_runtime_deps(
            uinput_writer=manager.observed_uinput_writer,
            emit_mouse_move_fn=manager.emit_diagnostics_output_mouse_move,
        ),
    )
    removed.release_tracked_outputs()
    await removed.release()

    if keep:
        manager.grabbed_devices[hardware_id] = keep
    else:
        manager.grabbed_devices.pop(hardware_id, None)
        if not manager.grab_state.desired_paths.get(hardware_id):
            manager.active_mappings.pop(hardware_id, None)
            manager.grab_state.desired_paths.pop(hardware_id, None)
            manager.grab_state.desired_grabs.pop(hardware_id, None)
        runtime_outputs.destroy_global_uinputs(manager, log=log)


async def release_all_devices(
    manager: _GrabManager, *, fire_and_observe_fn: FireAndObserve
) -> None:
    async with manager._op_lock:
        await manager.cancel_macro_playback()
        await runtime_combos.clear_combo_runtime(
            manager,
            deps=combo_runtime_deps(
                fire_and_observe_fn=fire_and_observe_fn,
                uinput_writer=manager.observed_uinput_writer,
                emit_mouse_move_fn=manager.emit_diagnostics_output_mouse_move,
            ),
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
    str_value_fn: StrValueFn,
    optional_str_fn: OptionalStrFn,
    int_value_fn: IntValueFn,
    int_or_none_fn: IntOrNoneFn,
    float_value_fn: FloatValueFn,
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
                    str_value=str_value_fn,
                    optional_str=optional_str_fn,
                    int_value=int_value_fn,
                    int_or_none=int_or_none_fn,
                    float_value=float_value_fn,
                )
            elif action_dict is not None:
                parsed_mapping[button_id] = runtime_actions.parse_action(
                    manager,
                    action_dict,
                    str_value=str_value_fn,
                    optional_str=optional_str_fn,
                    int_value=int_value_fn,
                    int_or_none=int_or_none_fn,
                    float_value=float_value_fn,
                )

        previous_mapping = dict(manager.active_mappings.get(hardware_id, {}))
        manager.active_mappings[hardware_id] = parsed_mapping
        for device in manager.grabbed_devices.get(hardware_id, []):
            await device.reset_mapping_runtime_state(previous_mapping=previous_mapping)
        log.info("Updated mapping for %s (%d buttons)", hardware_id, len(parsed_mapping))
        return {"updated": True, "hardware_id": hardware_id}
