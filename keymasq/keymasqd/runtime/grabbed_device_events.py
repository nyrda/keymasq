import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Hashable, Mapping
from typing import cast

import evdev

from keymasq.common.combos import normalize_combo_evdev
from keymasq.common.devices import (
    canonical_gamepad_button_name,
    classify_event_device_type,
    high_res_wheel_low_res_code,
    normalize_evdev_binding_value,
    normalize_wheel_value,
    wheel_button_id,
)
from keymasq.common.models import ActionType, MappingAction
from keymasq.common.types import SyntheticInputEvent as _SyntheticInputEvent
from keymasq.keymasqd.combo_engine import ComboDecision
from keymasq.keymasqd.runtime import analog_controls as runtime_analog_controls
from keymasq.keymasqd.runtime import grabbed_device_actions as runtime_actions
from keymasq.keymasqd.runtime import grabbed_device_outputs as runtime_outputs
from keymasq.keymasqd.runtime import repeat as runtime_repeat
from keymasq.keymasqd.runtime.action_runner import source_trigger_id
from keymasq.keymasqd.runtime.adapters import identity_uinput_writer
from keymasq.keymasqd.runtime.grabbed_device_types import (
    ActionExecutionDeps,
    AsyncioModule,
    EvdevModule,
    EventProcessingDeps,
    FireAndObserve,
    GrabbedDeviceRuntime,
    InputEventLike,
    TimeModule,
    runtime_is_running,
)

_PASSTHROUGH_ECHO_EVENT_TYPES = frozenset(
    int(code)
    for code in (
        getattr(evdev.ecodes, "EV_FF", None),
        getattr(evdev.ecodes, "EV_LED", None),
        getattr(evdev.ecodes, "EV_SND", None),
    )
    if isinstance(code, int)
)


def _fire_and_observe(coro: Awaitable[object], label: str) -> asyncio.Task[object]:
    task = asyncio.ensure_future(coro)

    def _log_task_result(done: asyncio.Task[object]) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            exc = done.exception()
            if exc is not None:
                logging.getLogger("keymasqd.devices").warning("%s failed: %s", label, exc)

    task.add_done_callback(_log_task_result)
    return task


def build_action_execution_deps(
    *, fire_and_observe_fn: FireAndObserve = _fire_and_observe
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
    fire_and_observe_fn: FireAndObserve = _fire_and_observe,
) -> EventProcessingDeps:
    return EventProcessingDeps(
        evdev_mod=evdev,
        time_mod=time,
        log=log,
        classify_event_device_type_fn=classify_event_device_type,
        action_deps=build_action_execution_deps(fire_and_observe_fn=fire_and_observe_fn),
    )


def _evdev_code_name(raw_name: object, fallback: int) -> str:
    if isinstance(raw_name, tuple):
        names = cast(tuple[object, ...], raw_name)
        first: object = names[0] if names else str(fallback)
        return str(first).lower()
    return str(raw_name).lower()


def _event_code_int(value: object) -> int | None:
    try:
        return int(cast(int | float | str | bytes, value))
    except (TypeError, ValueError):
        return None


def _evdev_code_names(
    event_type: object, *, evdev_mod: EvdevModule
) -> Mapping[object, object] | None:
    if not isinstance(event_type, Hashable):
        return None
    ecodes = cast(object, getattr(evdev_mod, "ecodes", None))
    raw_bytype = cast(object, getattr(ecodes, "bytype", None))
    if not isinstance(raw_bytype, Mapping):
        return None
    bytype = cast(Mapping[object, object], raw_bytype)
    raw_names = bytype.get(event_type)
    if not isinstance(raw_names, Mapping):
        return None
    return cast(Mapping[object, object], raw_names)


def _record_diagnostics(
    device_runtime: GrabbedDeviceRuntime,
    label: str,
    started_ns: int,
    *,
    time_mod: TimeModule,
) -> None:
    if device_runtime.diagnostics_recorder is None:
        return
    device_runtime.diagnostics_recorder(
        label,
        (time_mod.perf_counter_ns() - started_ns) / 1000.0,
    )


