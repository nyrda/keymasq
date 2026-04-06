import asyncio
import contextlib
import errno
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from typing import Final, TypeVar, cast

import evdev

from keyforge.common.combos import normalize_combo_evdev
from keyforge.common.devices import (
    canonical_gamepad_button_name,
    classify_event_device_type,
    get_interface_id,
    resolve_stable_path,
)
from keyforge.common.ipc import CommandType
from keyforge.common.models import ActionType, DeviceType, MappingAction
from keyforge.keyforged.combo_engine import ComboDecision
from keyforge.keyforged.output_helpers import get_trigger_axis, resolve_output_code
from keyforge.keyforged.recording import RecordingManager
from keyforge.keyforged.runtime.grabbed_device_actions import (
    execute_action,
    track_superkey_output,
)
from keyforge.keyforged.runtime.grabbed_device_outputs import (
    emit_configured_mouse_move,
    ensure_key_released,
    ensure_trigger_released,
    passthrough,
    release_all_keys,
    write_key,
)
from keyforge.keyforged.runtime.grabbed_device_repeat import (
    rapidfire_key,
    start_rapidfire_task,
    tap_key,
    tap_move,
    tap_trigger,
)
from keyforge.keyforged.runtime.grabbed_device_types import (
    AsyncioEvent as _AsyncioEvent,
)
from keyforge.keyforged.runtime.grabbed_device_types import (
    AsyncioLoop as _AsyncioLoop,
)
from keyforge.keyforged.runtime.grabbed_device_types import (
    AsyncioModule as _AsyncioModule,
)
from keyforge.keyforged.runtime.grabbed_device_types import (
    BroadcastCallback,
    ClassifyEventDeviceTypeFn,
    DeviceEventCallback,
    GrabbedDeviceState,
    MacroPlayer,
    MappingGetter,
)
from keyforge.keyforged.runtime.grabbed_device_types import (
    ErrnoModule as _ErrnoModule,
)
from keyforge.keyforged.runtime.grabbed_device_types import (
    EvdevModule as _EvdevModule,
)
from keyforge.keyforged.runtime.grabbed_device_types import (
    GrabbedDeviceRuntime as _GrabbedDeviceRuntime,
)
from keyforge.keyforged.runtime.grabbed_device_types import (
    InputEventLike as _InputEventLike,
)
from keyforge.keyforged.runtime.grabbed_device_types import (
    ManagedInputDevice as _ManagedInputDevice,
)
from keyforge.keyforged.runtime.grabbed_device_types import (
    TimeModule as _TimeModule,
)
from keyforge.keyforged.runtime.grabbed_device_types import (
    WritableUInput as _WritableUInput,
)
from keyforge.keyforged.runtime.grabbed_device_types import (
    runtime_is_running as _runtime_is_running,
)
from keyforge.keyforged.runtime.outputs import uinput_identity
from keyforge.keyforged.superkey_state import SuperkeyMachine

log = logging.getLogger("keyforged.devices")
ACTIVE_KEY_IDLE_LOG_INTERVAL_S = 1.0
ACTIVE_KEY_IDLE_MAX_WAIT_S = 300.0
COMBO_HELD_REARM_MODIFIERS = frozenset({"shift", "ctrl", "alt", "meta"})

_T = TypeVar("_T")

__all__ = [
    "ASYNCIO_RUNTIME",
    "ACTIVE_KEY_IDLE_LOG_INTERVAL_S",
    "ACTIVE_KEY_IDLE_MAX_WAIT_S",
    "COMBO_HELD_REARM_MODIFIERS",
    "GrabbedDevice",
    "GrabbedDeviceState",
    "broadcast_grab_status",
    "cleanup_runtime_failure",
    "ensure_key_released",
    "ensure_trigger_released",
    "emit_configured_mouse_move",
    "event_loop",
    "execute_action",
    "find_action_for_code",
    "find_action_for_event",
    "find_action_for_name",
    "get_event_name",
    "get_key_name",
    "passthrough",
    "process_event",
    "rapidfire_key",
    "release_all_keys",
    "reconcile_startup_held_action",
    "seed_startup_held_actions",
    "start_rapidfire_task",
    "tap_key",
    "tap_move",
    "tap_trigger",
    "track_superkey_output",
    "wait_for_active_key_activity",
    "wait_for_active_keys_to_clear",
    "write_key",
]


