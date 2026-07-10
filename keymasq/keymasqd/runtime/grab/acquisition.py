"""Acquisition transaction for reconciling and grabbing evdev interfaces."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

import evdev

from keymasq.common.model.actions import MappingAction
from keymasq.keymasqd.combo_engine import ComboDecision
from keymasq.keymasqd.runtime import adapters, device_path_resolver
from keymasq.keymasqd.runtime.combo import events, lifecycle
from keymasq.keymasqd.runtime.combo.state import ComboRuntimeDeps
from keymasq.keymasqd.runtime.grab.outputs import (
    destroy_transaction_outputs,
    ensure_global_outputs,
)
from keymasq.keymasqd.runtime.grab.planning import (
    analog_input_bindings,
    build_grab_plan,
    device_has_mapped_buttons,
    grabbed_device_claim_paths,
    log_grab_request,
    persist_desired_grab,
    store_grabbed_devices,
    update_existing_devices,
)
from keymasq.keymasqd.runtime.grab.recovery import rollback_failed_grab_report
from keymasq.keymasqd.runtime.grab.release import (
    cancel_pending_hardware_release,
    cancel_pending_interface_release,
    release_interface,
    schedule_interface_release,
)
from keymasq.keymasqd.runtime.grab.source_hiding import (
    disable_hardware_hotplug_hiding_if_unused_best_effort,
    enable_hardware_hotplug_hiding_best_effort,
)
from keymasq.keymasqd.runtime.grab.state import (
    GrabAcquisitionState,
    GrabDeviceDeps,
    GrabManager,
    GrabPlan,
    GrabRequest,
    ManagedGrabbedDevice,
)
from keymasq.keymasqd.runtime.grab.support import combo_runtime_deps

log = logging.getLogger("keymasqd.devices")


@dataclass(frozen=True)
class RuntimeCallbacks:
    combo_deps: ComboRuntimeDeps
    event_callback: Callable[..., Awaitable[ComboDecision | bool | None]]
    runtime_cleanup_callback: Callable[[str, str | None], Awaitable[None]]
    runtime_disconnect_callback: Callable[[str, str], Awaitable[None]]


async def grab_device_unlocked(
    manager: GrabManager,
    request: GrabRequest,
    deps: GrabDeviceDeps,
) -> dict[str, object]:
    """Execute a planned acquisition transaction while the manager lock is held."""

    deps.clear_device_path_cache_fn()
    device_path_resolver.clear_cached_devices()
    cancel_pending_hardware_release(manager, request.hardware_id)

    plan = build_grab_plan(manager, request, deps)
    if request.update_desired:
        persist_desired_grab(manager, request, plan, deps)
    log_grab_request(plan)
    update_existing_devices(plan, request, deps)
    reconcile_existing_interface_releases(manager, request.hardware_id, plan, deps)
    callbacks = build_runtime_callbacks(manager, deps)
    state = GrabAcquisitionState(devices=list(plan.existing_devices))

    for path in sorted(plan.requested_paths):
        await grab_one_interface(manager, request, plan, deps, callbacks, state, path)

    return await finalize_grab(manager, request, plan, deps, state)


def reconcile_existing_interface_releases(
    manager: GrabManager,
    hardware_id: str,
    plan: GrabPlan,
    deps: GrabDeviceDeps,
) -> None:
    """Cancel pending releases for kept interfaces and schedule disappeared ones."""

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
            asyncio_mod=adapters.ASYNCIO_RUNTIME,
            log=log,
        )


def build_runtime_callbacks(
    manager: GrabManager,
    deps: GrabDeviceDeps,
) -> RuntimeCallbacks:
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
        return await events.on_device_event(
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
        await lifecycle.clear_combo_runtime_for_binding_scope(
            manager,
            cleanup_hardware_id,
            cleanup_source,
            deps=combo_deps,
        )

    async def runtime_disconnect_callback(
        disconnected_hardware_id: str,
        disconnected_path: str,
    ) -> None:
        await release_interface(
            manager,
            disconnected_hardware_id,
            disconnected_path,
        )

    return RuntimeCallbacks(
        combo_deps=combo_deps,
        event_callback=event_callback,
        runtime_cleanup_callback=runtime_cleanup_callback,
        runtime_disconnect_callback=runtime_disconnect_callback,
    )


def construct_grabbed_device(
    manager: GrabManager,
    request: GrabRequest,
    plan: GrabPlan,
    deps: GrabDeviceDeps,
    callbacks: RuntimeCallbacks,
    path: str,
    interface_id: str,
    probe_device: object,
) -> ManagedGrabbedDevice:
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
        inspector_suppressed_ids_getter=(manager.device_inspector_suppressed_hardware_ids_snapshot),
        inspector_suppression_disabler=manager.disable_device_inspector_suppression,
        profile_activation_recorder=manager.record_profile_action,
        profile_activation_trigger_start_observer=(manager.observe_profile_trigger_start),
        profile_activation_trigger_end_observer=manager.observe_profile_trigger_end,
        suppress_rel_getter=lambda: manager.macro_state.mouse_rel_suppressed,
        mouse_rel_suppression_start_callback=lambda: None,
        diagnostics_recorder=diagnostics_recorder,
        runtime_cleanup_callback=callbacks.runtime_cleanup_callback,
        runtime_disconnect_callback=callbacks.runtime_disconnect_callback,
        repeat_state=manager.repeat_state,
        interface_id=interface_id,
    )


def probe_interface_device_sync(
    manager: GrabManager,
    path: str,
) -> tuple[ManagedGrabbedDevice, dict[int, Sequence[object]]]:
    probe_device = manager._device_input(path)
    try:
        caps = cast(dict[int, Sequence[object]], probe_device.capabilities())
    except Exception:
        adapters.close_device(probe_device)
        raise
    return probe_device, caps


async def grab_one_interface(
    manager: GrabManager,
    request: GrabRequest,
    plan: GrabPlan,
    deps: GrabDeviceDeps,
    callbacks: RuntimeCallbacks,
    state: GrabAcquisitionState,
    path: str,
) -> None:
    if path in plan.existing_by_claim_path:
        return

    raw_device: Any | None = None
    try:
        probe_device, caps = await adapters.ASYNCIO_RUNTIME.to_thread(
            probe_interface_device_sync,
            manager,
            path,
        )
        raw_device = probe_device
        state.available_count += 1
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
            ensure_global_outputs(
                manager,
                request.hardware_id,
                state,
                log=log,
            )
            device = construct_grabbed_device(
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
            store_grabbed_devices(manager, request.hardware_id, state.devices)
            try:
                await grab_with_retry(
                    device,
                    path,
                    asyncio_mod=adapters.ASYNCIO_RUNTIME,
                    log=log,
                    errno_mod=deps.errno_mod,
                )
            except (asyncio.CancelledError, Exception):
                state.devices.remove(device)
                store_grabbed_devices(manager, request.hardware_id, state.devices)
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
            adapters.close_device(raw_device)
            raw_device = None
        if exc.errno in {deps.errno_mod.ENOENT, deps.errno_mod.ENODEV}:
            log.info(
                "Skipping unavailable interface for %s: %s",
                request.hardware_id,
                path,
            )
            return
        rollback = await rollback_failed_grab_report(
            manager,
            request,
            plan,
            state,
            path,
            exc,
        )
        raise rollback.reported_exception from exc
    except Exception as exc:
        if raw_device is not None:
            adapters.close_device(raw_device)
            raw_device = None
        rollback = await rollback_failed_grab_report(
            manager,
            request,
            plan,
            state,
            path,
            exc,
        )
        raise rollback.reported_exception from exc
    finally:
        if raw_device is not None:
            adapters.close_device(raw_device)


async def finalize_grab(
    manager: GrabManager,
    request: GrabRequest,
    plan: GrabPlan,
    deps: GrabDeviceDeps,
    state: GrabAcquisitionState,
) -> dict[str, object]:
    """Commit acquired devices, source-hiding state, and the public result payload."""

    del deps  # Part of the transaction contract; finalization needs only resolved state.
    waiting_for_device = state.is_waiting_for_device(plan)
    if (
        not waiting_for_device
        and request.hardware_id not in manager.grabbed_devices
        and plan.requested_paths
        and (plan.mapped_evdev_names or plan.mapped_bindings)
        and state.grabbed_count == 0
    ):
        destroy_transaction_outputs(manager, state, log=log)
        raise ValueError(
            f"No interfaces for {request.hardware_id} matched mapped buttons "
            f"(paths={len(plan.requested_paths)}, mapped_names={len(plan.mapped_evdev_names)}, "
            f"mapped_bindings={len(plan.mapped_bindings)})"
        )

    store_grabbed_devices(manager, request.hardware_id, state.devices)

    log.info(
        "Configured device %s: total_interfaces=%d newly_grabbed=%d skipped=%d",
        request.hardware_id,
        len(state.devices),
        state.grabbed_count,
        state.skipped_count,
    )
    if request.update_desired:
        if plan.requests_gamepad_source_hiding and waiting_for_device:
            await enable_hardware_hotplug_hiding_best_effort(
                manager,
                request.hardware_id,
            )
        else:
            await disable_hardware_hotplug_hiding_if_unused_best_effort(
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


async def grab_with_retry(
    device: ManagedGrabbedDevice,
    path: str,
    *,
    asyncio_mod: adapters.AsyncioRuntimeAdapter,
    log: logging.Logger,
    errno_mod: adapters.ErrnoModule,
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