def _log_raw_hardware_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    *,
    evdev_mod: EvdevModule,
    log: logging.Logger,
) -> None:
    if device_runtime.verbosity < 3:
        return
    if event.type == evdev_mod.ecodes.EV_SYN:
        return
    if event.type == evdev_mod.ecodes.EV_REL and event.code in (
        evdev_mod.ecodes.REL_X,
        evdev_mod.ecodes.REL_Y,
    ):
        return
    log.debug(
        "[hw %s %s] type=%s code=%s name=%s value=%s",
        device_runtime.hardware_id,
        device_runtime.interface_id,
        event.type,
        event.code,
        event_name,
        event.value,
    )


def _log_mapped_action(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction | None,
    event: InputEventLike,
    event_name: str,
    *,
    evdev_mod: EvdevModule,
    log: logging.Logger,
) -> None:
    if device_runtime.verbosity < 2:
        return
    if event.type == evdev_mod.ecodes.EV_REL and event.code in (
        evdev_mod.ecodes.REL_X,
        evdev_mod.ecodes.REL_Y,
    ):
        return
    if action is None:
        log.debug(
            "[%s] %s (%s) -> PASSTHROUGH value=%s",
            device_runtime.hardware_id,
            event_name,
            event.code,
            event.value,
        )
        return
    if action.action_type == ActionType.SUPPRESS:
        log.debug(
            "[%s] %s (%s) -> SUPPRESS value=%s",
            device_runtime.hardware_id,
            event_name,
            event.code,
            event.value,
        )
        return
    if action.action_type in (
        ActionType.KEYBOARD,
        ActionType.MOUSE,
        ActionType.GAMEPAD,
        ActionType.GAMEPAD_AXIS,
    ):
        target = action.target or "?"
        mods: list[str] = []
        if action.rapidfire_enabled:
            mods.append(f"rf:{action.rapidfire_hold_ms}/{action.rapidfire_wait_ms}")
        if action.tap_enabled:
            mods.append(f"tap:{action.tap_hold_ms}")
        mod_str = f" [{', '.join(mods)}]" if mods else ""
        log.debug(
            "[%s] %s (%s) -> %s:%s%s value=%s",
            device_runtime.hardware_id,
            event_name,
            event.code,
            action.action_type.value,
            f"{target}={int(action.axis_value)}"
            if action.action_type == ActionType.GAMEPAD_AXIS
            else target,
            mod_str,
            event.value,
        )
        return
    if action.action_type in (
        ActionType.MOUSE_MOVE_REL,
        ActionType.MOUSE_MOVE_ABS,
        ActionType.MOUSE_MOVE_NATURAL_ABS,
    ):
        log.debug(
            "[%s] %s (%s) -> %s x=%s y=%s value=%s",
            device_runtime.hardware_id,
            event_name,
            event.code,
            action.action_type.value,
            int(action.move_x),
            int(action.move_y),
            event.value,
        )
        return
    if action.action_type == ActionType.EXEC:
        log.debug(
            "[%s] %s (%s) -> EXEC %s value=%s",
            device_runtime.hardware_id,
            event_name,
            event.code,
            action.cmd or "",
            event.value,
        )
        return
    if action.action_type == ActionType.SUPERKEY:
        sk_name = action.superkey_config.name if action.superkey_config else "?"
        log.debug(
            "[%s] %s (%s) -> SUPERKEY:%s value=%s",
            device_runtime.hardware_id,
            event_name,
            event.code,
            sk_name,
            event.value,
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
            if not runtime_is_running(device_runtime):
                break
            try:
                await process_event(
                    device_runtime,
                    event,
                    deps=deps,
                )
                error_backoff = 0.01
            except Exception:
                if runtime_is_running(device_runtime):
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
        if runtime_is_running(device_runtime):
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
    runtime_outputs.release_all_keys(
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


def get_event_name(event: InputEventLike, *, evdev_mod: EvdevModule) -> str:
    raw_code: object = event.code
    code = _event_code_int(raw_code)
    if code is None:
        return str(raw_code)
    names = _evdev_code_names(event.type, evdev_mod=evdev_mod)
    if names is None:
        return str(event.code)
    raw_code_name = names.get(code, str(raw_code))
    return _evdev_code_name(raw_code_name, code)


def get_key_name(code: int, *, evdev_mod: EvdevModule) -> str | None:
    ecodes = cast(object, getattr(evdev_mod, "ecodes", None))
    ev_key = cast(object, getattr(ecodes, "EV_KEY", None))
    names = _evdev_code_names(ev_key, evdev_mod=evdev_mod)
    if names is None:
        return None
    raw_code_name = names.get(code, str(code))
    return _evdev_code_name(raw_code_name, code)


def _inspector_active(device_runtime: GrabbedDeviceRuntime) -> bool:
    getter = device_runtime.inspector_active_getter
    return bool(getter and getter(device_runtime.hardware_id))


def _inspector_suppressed(device_runtime: GrabbedDeviceRuntime) -> bool:
    getter = device_runtime.inspector_suppression_getter
    return bool(getter and getter(device_runtime.hardware_id))


def _inspector_suppressed_hardware_ids(device_runtime: GrabbedDeviceRuntime) -> set[str]:
    getter = device_runtime.inspector_suppressed_ids_getter
    if getter is None:
        return {device_runtime.hardware_id} if _inspector_suppressed(device_runtime) else set()
    suppressed_ids: set[str] = set()
    for hardware_id in getter():
        normalized = str(hardware_id or "").strip()
        if normalized:
            suppressed_ids.add(normalized)
    return suppressed_ids


def _event_time_us(event: InputEventLike) -> int:
    sec = cast(object, getattr(event, "sec", None))
    usec = cast(object, getattr(event, "usec", None))
    if sec is None or usec is None:
        return 0
    try:
        return int(cast(int | float | str | bytes, sec)) * 1_000_000 + int(
            cast(int | float | str | bytes, usec)
        )
    except (TypeError, ValueError):
        return 0


def _event_type_name(event_type: int, *, evdev_mod: EvdevModule) -> str:
    raw_events_obj = cast(object, getattr(evdev_mod.ecodes, "EV", {}))
    if isinstance(raw_events_obj, dict):
        raw_events = cast(dict[object, object], raw_events_obj)
        value = raw_events.get(int(event_type))
        if value is not None:
            return str(value)
    return str(int(event_type))


def _inspector_control_id(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    *,
    evdev_mod: EvdevModule,
) -> str:
    normalized_value = normalize_evdev_binding_value(int(event.type), int(event.value))
    button_id = device_runtime.event_binding_to_button.get(
        (int(event.type), int(event.code), normalized_value)
    )
    if button_id:
        return button_id
    button_id = device_runtime.event_code_to_button.get((int(event.type), int(event.code)))
    if button_id:
        return button_id
    if int(event.type) == int(evdev_mod.ecodes.EV_KEY):
        return device_runtime.evdev_to_button.get(event_name.lower(), "")
    return ""


def _broadcast_inspector_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    *,
    evdev_mod: EvdevModule,
) -> None:
    callback = device_runtime.inspector_event_callback
    if callback is None or not _inspector_active(device_runtime):
        return

    control_id = _inspector_control_id(
        device_runtime,
        event,
        event_name,
        evdev_mod=evdev_mod,
    )
    analog_id = ""
    analog_role = ""
    analog_binding = device_runtime.analog_axis_bindings.get((int(event.type), int(event.code)))
    if analog_binding is not None:
        analog_id, analog_role = analog_binding

    action_type = ""
    mapping = device_runtime.mapping_getter()
    mapped_id = analog_id or control_id
    if mapped_id:
        action = mapping.get(mapped_id)
        if action is not None:
            action_type = action.action_type.value

    callback(
        {
            "hardware_id": device_runtime.hardware_id,
            "path": device_runtime.path,
            "stable_path": device_runtime.stable_path,
            "source": device_runtime.interface_id,
            "time_us": _event_time_us(event),
            "type": int(event.type),
            "type_name": _event_type_name(int(event.type), evdev_mod=evdev_mod),
            "code": int(event.code),
            "code_name": event_name,
            "value": int(event.value),
            "control_id": control_id,
            "analog_id": analog_id,
            "analog_role": analog_role,
            "action_type": action_type,
            "suppressed": _inspector_suppressed(device_runtime),
        }
    )


def _is_inspector_escape_press(
    event: InputEventLike,
    event_name: str,
    *,
    evdev_mod: EvdevModule,
) -> bool:
    return (
        int(event.type) == int(evdev_mod.ecodes.EV_KEY)
        and int(event.value) == 1
        and (
            event_name == "key_esc"
            or int(event.code) == int(getattr(evdev_mod.ecodes, "KEY_ESC", -1))
        )
    )


async def process_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    *,
    deps: EventProcessingDeps,
) -> None:
    evdev_mod = deps.evdev_mod
    time_mod = deps.time_mod
    started_ns = time_mod.perf_counter_ns()
    diag_label = "unknown"
    combo_consumed = False
    combo_passthrough_requested = False
    suppress_recalled_release_passthrough = False

    event_name = get_event_name(event, evdev_mod=evdev_mod)
    _broadcast_inspector_event(
        device_runtime,
        event,
        event_name,
        evdev_mod=evdev_mod,
    )
    _log_raw_hardware_event(
        device_runtime,
        event,
        event_name,
        evdev_mod=evdev_mod,
        log=deps.log,
    )
    if event.type in _PASSTHROUGH_ECHO_EVENT_TYPES:
        _record_diagnostics(
            device_runtime,
            "passthrough_echo_suppressed",
            started_ns,
            time_mod=time_mod,
        )
        return

    suppressed_hardware_ids = _inspector_suppressed_hardware_ids(device_runtime)
    if suppressed_hardware_ids and _is_inspector_escape_press(
        event,
        event_name,
        evdev_mod=evdev_mod,
    ):
        disabler = device_runtime.inspector_suppression_disabler
        if disabler is not None:
            for hardware_id in sorted(suppressed_hardware_ids):
                await disabler(hardware_id, "key_esc")
        _record_diagnostics(
            device_runtime,
            "inspector_escape_key",
            started_ns,
            time_mod=time_mod,
        )
        return
    if _inspector_suppressed(device_runtime):
        _record_diagnostics(
            device_runtime,
            "inspector_suppressed",
            started_ns,
            time_mod=time_mod,
        )
        return
    if event.type == evdev_mod.ecodes.EV_KEY:
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
    normalized_event_name = normalize_combo_evdev(event_name)
    if (
        event.type == evdev_mod.ecodes.EV_KEY
        and normalized_event_name in device_runtime.state.combo_recalled_bindings
    ):
        if int(event.value) == 2:
            _record_diagnostics(
                device_runtime,
                "combo_recalled_repeat_suppressed",
                started_ns,
                time_mod=time_mod,
            )
            return
        device_runtime.state.combo_recalled_bindings.discard(normalized_event_name)
        device_runtime.state.combo_passthrough_held.discard(event_name)
        if int(event.value) == 0:
            suppress_recalled_release_passthrough = True

    consumed = await device_runtime.event_callback(
        device_runtime.hardware_id,
        device_runtime.path,
        event.type,
        event.code,
        event.value,
        device_runtime.stable_path,
        device_runtime.interface_id,
    )
    if consumed is True:
        _clear_released_source_action(
            device_runtime,
            event,
            event_name,
            evdev_mod=evdev_mod,
        )
        return
    if isinstance(consumed, ComboDecision):
        if consumed.consume_current_event:
            if not (
                event.type == evdev_mod.ecodes.EV_KEY
                and int(event.value) == 0
                and (
                    event_name in device_runtime.state.held_source_actions
                    or event_name in device_runtime.state.combo_passthrough_held
                )
            ):
                return
            combo_consumed = True
        if consumed.passthrough_current_event:
            combo_passthrough_requested = True

    if suppress_recalled_release_passthrough:
        _clear_released_source_action(
            device_runtime,
            event,
            event_name,
            evdev_mod=evdev_mod,
        )
        _record_diagnostics(
            device_runtime,
            "combo_recalled_release_suppressed",
            started_ns,
            time_mod=time_mod,
        )
        return

    if event.type == evdev_mod.ecodes.EV_SYN:
        diag_label = _process_syn_event(
            device_runtime,
            event,
            evdev_mod=evdev_mod,
        )
        _record_diagnostics(device_runtime, diag_label, started_ns, time_mod=time_mod)
        return

    if event.type == evdev_mod.ecodes.EV_ABS and (
        int(event.type),
        int(event.code),
    ) in device_runtime.analog_axis_bindings:
        mapping = device_runtime.mapping_getter()
        if await runtime_analog_controls.process_analog_event(
            device_runtime,
            event,
            event_name,
            mapping,
            deps=deps.action_deps,
        ):
            _record_diagnostics(
                device_runtime,
                "action_analog_control",
                started_ns,
                time_mod=time_mod,
            )
            return

    if event.type not in (evdev_mod.ecodes.EV_KEY, evdev_mod.ecodes.EV_REL):
        runtime_outputs.passthrough(
            device_runtime,
            event,
            evdev_mod=evdev_mod,
            uinput_writer=identity_uinput_writer,
            sync=False,
        )
        _record_diagnostics(device_runtime, "passthrough_other", started_ns, time_mod=time_mod)
        return

    if (
        event.type == evdev_mod.ecodes.EV_KEY
        and event_name in device_runtime.state.combo_passthrough_held
    ):
        runtime_outputs.passthrough(
            device_runtime,
            event,
            evdev_mod=evdev_mod,
            uinput_writer=identity_uinput_writer,
            sync=False,
        )
        runtime_repeat.remember_passthrough_event(
            device_runtime.repeat_state,
            device_runtime,
            event,
            event_name,
            evdev_mod=evdev_mod,
        )
        if int(event.value) == 0:
            device_runtime.state.combo_passthrough_held.discard(event_name)
            _clear_released_source_action(
                device_runtime,
                event,
                event_name,
                evdev_mod=evdev_mod,
            )
        _record_diagnostics(device_runtime, "combo_passthrough_held", started_ns, time_mod=time_mod)
        return

    recording_manager = device_runtime.recording_manager
    recording_active = bool(recording_manager and recording_manager.is_recording)
    mapping = device_runtime.mapping_getter()
    has_held_source_action = (
        event.type == evdev_mod.ecodes.EV_KEY
        and event_name in device_runtime.state.held_source_actions
    )
    if event.type == evdev_mod.ecodes.EV_REL:
        wheel_diag_label = await _process_wheel_event(
            device_runtime,
            event,
            event_name,
            mapping,
            recording_manager=recording_manager,
            deps=deps,
        )
        if wheel_diag_label is not None:
            _record_diagnostics(
                device_runtime,
                wheel_diag_label,
                started_ns,
                time_mod=time_mod,
            )
            return
    if not mapping and not recording_active and not has_held_source_action:
        if (
            combo_passthrough_requested
            and event.type == evdev_mod.ecodes.EV_KEY
            and int(event.value) == 1
        ):
            device_runtime.state.combo_passthrough_held.add(event_name)
        runtime_repeat.remember_passthrough_event(
            device_runtime.repeat_state,
            device_runtime,
            event,
            event_name,
            evdev_mod=evdev_mod,
        )
        runtime_outputs.passthrough(
            device_runtime,
            event,
            evdev_mod=evdev_mod,
            uinput_writer=identity_uinput_writer,
            sync=False,
        )
        _record_profile_input_if_countable(
            device_runtime,
            event,
            event_name,
            evdev_mod=evdev_mod,
        )
        diag_label = "combo_passthrough" if combo_passthrough_requested else "passthrough_fast"
        _record_diagnostics(device_runtime, diag_label, started_ns, time_mod=time_mod)
        return

    diag_label = await _apply_mapped_action_or_passthrough(
        device_runtime,
        event,
        event_name,
        mapping,
        recording_manager=recording_manager,
        combo_consumed=combo_consumed,
        combo_passthrough_requested=combo_passthrough_requested,
        deps=deps,
    )
    _record_diagnostics(device_runtime, diag_label, started_ns, time_mod=time_mod)