class _AsyncioRuntimeAdapter:
    def get_running_loop(self) -> _AsyncioLoop:
        return asyncio.get_running_loop()

    def create_event(self) -> _AsyncioEvent:
        return asyncio.Event()

    def wait_for(self, aw: Awaitable[_T], timeout: float) -> Awaitable[_T]:
        return asyncio.wait_for(aw, timeout)

    async def sleep(self, delay: float, /) -> None:
        await asyncio.sleep(delay)

    def current_task(self) -> asyncio.Task[object] | None:
        return cast(asyncio.Task[object] | None, asyncio.current_task())

    def create_task(self, coro: Coroutine[object, object, _T], /) -> asyncio.Task[_T]:
        return asyncio.create_task(coro)

    def to_thread(
        self,
        func: Callable[..., _T],
        /,
        *args: object,
        **kwargs: object,
    ) -> Awaitable[_T]:
        return asyncio.to_thread(func, *args, **kwargs)


ASYNCIO_RUNTIME: Final[_AsyncioModule] = _AsyncioRuntimeAdapter()



def _device_input(path: str) -> _ManagedInputDevice:
    return cast(_ManagedInputDevice, evdev.InputDevice(path))


def _uinput_writer(device: object | None) -> _WritableUInput | None:
    return cast(_WritableUInput | None, device)


def _fire_and_observe(coro: Awaitable[object], label: str) -> asyncio.Task[object]:
    task = asyncio.ensure_future(coro)

    def _log_task_result(done: asyncio.Task[object]) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            exc = done.exception()
            if exc is not None:
                log.warning("%s failed: %s", label, exc)

    task.add_done_callback(_log_task_result)
    return task


def _evdev_code_name(raw_name: object, fallback: int) -> str:
    if isinstance(raw_name, tuple):
        names = cast(tuple[object, ...], raw_name)
        first: object = names[0] if names else str(fallback)
        return str(first).lower()
    return str(raw_name).lower()


async def event_loop(
    device_runtime: _GrabbedDeviceRuntime,
    *,
    asyncio_mod: _AsyncioModule,
    log: logging.Logger,
) -> None:
    error_backoff = 0.01
    device = device_runtime.device
    if device is None:
        return

    try:
        async for event in device.async_read_loop():
            if not _runtime_is_running(device_runtime):
                break
            try:
                await process_event(
                    device_runtime,
                    event,
                    evdev_mod=evdev,
                    time_mod=time,
                    log=log,
                    combo_decision_cls=ComboDecision,
                    classify_event_device_type_fn=classify_event_device_type,
                    action_type_enum=ActionType,
                )
                error_backoff = 0.01
            except Exception as exc:
                if _runtime_is_running(device_runtime):
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
        if _runtime_is_running(device_runtime):
            await cleanup_runtime_failure(device_runtime, log=log)
            log.warning("Device read error on %s: %s", device_runtime.path, exc)


