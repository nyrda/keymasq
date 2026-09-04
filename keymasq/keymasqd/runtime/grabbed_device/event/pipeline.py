"""Grabbed-device event-loop and ordered pipeline orchestration.

Detailed classification, inspector, combo, passthrough, analog, and mapping
behavior lives in focused sibling modules. This module owns their ordering and
the event-loop recovery boundary.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable
from typing import cast

import evdev

from keymasq.common.devices import classify_event_device_type
from keymasq.keymasqd.runtime.action.triggers import source_trigger_id
from keymasq.keymasqd.runtime.adapters import identity_uinput_writer
from keymasq.keymasqd.runtime.grabbed_device import outputs
from keymasq.keymasqd.runtime.grabbed_device.event.analog import dispatch_analog_event
from keymasq.keymasqd.runtime.grabbed_device.event.classification import (
    EventClass,
    classify_event,
    get_event_name,
)
from keymasq.keymasqd.runtime.grabbed_device.event.combo import (
    finish_combo_passthrough_held_event,
    intercept_recalled_event,
    is_combo_passthrough_held_event,
    route_combo_callback_result,
)
from keymasq.keymasqd.runtime.grabbed_device.event.diagnostics import (
    log_raw_hardware_event,
    record_diagnostics,
)
from keymasq.keymasqd.runtime.grabbed_device.event.dispatch import (
    apply_fast_passthrough,
    apply_mapped_action_or_passthrough,
    clear_released_source_action,
    process_wheel_event,
)
from keymasq.keymasqd.runtime.grabbed_device.event.inspector import (
    broadcast_inspector_event,
    intercept_inspector_suppression,
)
from keymasq.keymasqd.runtime.grabbed_device.event.passthrough import (
    emit_passthrough_event,
    process_syn_event,
)
from keymasq.keymasqd.runtime.grabbed_device.types import (
    ActionExecutionDeps,
    AsyncioModule,
    EvdevModule,
    EventProcessingDeps,
    FireAndObserve,
    GrabbedDeviceRuntime,
    InputAccessMode,
    InputEventLike,
)
from keymasq.keymasqd.runtime.motion_controls import dispatch_motion_event


def fire_and_observe(coro: Awaitable[object], label: str) -> asyncio.Task[object]:
    task = asyncio.ensure_future(coro)

    def _log_task_result(done: asyncio.Task[object]) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            exc = done.exception()
            if exc is not None:
                logging.getLogger("keymasqd.devices").warning("%s failed: %s", label, exc)

    task.add_done_callback(_log_task_result)
    return task


def build_action_execution_deps(
    *, fire_and_observe_fn: FireAndObserve = fire_and_observe
) -> ActionExecutionDeps:
    return ActionExecutionDeps(
        asyncio_mod=cast(AsyncioModule, asyncio),
        fire_and_observe_fn=fire_and_observe_fn,
        evdev_mod=evdev,
        uinput_writer=identity_uinput_writer,
    )


def build_event_processing_deps(
    *,
    log: logging.Logger,
    fire_and_observe_fn: FireAndObserve = fire_and_observe,
) -> EventProcessingDeps:
    return EventProcessingDeps(
        evdev_mod=evdev,
        time_mod=time,
        log=log,
        classify_event_device_type_fn=classify_event_device_type,
        action_deps=build_action_execution_deps(fire_and_observe_fn=fire_and_observe_fn),
    )


async def event_loop(
    device_runtime: GrabbedDeviceRuntime,
    *,
    asyncio_mod: AsyncioModule,
    log: logging.Logger,
) -> None:
    error_backoff = 0.01
    device = device_runtime.device
    if device is None:
        return
    deps = build_event_processing_deps(log=log)

    try:
        async for event in device.async_read_loop():
            if not device_runtime.running:
                break
            try:
                await process_event(device_runtime, event, deps=deps)
                error_backoff = 0.01
            except Exception:
                if device_runtime.running:
                    await recover_from_event_processing_error(device_runtime)
                    log.exception(
                        "Event processing error on %s (backoff %.3fs)",
                        device_runtime.path,
                        error_backoff,
                    )
                    await asyncio_mod.sleep(error_backoff)
                    error_backoff = min(0.5, error_backoff * 2)
    except asyncio.CancelledError:
        pass
    except OSError as exc:
        if device_runtime.running:
            await cleanup_runtime_failure(device_runtime, log=log)
            log.warning("Device read error on %s: %s", device_runtime.path, exc)
            disconnect_callback = device_runtime.runtime_disconnect_callback
            if disconnect_callback is not None:
                try:
                    await disconnect_callback(device_runtime.hardware_id, device_runtime.path)
                except Exception:
                    log.exception(
                        "Failed to release disconnected device %s",
                        device_runtime.path,
                    )


async def cleanup_runtime_failure(
    device_runtime: GrabbedDeviceRuntime, *, log: logging.Logger
) -> None:
    if device_runtime.runtime_cleanup_callback is not None:
        try:
            await device_runtime.runtime_cleanup_callback(
                device_runtime.hardware_id,
                device_runtime.interface_id,
            )
        except Exception:
            log.exception(
                "Failed to clear combo runtime after device error on %s",
                device_runtime.path,
            )
    try:
        await device_runtime.reset_analog_controls()
    except Exception:
        log.exception(
            "Failed to reset analog controls after event error on %s",
            device_runtime.path,
        )
    try:
        await device_runtime.reset_superkeys()
    except Exception:
        log.exception("Failed to reset superkeys after event error on %s", device_runtime.path)
    observe_profile_trigger_end_for_held_sources(device_runtime)
    outputs.release_all_keys(
        device_runtime,
        evdev_mod=evdev,
        uinput_writer=identity_uinput_writer,
    )


async def recover_from_event_processing_error(device_runtime: GrabbedDeviceRuntime) -> None:
    await cleanup_runtime_failure(device_runtime, log=logging.getLogger("keymasqd.devices"))


def observe_profile_trigger_end_for_held_sources(
    device_runtime: GrabbedDeviceRuntime,
) -> None:
    event_names = set(device_runtime.state.held_source_keys)
    event_names.update(device_runtime.state.held_source_actions)
    event_names.update(device_runtime.state.held_profile_trigger_events)
    device_runtime.state.held_profile_trigger_events.clear()
    observer = device_runtime.profile_activation_trigger_end_observer
    if observer is None:
        return
    for event_name in sorted(event_names):
        observer(source_trigger_id(device_runtime.hardware_id, event_name))


def _observe_source_key_transition(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
) -> None:
    trigger_id = source_trigger_id(device_runtime.hardware_id, event_name)
    if int(event.value) == 1:
        device_runtime.state.held_source_keys.add(event_name)
        observer = device_runtime.profile_activation_trigger_start_observer
        if observer is not None:
            observer(trigger_id)
    elif int(event.value) == 0:
        device_runtime.state.held_source_keys.discard(event_name)
        observer = device_runtime.profile_activation_trigger_end_observer
        if observer is not None:
            observer(trigger_id)


def _intercept_paused_or_quarantined_input(
    device_runtime: GrabbedDeviceRuntime,
    *,
    event_class: EventClass,
    event_name: str,
    event_value: int,
) -> str | None:
    paused_getter = device_runtime.input_paused_getter
    paused = bool(paused_getter and paused_getter())
    quarantined = device_runtime.state.quarantined_source_keys

    if event_class is not EventClass.KEY:
        return "runtime_input_paused" if paused else None

    if paused:
        if event_value in {1, 2}:
            quarantined.add(event_name)
        elif event_value == 0:
            quarantined.discard(event_name)
        return "runtime_input_paused"

    if event_name not in quarantined:
        return None
    if event_value == 1:
        quarantined.discard(event_name)
        return None
    if event_value == 0:
        quarantined.discard(event_name)
    return "runtime_input_quarantined"


def _finish_diagnostics(
    device_runtime: GrabbedDeviceRuntime,
    label: str,
    started_ns: int,
    *,
    deps: EventProcessingDeps,
) -> None:
    record_diagnostics(
        device_runtime,
        label,
        started_ns,
        time_mod=deps.time_mod,
    )


def _is_motion_syn_dropped(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    *,
    evdev_mod: EvdevModule,
) -> bool:
    return (
        bool(device_runtime.motion_axis_bindings)
        and int(event.type) == int(evdev_mod.ecodes.EV_SYN)
        and int(event.code) == int(evdev_mod.ecodes.SYN_DROPPED)
    )


async def process_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    *,
    deps: EventProcessingDeps,
) -> None:
    """Run one input event through the ordered grabbed-device pipeline."""

    evdev_mod = deps.evdev_mod
    started_ns = deps.time_mod.perf_counter_ns()
    event_name = get_event_name(event, evdev_mod=evdev_mod)
    event_class = classify_event(
        event,
        evdev_mod=evdev_mod,
        analog_axis_bindings=device_runtime.analog_axis_bindings.keys(),
    )

    motion_drop_handled = _is_motion_syn_dropped(
        device_runtime,
        event,
        evdev_mod=evdev_mod,
    )
    if motion_drop_handled:
        # Stream loss invalidates held motion state even while actions are suppressed.
        await dispatch_motion_event(
            device_runtime,
            event,
            device_runtime.mapping_getter(),
            deps=deps.action_deps,
        )

    paused_label = _intercept_paused_or_quarantined_input(
        device_runtime,
        event_class=event_class,
        event_name=event_name,
        event_value=int(event.value),
    )
    if paused_label is not None:
        _finish_diagnostics(device_runtime, paused_label, started_ns, deps=deps)
        return

    broadcast_inspector_event(
        device_runtime,
        event,
        event_name,
        evdev_mod=evdev_mod,
    )
    log_raw_hardware_event(
        device_runtime,
        event,
        event_name,
        evdev_mod=evdev_mod,
        log=deps.log,
    )
    if event_class is EventClass.PASSTHROUGH_ECHO:
        _finish_diagnostics(
            device_runtime,
            "passthrough_echo_suppressed",
            started_ns,
            deps=deps,
        )
        return

    inspector_label = await intercept_inspector_suppression(
        device_runtime,
        event,
        event_name,
        evdev_mod=evdev_mod,
    )
    if inspector_label is not None:
        _finish_diagnostics(device_runtime, inspector_label, started_ns, deps=deps)
        return

    if device_runtime.access_mode is InputAccessMode.OBSERVE:
        motion_event = (int(event.type), int(event.code)) in (
            device_runtime.motion_axis_bindings
        ) or int(event.type) == int(evdev_mod.ecodes.EV_SYN)
        consumed = motion_drop_handled
        if motion_event and not motion_drop_handled:
            consumed = await dispatch_motion_event(
                device_runtime,
                event,
                device_runtime.mapping_getter(),
                deps=deps.action_deps,
            )
        _finish_diagnostics(
            device_runtime,
            "action_motion_control" if consumed else "observed_motion",
            started_ns,
            deps=deps,
        )
        return

    if device_runtime.motion_axis_bindings:
        motion_axis_event = (int(event.type), int(event.code)) in (
            device_runtime.motion_axis_bindings
        )
        motion_syn_event = int(event.type) == int(evdev_mod.ecodes.EV_SYN)
        if motion_axis_event or motion_syn_event:
            motion_consumed = motion_drop_handled
            if not motion_drop_handled:
                motion_consumed = await dispatch_motion_event(
                    device_runtime,
                    event,
                    device_runtime.mapping_getter(),
                    deps=deps.action_deps,
                )
            if motion_syn_event:
                syn_label = process_syn_event(device_runtime, event, evdev_mod=evdev_mod)
                _finish_diagnostics(
                    device_runtime,
                    "action_motion_control" if motion_consumed else syn_label,
                    started_ns,
                    deps=deps,
                )
                return
            if motion_consumed:
                _finish_diagnostics(
                    device_runtime,
                    "action_motion_control",
                    started_ns,
                    deps=deps,
                )
                return
            emit_passthrough_event(device_runtime, event, evdev_mod=evdev_mod)
            _finish_diagnostics(device_runtime, "passthrough_motion", started_ns, deps=deps)
            return

    event_is_key = event_class is EventClass.KEY
    if event_is_key:
        _observe_source_key_transition(device_runtime, event, event_name)

    recalled = intercept_recalled_event(
        device_runtime.state,
        event_is_key=event_is_key,
        event_name=event_name,
        event_value=int(event.value),
    )
    if recalled.stop_processing:
        if recalled.diagnostic_label is not None:
            _finish_diagnostics(
                device_runtime,
                recalled.diagnostic_label,
                started_ns,
                deps=deps,
            )
        return

    callback_result = await device_runtime.event_callback(
        device_runtime.hardware_id,
        device_runtime.path,
        event.type,
        event.code,
        event.value,
        device_runtime.stable_path,
        device_runtime.interface_id,
    )
    combo_route = route_combo_callback_result(
        callback_result,
        event_is_key=event_is_key,
        event_value=int(event.value),
        event_name=event_name,
        held_source_actions=device_runtime.state.held_source_actions,
        combo_passthrough_held=device_runtime.state.combo_passthrough_held,
    )
    if combo_route.clear_released_source_action:
        clear_released_source_action(
            device_runtime,
            event,
            event_name,
            evdev_mod=evdev_mod,
        )
    if combo_route.stop_processing:
        return

    if recalled.suppress_release_after_callback:
        clear_released_source_action(
            device_runtime,
            event,
            event_name,
            evdev_mod=evdev_mod,
        )
        _finish_diagnostics(
            device_runtime,
            "combo_recalled_release_suppressed",
            started_ns,
            deps=deps,
        )
        return

    if event_class is EventClass.SYNCHRONIZATION:
        diag_label = process_syn_event(device_runtime, event, evdev_mod=evdev_mod)
        _finish_diagnostics(device_runtime, diag_label, started_ns, deps=deps)
        return

    if event_class is EventClass.ANALOG_AXIS:
        if await dispatch_analog_event(
            device_runtime,
            event,
            event_name,
            device_runtime.mapping_getter(),
            deps=deps.action_deps,
        ):
            _finish_diagnostics(
                device_runtime,
                "action_analog_control",
                started_ns,
                deps=deps,
            )
            return

    if event_class not in (EventClass.KEY, EventClass.RELATIVE):
        emit_passthrough_event(device_runtime, event, evdev_mod=evdev_mod)
        _finish_diagnostics(device_runtime, "passthrough_other", started_ns, deps=deps)
        return

    if is_combo_passthrough_held_event(
        device_runtime.state,
        event_is_key=event_is_key,
        event_name=event_name,
    ):
        emit_passthrough_event(
            device_runtime,
            event,
            evdev_mod=evdev_mod,
            event_name=event_name,
            remember_for_repeat=True,
            remember_after_emit=True,
        )
        if finish_combo_passthrough_held_event(
            device_runtime.state,
            event_name=event_name,
            event_value=int(event.value),
        ):
            clear_released_source_action(
                device_runtime,
                event,
                event_name,
                evdev_mod=evdev_mod,
            )
        _finish_diagnostics(
            device_runtime,
            "combo_passthrough_held",
            started_ns,
            deps=deps,
        )
        return

    recording_manager = device_runtime.recording_manager
    recording_active = bool(recording_manager and recording_manager.is_recording)
    mapping = device_runtime.mapping_getter()
    has_held_source_action = event_is_key and (
        event_name in device_runtime.state.held_source_actions
    )
    if event_class is EventClass.RELATIVE:
        wheel_diag_label = await process_wheel_event(
            device_runtime,
            event,
            event_name,
            mapping,
            recording_manager=recording_manager,
            deps=deps,
        )
        if wheel_diag_label is not None:
            _finish_diagnostics(
                device_runtime,
                wheel_diag_label,
                started_ns,
                deps=deps,
            )
            return

    if not mapping and not recording_active and not has_held_source_action:
        diag_label = apply_fast_passthrough(
            device_runtime,
            event,
            event_name,
            combo_passthrough_requested=combo_route.passthrough_requested,
            evdev_mod=evdev_mod,
        )
        _finish_diagnostics(device_runtime, diag_label, started_ns, deps=deps)
        return

    diag_label = await apply_mapped_action_or_passthrough(
        device_runtime,
        event,
        event_name,
        mapping,
        recording_manager=recording_manager,
        combo_consumed=combo_route.combo_consumed,
        combo_passthrough_requested=combo_route.passthrough_requested,
        deps=deps,
    )
    _finish_diagnostics(device_runtime, diag_label, started_ns, deps=deps)