def _process_syn_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    *,
    evdev_mod: EvdevModule,
) -> str:
    diag_label = "syn"
    event_code = int(event.code)
    syn_report = int(getattr(evdev_mod.ecodes, "SYN_REPORT", 0))
    syn_mt_report = int(getattr(evdev_mod.ecodes, "SYN_MT_REPORT", 0))
    if event_code == syn_report:
        passthrough_frame_open = runtime_outputs.passthrough_frame_open(
            device_runtime,
            device_runtime.uinput,
        )
        runtime_outputs.flush_passthrough_frame(
            device_runtime,
            device_runtime.uinput,
            uinput_writer=identity_uinput_writer,
        )
        if passthrough_frame_open:
            diag_label = "passthrough_syn"
    elif event_code == syn_mt_report:
        writer = identity_uinput_writer(device_runtime.uinput)
        if writer is not None:
            writer.write(evdev_mod.ecodes.EV_SYN, event_code, int(event.value))
            runtime_outputs.mark_passthrough_frame_open(
                device_runtime,
                device_runtime.uinput,
            )
    else:
        runtime_outputs.mark_passthrough_frame_closed(
            device_runtime,
            device_runtime.uinput,
        )
    return diag_label


async def _process_wheel_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    mapping: dict[str, MappingAction],
    *,
    recording_manager: object | None,
    deps: EventProcessingDeps,
) -> str | None:
    evdev_mod = deps.evdev_mod
    high_res_wheel_action = _find_high_res_wheel_low_res_action(
        device_runtime,
        event,
        mapping,
        evdev_mod=evdev_mod,
    )
    if (
        high_res_wheel_action is not None
        and high_res_wheel_action.action_type == ActionType.PASSTHROUGH
    ):
        _record_grabbed_event_if_allowed(
            device_runtime,
            event,
            recording_manager=recording_manager,
            deps=deps,
        )
        runtime_outputs.passthrough(
            device_runtime,
            event,
            evdev_mod=evdev_mod,
            uinput_writer=identity_uinput_writer,
            sync=False,
        )
        return "wheel_passthrough"
    if (
        high_res_wheel_action is not None
        and high_res_wheel_action.action_type != ActionType.PASSTHROUGH
    ):
        if not _is_recording_control_action(high_res_wheel_action):
            _record_grabbed_event_if_allowed(
                device_runtime,
                event,
                recording_manager=recording_manager,
                deps=deps,
            )
        return "wheel_high_res_suppressed"
    return await _process_wheel_pulse_event(
        device_runtime,
        event,
        event_name,
        mapping,
        recording_manager=recording_manager,
        deps=deps,
    )


