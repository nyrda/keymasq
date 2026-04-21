import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import cast

import evdev

from keymasq.common.combos import normalize_combo_evdev
from keymasq.common.devices import (
    canonical_gamepad_button_name,
    classify_event_device_type,
    normalize_evdev_binding_value,
)
from keymasq.common.models import ActionType, MappingAction
from keymasq.keymasqd.combo_engine import ComboDecision
from keymasq.keymasqd.runtime import grabbed_device_actions as runtime_actions
from keymasq.keymasqd.runtime import grabbed_device_outputs as runtime_outputs
from keymasq.keymasqd.runtime.grabbed_device_types import (
    ActionExecutionDeps,
    AsyncioModule,
    EvdevModule,
    EventProcessingDeps,
    FireAndObserve,
    GrabbedDeviceRuntime,
    InputEventLike,
    TimeModule,
    identity_uinput_writer,
    runtime_is_running,
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
    if action.action_type in (ActionType.KEYBOARD, ActionType.MOUSE, ActionType.GAMEPAD):
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
            target,
            mod_str,
            event.value,
        )
        return
    if action.action_type in (ActionType.MOUSE_MOVE_REL, ActionType.MOUSE_MOVE_ABS):
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

    try:
        async for event in device.async_read_loop():
            if not runtime_is_running(device_runtime):
                break
            try:
                await process_event(
                    device_runtime,
                    event,
                    deps=build_event_processing_deps(log=log),
                )
                error_backoff = 0.01
            except Exception as exc:
                if runtime_is_running(device_runtime):
                    await recover_from_event_processing_error(device_runtime)
                    log.warning(
                        "Event processing error on %s: %s (backoff %.3fs)",
                        device_runtime.path,
                        exc,
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


async def cleanup_runtime_failure(
    device_runtime: GrabbedDeviceRuntime, *, log: logging.Logger
) -> None:
    if device_runtime.runtime_cleanup_callback is not None:
        try:
            await device_runtime.runtime_cleanup_callback(
                device_runtime.hardware_id,
                device_runtime.interface_id,
            )
        except Exception as exc:
            log.warning(
                "Failed to clear combo runtime after device error on %s: %s",
                device_runtime.path,
                exc,
            )
    try:
        await device_runtime.reset_superkeys()
    except Exception as exc:
        log.warning(
            "Failed to reset superkeys after event error on %s: %s", device_runtime.path, exc
        )
    runtime_outputs.release_all_keys(
        device_runtime,
        evdev_mod=evdev,
        uinput_writer=identity_uinput_writer,
    )


async def recover_from_event_processing_error(device_runtime: GrabbedDeviceRuntime) -> None:
    await cleanup_runtime_failure(device_runtime, log=logging.getLogger("keymasqd.devices"))


def get_event_name(event: InputEventLike, *, evdev_mod: EvdevModule) -> str:
    try:
        raw_code_name: object = evdev_mod.ecodes.bytype[event.type].get(
            event.code, str(event.code)
        )
        return _evdev_code_name(raw_code_name, int(event.code))
    except Exception:
        return str(event.code)


def get_key_name(code: int, *, evdev_mod: EvdevModule) -> str | None:
    try:
        raw_code_name: object = evdev_mod.ecodes.bytype[evdev_mod.ecodes.EV_KEY].get(
            code, str(code)
        )
        return _evdev_code_name(raw_code_name, code)
    except Exception:
        return None


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
    _log_raw_hardware_event(
        device_runtime,
        event,
        event_name,
        evdev_mod=evdev_mod,
        log=deps.log,
    )
    if event.type == evdev_mod.ecodes.EV_KEY:
        if int(event.value) == 1:
            device_runtime.state.held_source_keys.add(event_name)
        elif int(event.value) == 0:
            device_runtime.state.held_source_keys.discard(event_name)
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
        device_runtime.state.held_source_actions.pop(event_name, None)
        _record_diagnostics(
            device_runtime,
            "combo_recalled_release_suppressed",
            started_ns,
            time_mod=time_mod,
        )
        return

    if event.type == evdev_mod.ecodes.EV_SYN:
        _record_diagnostics(device_runtime, "syn", started_ns, time_mod=time_mod)
        return

    if event.type not in (evdev_mod.ecodes.EV_KEY, evdev_mod.ecodes.EV_REL):
        runtime_outputs.passthrough(
            device_runtime,
            event,
            evdev_mod=evdev_mod,
            uinput_writer=identity_uinput_writer,
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
        )
        if int(event.value) == 0:
            device_runtime.state.combo_passthrough_held.discard(event_name)
        _record_diagnostics(device_runtime, "combo_passthrough_held", started_ns, time_mod=time_mod)
        return

    recording_manager = device_runtime.recording_manager
    recording_active = bool(recording_manager and recording_manager.is_recording)
    mapping = device_runtime.mapping_getter()
    has_held_source_action = (
        event.type == evdev_mod.ecodes.EV_KEY
        and event_name in device_runtime.state.held_source_actions
    )
    if not mapping and not recording_active and not has_held_source_action:
        if (
            combo_passthrough_requested
            and event.type == evdev_mod.ecodes.EV_KEY
            and int(event.value) == 1
        ):
            device_runtime.state.combo_passthrough_held.add(event_name)
        runtime_outputs.passthrough(
            device_runtime,
            event,
            evdev_mod=evdev_mod,
            uinput_writer=identity_uinput_writer,
        )
        diag_label = "combo_passthrough" if combo_passthrough_requested else "passthrough_fast"
        _record_diagnostics(device_runtime, diag_label, started_ns, time_mod=time_mod)
        return

    action = find_action_for_event(device_runtime, event, mapping)
    if event.type == evdev_mod.ecodes.EV_KEY:
        held_action = device_runtime.state.held_source_actions.get(event_name)
        if int(event.value) == 1 and event_name not in device_runtime.state.held_source_actions:
            device_runtime.state.held_source_actions[event_name] = action
        elif int(event.value) in (0, 2) and event_name in device_runtime.state.held_source_actions:
            action = held_action

    if recording_active and recording_manager is not None and not _is_recording_control_action(
        action,
    ):
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
        if record_grabbed_event:
            input_event = cast(evdev.InputEvent, event)
            recording_manager.record_event(
                deps.classify_event_device_type_fn(input_event, device_runtime.device_types),
                input_event,
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
        runtime_outputs.passthrough(
            device_runtime,
            event,
            evdev_mod=evdev_mod,
            uinput_writer=identity_uinput_writer,
        )
        diag_label = "combo_passthrough" if combo_passthrough_requested else "passthrough_mapped"

    if event.type == evdev_mod.ecodes.EV_KEY and int(event.value) == 0:
        device_runtime.state.held_source_actions.pop(event_name, None)

    _record_diagnostics(device_runtime, diag_label, started_ns, time_mod=time_mod)


def _is_recording_control_action(
    action: MappingAction | None,
) -> bool:
    return bool(
        action
        and action.action_type
        in (
            ActionType.START_MACRO_RECORDING,
            ActionType.STOP_MACRO_RECORDING,
            ActionType.CANCEL_MACRO_PLAYBACK,
        )
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