async def cleanup_runtime_failure(
    device_runtime: _GrabbedDeviceRuntime, *, log: logging.Logger
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
    release_all_keys(device_runtime, evdev_mod=evdev, uinput_writer=_uinput_writer)


async def recover_from_event_processing_error(device_runtime: _GrabbedDeviceRuntime) -> None:
    await cleanup_runtime_failure(device_runtime, log=log)


def get_event_name(event: _InputEventLike, *, evdev_mod: _EvdevModule) -> str:
    try:
        raw_code_name: object = evdev_mod.ecodes.bytype[event.type].get(
            event.code, str(event.code)
        )
        return _evdev_code_name(raw_code_name, int(event.code))
    except Exception:
        return str(event.code)


def get_key_name(code: int, *, evdev_mod: _EvdevModule) -> str | None:
    try:
        raw_code_name: object = evdev_mod.ecodes.bytype[evdev_mod.ecodes.EV_KEY].get(
            code, str(code)
        )
        return _evdev_code_name(raw_code_name, code)
    except Exception:
        return None


async def broadcast_grab_status(
    device_runtime: _GrabbedDeviceRuntime,
    state: str,
    active_names: list[str],
    *,
    waited_s: float,
    command_type: type[CommandType],
    log: logging.Logger,
) -> None:
    if device_runtime.broadcast_callback is None:
        return
    try:
        await device_runtime.broadcast_callback(
            command_type.DEVICE_GRAB_STATUS,
            {
                "hardware_id": device_runtime.hardware_id,
                "path": device_runtime.path,
                "state": state,
                "active_keys": list(active_names),
                "waited_s": float(waited_s),
            },
        )
    except Exception as exc:
        log.warning("Failed to broadcast grab status for %s: %s", device_runtime.hardware_id, exc)


async def wait_for_active_key_activity(
    device_runtime: _GrabbedDeviceRuntime,
    timeout_s: float,
    *,
    asyncio_mod: _AsyncioModule,
    errno_mod: _ErrnoModule,
    log: logging.Logger,
) -> bool:
    if device_runtime.device is None:
        return False

    loop = asyncio_mod.get_running_loop()
    readable = asyncio_mod.create_event()
    fd = device_runtime.device.fileno()
    loop.add_reader(fd, readable.set)
    try:
        try:
            await asyncio_mod.wait_for(readable.wait(), timeout_s)
        except TimeoutError:
            return False
    finally:
        loop.remove_reader(fd)

    while True:
        try:
            event = device_runtime.device.read_one()
        except BlockingIOError:
            break
        except OSError as exc:
            if exc.errno in (errno_mod.EAGAIN, errno_mod.EWOULDBLOCK):
                break
            log.warning(
                "[%s] failed to drain pending events before grab: %s",
                device_runtime.hardware_id,
                exc,
            )
            break
        except Exception as exc:
            log.warning(
                "[%s] failed to drain pending events before grab: %s",
                device_runtime.hardware_id,
                exc,
            )
            break
        if event is None:
            break

    return True


async def wait_for_active_keys_to_clear(
    device_runtime: _GrabbedDeviceRuntime,
    *,
    asyncio_mod: _AsyncioModule,
    time_mod: _TimeModule,
    log: logging.Logger,
    active_key_idle_max_wait_s: float,
    active_key_idle_log_interval_s: float,
) -> None:
    if device_runtime.device is None:
        return

    started_at = time_mod.monotonic()
    warned = False
    last_log_at = 0.0
    while True:
        try:
            active_codes = list(
                await asyncio_mod.to_thread(device_runtime.device.active_keys) or []
            )
        except Exception as exc:
            log.warning(
                "[%s] failed to read active keys before grab: %s; proceeding with grab",
                device_runtime.hardware_id,
                exc,
            )
            return

        now = time_mod.monotonic()
        if not active_codes:
            if warned:
                await broadcast_grab_status(
                    device_runtime,
                    "ready",
                    [],
                    waited_s=now - started_at,
                    command_type=CommandType,
                    log=log,
                )
                log.info(
                    "[%s] active keys cleared, proceeding with grab", device_runtime.hardware_id
                )
            return
        active_names = [
            event_name
            for code in active_codes
            if (event_name := get_key_name(int(code), evdev_mod=evdev)) is not None
        ]
        summary = (
            ", ".join(active_names)
            if active_names
            else ", ".join(str(int(code)) for code in active_codes)
        )
        if now - started_at >= active_key_idle_max_wait_s:
            await broadcast_grab_status(
                device_runtime,
                "timed_out",
                active_names,
                waited_s=now - started_at,
                command_type=CommandType,
                log=log,
            )
            log.error(
                "[%s] timed out waiting %.1fs for active keys to clear before grab: %s",
                device_runtime.hardware_id,
                active_key_idle_max_wait_s,
                summary,
            )
            raise TimeoutError(
                "timed out waiting "
                f"{active_key_idle_max_wait_s:.1f}s for active keys to clear: {summary}"
            )
        if not warned:
            await broadcast_grab_status(
                device_runtime,
                "waiting",
                active_names,
                waited_s=now - started_at,
                command_type=CommandType,
                log=log,
            )
            log.warning(
                "[%s] delaying grab until keys are released: %s",
                device_runtime.hardware_id,
                summary,
            )
            warned = True
            last_log_at = now
        elif now - last_log_at >= active_key_idle_log_interval_s:
            await broadcast_grab_status(
                device_runtime,
                "waiting",
                active_names,
                waited_s=now - started_at,
                command_type=CommandType,
                log=log,
            )
            log.info(
                "[%s] still waiting to grab; active keys still down: %s",
                device_runtime.hardware_id,
                summary,
            )
            last_log_at = now

        next_heartbeat_at = last_log_at + active_key_idle_log_interval_s
        wait_timeout = min(
            active_key_idle_max_wait_s - (now - started_at),
            max(0.0, next_heartbeat_at - now),
        )
        if wait_timeout <= 0.0:
            continue
        await wait_for_active_key_activity(
            device_runtime,
            wait_timeout,
            asyncio_mod=asyncio_mod,
            errno_mod=errno,
            log=log,
        )


def seed_startup_held_actions(device_runtime: _GrabbedDeviceRuntime) -> None:
    if device_runtime.device is None:
        return

    try:
        active_codes = list(device_runtime.device.active_keys() or [])
    except Exception:
        return

    mapping = device_runtime.mapping_getter()
    for code in active_codes:
        event_name = get_key_name(int(code), evdev_mod=evdev)
        if not event_name or event_name in device_runtime.state.held_source_actions:
            continue
        action = find_action_for_code(device_runtime, int(code), event_name, mapping)
        device_runtime.state.held_source_actions[event_name] = action
        reconcile_startup_held_action(device_runtime, action, action_type_enum=ActionType)


def reconcile_startup_held_action(
    device_runtime: _GrabbedDeviceRuntime,
    action: MappingAction | None,
    *,
    action_type_enum: type[ActionType],
) -> None:
    if action is None or not action.target:
        return

    if action.action_type == action_type_enum.KEYBOARD:
        code = resolve_output_code(action.target)
        if code is not None:
            ensure_key_released(device_runtime, code, device_runtime.keyboard_uinput)
        return

    if action.action_type == action_type_enum.MOUSE:
        code = resolve_output_code(action.target)
        if code is not None:
            ensure_key_released(device_runtime, code, device_runtime.mouse_uinput)
        return

    if action.action_type == action_type_enum.GAMEPAD:
        is_trigger, axis_code = get_trigger_axis(action.target)
        if is_trigger and axis_code is not None:
            ensure_trigger_released(
                device_runtime,
                axis_code,
                evdev_mod=evdev,
                uinput_writer=_uinput_writer,
            )
            return
        code = resolve_output_code(action.target)
        if code is not None:
            ensure_key_released(device_runtime, code, device_runtime.gamepad_uinput)


async def process_event(
    device_runtime: _GrabbedDeviceRuntime,
    event: _InputEventLike,
    *,
    evdev_mod: _EvdevModule,
    time_mod: _TimeModule,
    log: logging.Logger,
    combo_decision_cls: type[ComboDecision],
    classify_event_device_type_fn: ClassifyEventDeviceTypeFn,
    action_type_enum: type[ActionType],
) -> None:
    started_ns = time_mod.perf_counter_ns()
    diag_label = "unknown"
    combo_consumed = False
    combo_passthrough_requested = False

    event_name = get_event_name(event, evdev_mod=evdev_mod)
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
    if isinstance(consumed, combo_decision_cls):
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

    if event.type == evdev_mod.ecodes.EV_SYN:
        diag_label = "syn"
        if device_runtime.diagnostics_recorder:
            device_runtime.diagnostics_recorder(
                diag_label,
                (time_mod.perf_counter_ns() - started_ns) / 1000.0,
            )
        return

    if event.type not in (evdev_mod.ecodes.EV_KEY, evdev_mod.ecodes.EV_REL):
        passthrough(device_runtime, event, evdev_mod=evdev_mod, uinput_writer=_uinput_writer)
        diag_label = "passthrough_other"
        if device_runtime.diagnostics_recorder:
            device_runtime.diagnostics_recorder(
                diag_label,
                (time_mod.perf_counter_ns() - started_ns) / 1000.0,
            )
        return

    if (
        event.type == evdev_mod.ecodes.EV_KEY
        and event_name in device_runtime.state.combo_passthrough_held
    ):
        passthrough(device_runtime, event, evdev_mod=evdev_mod, uinput_writer=_uinput_writer)
        if int(event.value) == 0:
            device_runtime.state.combo_passthrough_held.discard(event_name)
        diag_label = "combo_passthrough_held"
        if device_runtime.diagnostics_recorder:
            device_runtime.diagnostics_recorder(
                diag_label,
                (time_mod.perf_counter_ns() - started_ns) / 1000.0,
            )
        return

    recording_active = bool(
        device_runtime.recording_manager and device_runtime.recording_manager.is_recording
    )
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
        passthrough(device_runtime, event, evdev_mod=evdev_mod, uinput_writer=_uinput_writer)
        diag_label = "combo_passthrough" if combo_passthrough_requested else "passthrough_fast"
        if device_runtime.diagnostics_recorder:
            device_runtime.diagnostics_recorder(
                diag_label,
                (time_mod.perf_counter_ns() - started_ns) / 1000.0,
            )
        return

    action = find_action_for_event(device_runtime, event, mapping)
    if event.type == evdev_mod.ecodes.EV_KEY:
        held_action = device_runtime.state.held_source_actions.get(event_name)
        if int(event.value) == 1 and event_name not in device_runtime.state.held_source_actions:
            device_runtime.state.held_source_actions[event_name] = action
        elif int(event.value) in (0, 2) and event_name in device_runtime.state.held_source_actions:
            action = held_action

    if recording_active:
        if not (
            action
            and action.action_type
            in (
                action_type_enum.START_MACRO_RECORDING,
                action_type_enum.STOP_MACRO_RECORDING,
                action_type_enum.CANCEL_MACRO_PLAYBACK,
            )
        ):
            recording_manager = device_runtime.recording_manager
            if recording_manager is None:
                return
            input_event = cast(evdev.InputEvent, event)
            recording_manager.record_event(
                classify_event_device_type_fn(input_event, device_runtime.device_types),
                input_event,
            )

    if device_runtime.verbosity >= 2:
        if event.type == evdev_mod.ecodes.EV_REL and event.code in (
            evdev_mod.ecodes.REL_X,
            evdev_mod.ecodes.REL_Y,
        ):
            pass
        elif action:
            if action.action_type == action_type_enum.SUPPRESS:
                log.debug(
                    "[%s] %s (%s) -> SUPPRESS value=%s",
                    device_runtime.hardware_id,
                    event_name,
                    event.code,
                    event.value,
                )
            elif action.action_type in (
                action_type_enum.KEYBOARD,
                action_type_enum.MOUSE,
                action_type_enum.GAMEPAD,
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
                    target,
                    mod_str,
                    event.value,
                )
            elif action.action_type in (
                action_type_enum.MOUSE_MOVE_REL,
                action_type_enum.MOUSE_MOVE_ABS,
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
            elif action.action_type == action_type_enum.EXEC:
                log.debug(
                    "[%s] %s (%s) -> EXEC %s value=%s",
                    device_runtime.hardware_id,
                    event_name,
                    event.code,
                    action.cmd or "",
                    event.value,
                )
            elif action.action_type == action_type_enum.SUPERKEY:
                sk_name = action.superkey_config.name if action.superkey_config else "?"
                log.debug(
                    "[%s] %s (%s) -> SUPERKEY:%s value=%s",
                    device_runtime.hardware_id,
                    event_name,
                    event.code,
                    sk_name,
                    event.value,
                )
        else:
            log.debug(
                "[%s] %s (%s) -> PASSTHROUGH value=%s",
                device_runtime.hardware_id,
                event_name,
                event.code,
                event.value,
            )

    if action:
        await execute_action(
            device_runtime,
            action,
            event,
            event_name,
            asyncio_mod=ASYNCIO_RUNTIME,
            command_type=CommandType,
            fire_and_observe_fn=_fire_and_observe,
            action_type_enum=action_type_enum,
            superkey_machine_cls=SuperkeyMachine,
            evdev_mod=evdev_mod,
            uinput_writer=_uinput_writer,
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
        passthrough(device_runtime, event, evdev_mod=evdev_mod, uinput_writer=_uinput_writer)
        diag_label = "combo_passthrough" if combo_passthrough_requested else "passthrough_mapped"

    if event.type == evdev_mod.ecodes.EV_KEY and int(event.value) == 0:
        device_runtime.state.held_source_actions.pop(event_name, None)

    if device_runtime.diagnostics_recorder:
        device_runtime.diagnostics_recorder(
            diag_label, (time_mod.perf_counter_ns() - started_ns) / 1000.0
        )


def find_action_for_event(
    device_runtime: _GrabbedDeviceRuntime,
    event: _InputEventLike,
    mapping: dict[str, MappingAction],
) -> MappingAction | None:
    event_name = get_event_name(event, evdev_mod=evdev)
    return find_action_for_code(device_runtime, int(event.code), event_name, mapping)


def find_action_for_code(
    device_runtime: _GrabbedDeviceRuntime,
    event_code: int,
    event_name: str,
    mapping: dict[str, MappingAction],
) -> MappingAction | None:
    button_id = device_runtime.evdev_code_to_button.get(int(event_code))
    if button_id and button_id in mapping:
        return mapping[button_id]
    return find_action_for_name(
        device_runtime,
        event_name,
        mapping,
        canonical_gamepad_button_name_fn=canonical_gamepad_button_name,
    )


def find_action_for_name(
    device_runtime: _GrabbedDeviceRuntime,
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


class GrabbedDevice:
    def __init__(
        self,
        path: str,
        hardware_id: str,
        button_map: dict[str, str],
        mapping_getter: MappingGetter,
        event_callback: DeviceEventCallback,
        device_type: DeviceType = DeviceType.OTHER,
        device_types: list[str] | None = None,
        verbosity: int = 0,
        keyboard_uinput: evdev.UInput | None = None,
        mouse_uinput: evdev.UInput | None = None,
        gamepad_uinput: evdev.UInput | None = None,
        broadcast_callback: BroadcastCallback | None = None,
        recording_manager: RecordingManager | None = None,
        macro_player: MacroPlayer | None = None,
        suppress_rel_getter: Callable[[], bool] | None = None,
        mouse_rel_suppression_start_callback: Callable[[], None] | None = None,
        diagnostics_recorder: Callable[[str, float], None] | None = None,
        runtime_cleanup_callback: Callable[[str, str | None], Awaitable[None]] | None = None,
        button_codes: dict[str, int] | None = None,
    ) -> None:
        self.path = path
        self.hardware_id = hardware_id
        self.stable_path = resolve_stable_path(path)
        self.interface_id = str(get_interface_id(self.stable_path) or "").lower()
        self.button_map: dict[str, str] = {}
        self.evdev_to_button: dict[str, str] = {}
        self.evdev_code_to_button: dict[int, str] = {}
        self.update_button_map(button_map, button_codes)
        self.mapping_getter = mapping_getter
        self.event_callback = event_callback
        self.device_type = device_type
        self.device_types = device_types or [device_type.value]
        self.verbosity = verbosity
        self.device: _ManagedInputDevice | None = None
        self.uinput: evdev.UInput | None = None
        self.keyboard_uinput = keyboard_uinput
        self.mouse_uinput = mouse_uinput
        self.gamepad_uinput = gamepad_uinput
        self.broadcast_callback = broadcast_callback
        self.recording_manager: RecordingManager | None = recording_manager
        self.macro_player = macro_player
        self.suppress_rel_getter = suppress_rel_getter
        self.mouse_rel_suppression_start_callback = mouse_rel_suppression_start_callback
        self.diagnostics_recorder = diagnostics_recorder
        self.runtime_cleanup_callback = runtime_cleanup_callback
        self.task: asyncio.Task[None] | None = None
        self._running = False
        self.state = GrabbedDeviceState()

    def update_button_map(
        self,
        button_map: dict[str, str],
        button_codes: dict[str, int] | None = None,
    ) -> None:
        self.button_map = dict(button_map)
        self.evdev_to_button = {v.lower(): k for k, v in button_map.items()}
        self.evdev_code_to_button = {
            int(code): button_id for button_id, code in (button_codes or {}).items()
        }

    async def reset_mapping_runtime_state(self) -> None:
        for event_name in self.state.combo_passthrough_held:
            self.state.held_source_actions.setdefault(event_name, None)
        self.state.combo_passthrough_held.clear()
        await self.reset_superkeys()
        seed_startup_held_actions(self)

    async def reset_superkeys(self) -> None:
        for machine in self.state.superkey_machines.values():
            await machine.stop()
        self.state.superkey_machines.clear()

    async def grab(self) -> None:
        self.device = _device_input(self.path)
        caps = self.device.capabilities()
        caps.pop(evdev.ecodes.EV_SYN, None)

        passthrough_name, passthrough_vendor, passthrough_product = uinput_identity(
            f"keyforge-{self.hardware_id}",
            "passthrough",
            test_name=f"passthrough-{self.hardware_id}",
        )
        if passthrough_vendor is None or passthrough_product is None:
            self.uinput = evdev.UInput(
                events=cast(dict[int, Sequence[int]], caps),
                name=passthrough_name,
            )
        else:
            self.uinput = evdev.UInput(
                events=cast(dict[int, Sequence[int]], caps),
                name=passthrough_name,
                vendor=passthrough_vendor,
                product=passthrough_product,
            )

        try:
            await wait_for_active_keys_to_clear(
                self,
                asyncio_mod=ASYNCIO_RUNTIME,
                time_mod=time,
                log=log,
                active_key_idle_max_wait_s=ACTIVE_KEY_IDLE_MAX_WAIT_S,
                active_key_idle_log_interval_s=ACTIVE_KEY_IDLE_LOG_INTERVAL_S,
            )
            self.device.grab()
        except Exception:
            if self.uinput:
                try:
                    self.uinput.close()
                except Exception:
                    pass
                self.uinput = None
            raise

        self._running = True
        self.task = asyncio.create_task(event_loop(self, asyncio_mod=ASYNCIO_RUNTIME, log=log))

        log.info("Grabbed %s for %s", self.path, self.hardware_id)

    async def release(self) -> None:
        self._running = False
        release_all_keys(self, evdev_mod=evdev, uinput_writer=_uinput_writer)
        self.state.held_source_actions.clear()
        self.state.combo_passthrough_held.clear()

        await self.reset_superkeys()

        if self.task:
            self.task.cancel()
            try:
                await asyncio.wait_for(self.task, timeout=1.0)
            except (TimeoutError, asyncio.CancelledError):
                pass

        if self.device:
            try:
                self.device.ungrab()
            except Exception as exc:
                log.warning("Failed to ungrab %s: %s", self.path, exc)
            try:
                self.device.close()
            except Exception as exc:
                log.warning("Failed to close input device %s: %s", self.path, exc)

        if self.uinput:
            try:
                self.uinput.close()
            except Exception as exc:
                log.warning("Failed to close passthrough uinput for %s: %s", self.path, exc)

        self.device = None
        self.uinput = None

        log.info("Released %s", self.path)

    def release_tracked_outputs(self) -> None:
        release_all_keys(self, evdev_mod=evdev, uinput_writer=_uinput_writer)

    def emit_combo_release(self, evdev_name: str) -> None:
        if not self.uinput:
            return
        code = resolve_output_code(evdev_name)
        if code is None:
            return
        write_key(
            self,
            self.uinput,
            code,
            0,
            evdev_mod=evdev,
            uinput_writer=_uinput_writer,
        )

    def has_held_source_inputs(self) -> bool:
        return bool(self.state.held_source_actions)

    def combo_passthrough_held_modifiers(self) -> set[str]:
        return {
            event_name
            for event_name in self.state.combo_passthrough_held
            if normalize_combo_evdev(event_name) in COMBO_HELD_REARM_MODIFIERS
        }