async def _apply_mapped_action_or_passthrough(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    mapping: dict[str, MappingAction],
    *,
    recording_manager: object | None,
    combo_consumed: bool,
    combo_passthrough_requested: bool,
    deps: EventProcessingDeps,
) -> str:
    evdev_mod = deps.evdev_mod
    action = find_action_for_event(device_runtime, event, mapping)
    if event.type == evdev_mod.ecodes.EV_KEY:
        held_action = device_runtime.state.held_source_actions.get(event_name)
        if int(event.value) == 1 and event_name not in device_runtime.state.held_source_actions:
            device_runtime.state.held_source_actions[event_name] = action
        elif int(event.value) in (0, 2) and event_name in device_runtime.state.held_source_actions:
            action = held_action

    if not _is_recording_control_action(action):
        _record_grabbed_event_if_allowed(
            device_runtime,
            event,
            recording_manager=recording_manager,
            deps=deps,
        )

    _log_mapped_action(
        device_runtime,
        action,
        event,
        event_name,
        evdev_mod=evdev_mod,
        log=deps.log,
    )

    if action:
        _record_profile_action_if_countable(
            device_runtime,
            action,
            event,
            event_name,
            evdev_mod=evdev_mod,
        )
        await runtime_actions.execute_action(
            device_runtime,
            action,
            event,
            event_name,
            deps=deps.action_deps,
        )
        diag_label = (
            f"combo_release_action_{action.action_type.value}"
            if combo_consumed
            else f"action_{action.action_type.value}"
        )
    else:
        if (
            combo_passthrough_requested
            and event.type == evdev_mod.ecodes.EV_KEY
            and int(event.value) == 1
        ):
            device_runtime.state.combo_passthrough_held.add(event_name)
        runtime_repeat.remember_passthrough_event(
            device_runtime.repeat_state,
            device_runtime,
            event,
            event_name,
            evdev_mod=evdev_mod,
        )
        runtime_outputs.passthrough(
            device_runtime,
            event,
            evdev_mod=evdev_mod,
            uinput_writer=identity_uinput_writer,
            sync=False,
        )
        _record_profile_input_if_countable(
            device_runtime,
            event,
            event_name,
            evdev_mod=evdev_mod,
        )
        diag_label = (
            "combo_passthrough" if combo_passthrough_requested else "passthrough_mapped"
        )

    if event.type == evdev_mod.ecodes.EV_KEY and int(event.value) == 0:
        device_runtime.state.held_source_actions.pop(event_name, None)

    return diag_label


def _clear_released_source_action(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    *,
    evdev_mod: EvdevModule,
) -> None:
    if int(event.type) == int(evdev_mod.ecodes.EV_KEY) and int(event.value) == 0:
        device_runtime.state.held_source_actions.pop(event_name, None)


def _record_profile_input_if_countable(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    *,
    evdev_mod: EvdevModule,
) -> None:
    if int(event.type) != int(evdev_mod.ecodes.EV_KEY) or int(event.value) != 1:
        return
    recorder = device_runtime.profile_activation_recorder
    if recorder is not None:
        recorder(None, source_trigger_id(device_runtime.hardware_id, event_name))


def _record_profile_action_if_countable(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    evdev_mod: EvdevModule,
) -> None:
    if int(event.type) == int(evdev_mod.ecodes.EV_KEY) and int(event.value) != 1:
        return
    if int(event.type) == int(evdev_mod.ecodes.EV_REL):
        return
    recorder = device_runtime.profile_activation_recorder
    if recorder is not None:
        recorder(
            action.source_profile_name,
            source_trigger_id(device_runtime.hardware_id, event_name),
        )


def _is_recording_control_action(
    action: MappingAction | None,
) -> bool:
    return bool(
        action
        and action.action_type
        in (
            ActionType.START_MACRO_RECORDING,
            ActionType.STOP_MACRO_RECORDING,
            ActionType.PLAY_MACRO_SLOT,
            ActionType.CANCEL_MACRO_PLAYBACK,
            ActionType.EMERGENCY_RESET,
        )
    )


def _record_grabbed_event_if_allowed(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    *,
    recording_manager: object | None = None,
    deps: EventProcessingDeps,
) -> None:
    if recording_manager is None:
        recording_manager = device_runtime.recording_manager
    if not (recording_manager and bool(getattr(recording_manager, "is_recording", False))):
        return

    should_record_grabbed_event = getattr(
        recording_manager,
        "should_record_grabbed_event",
        None,
    )
    record_grabbed_event = True
    if callable(should_record_grabbed_event):
        record_grabbed_event = bool(
            should_record_grabbed_event(
                device_runtime.stable_path,
                device_runtime.device_types,
            )
        )
    if not record_grabbed_event:
        return

    input_event = cast(evdev.InputEvent, event)
    record_event = getattr(recording_manager, "record_event", None)
    if callable(record_event):
        record_event(
            deps.classify_event_device_type_fn(input_event, device_runtime.device_types),
            input_event,
        )


def find_action_for_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    mapping: dict[str, MappingAction],
) -> MappingAction | None:
    event_name = get_event_name(event, evdev_mod=evdev)
    return find_action_for_code(
        device_runtime,
        int(event.type),
        int(event.code),
        int(event.value),
        event_name,
        mapping,
    )


def find_action_for_code(
    device_runtime: GrabbedDeviceRuntime,
    event_type: int,
    event_code: int,
    event_value: int,
    event_name: str,
    mapping: dict[str, MappingAction],
) -> MappingAction | None:
    normalized_value = normalize_evdev_binding_value(int(event_type), int(event_value))
    button_id = device_runtime.event_binding_to_button.get(
        (int(event_type), int(event_code), normalized_value)
    )
    if button_id and button_id in mapping:
        return mapping[button_id]
    button_id = device_runtime.event_code_to_button.get((int(event_type), int(event_code)))
    if button_id and button_id in mapping:
        return mapping[button_id]
    if int(event_type) == evdev.ecodes.EV_REL:
        return None
    return find_action_for_name(
        device_runtime,
        event_name,
        mapping,
        canonical_gamepad_button_name_fn=canonical_gamepad_button_name,
    )


async def _process_wheel_pulse_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    mapping: dict[str, MappingAction],
    *,
    recording_manager: object | None,
    deps: EventProcessingDeps,
) -> str | None:
    evdev_mod = deps.evdev_mod
    if not _is_low_res_wheel_event(event, evdev_mod=evdev_mod):
        return None
    normalized_value = normalize_wheel_value(int(event.value))
    if normalized_value is None:
        return None

    action = find_action_for_code(
        device_runtime,
        int(event.type),
        int(event.code),
        int(event.value),
        event_name,
        mapping,
    )
    if action is None:
        return None

    if not _is_recording_control_action(action):
        _record_grabbed_event_if_allowed(
            device_runtime,
            event,
            recording_manager=recording_manager,
            deps=deps,
        )

    if action.action_type == ActionType.PASSTHROUGH:
        runtime_outputs.passthrough(
            device_runtime,
            event,
            evdev_mod=evdev_mod,
            uinput_writer=identity_uinput_writer,
            sync=False,
        )
        return "wheel_passthrough"
    if action.action_type == ActionType.SUPPRESS:
        return "action_suppress"

    button_id = find_button_id_for_code(
        device_runtime,
        int(event.type),
        int(event.code),
        int(event.value),
        mapping,
    )
    pulse_event_name = button_id or wheel_button_id(event_name, normalized_value) or event_name
    pulse_count = max(1, abs(int(event.value)))
    for _ in range(pulse_count):
        recorder = device_runtime.profile_activation_recorder
        if recorder is not None:
            recorder(
                action.source_profile_name,
                source_trigger_id(device_runtime.hardware_id, pulse_event_name),
            )
        await runtime_actions.execute_action_pulse(
            device_runtime,
            action,
            event,
            pulse_event_name,
            deps=deps.action_deps,
        )
    return f"action_{action.action_type.value}"


def _is_low_res_wheel_event(
    event: InputEventLike,
    *,
    evdev_mod: EvdevModule,
) -> bool:
    return int(event.type) == evdev_mod.ecodes.EV_REL and int(event.code) in {
        int(evdev_mod.ecodes.REL_WHEEL),
        int(evdev_mod.ecodes.REL_HWHEEL),
    }


def _find_high_res_wheel_low_res_action(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    mapping: dict[str, MappingAction],
    *,
    evdev_mod: EvdevModule,
) -> MappingAction | None:
    if int(event.type) != evdev_mod.ecodes.EV_REL:
        return None
    low_res_code = high_res_wheel_low_res_code(int(event.code))
    if low_res_code is None:
        return None
    normalized_value = normalize_wheel_value(int(event.value))
    if normalized_value is None:
        return None
    return find_action_for_code(
        device_runtime,
        int(evdev_mod.ecodes.EV_REL),
        low_res_code,
        normalized_value,
        get_event_name(
            _SyntheticInputEvent(
                int(evdev_mod.ecodes.EV_REL),
                low_res_code,
                normalized_value,
            ),
            evdev_mod=evdev_mod,
        ),
        mapping,
    )


def find_button_id_for_code(
    device_runtime: GrabbedDeviceRuntime,
    event_type: int,
    event_code: int,
    event_value: int,
    mapping: dict[str, MappingAction],
) -> str | None:
    normalized_value = normalize_evdev_binding_value(int(event_type), int(event_value))
    button_id = device_runtime.event_binding_to_button.get(
        (int(event_type), int(event_code), normalized_value)
    )
    if button_id and button_id in mapping:
        return button_id
    button_id = device_runtime.event_code_to_button.get((int(event_type), int(event_code)))
    if button_id and button_id in mapping:
        return button_id
    return None


def find_action_for_name(
    device_runtime: GrabbedDeviceRuntime,
    event_name: str,
    mapping: dict[str, MappingAction],
    *,
    canonical_gamepad_button_name_fn: Callable[[str], str],
) -> MappingAction | None:
    button_id = device_runtime.evdev_to_button.get(event_name.lower())
    if not button_id:
        canonical_name = canonical_gamepad_button_name_fn(event_name)
        if canonical_name != event_name.lower():
            button_id = device_runtime.evdev_to_button.get(canonical_name)

    if button_id and button_id in mapping:
        return mapping[button_id]

    return None
