# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

import asyncio
import contextlib
import errno
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any, Protocol, cast

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
from keyforge.keyforged.output_helpers import emit_mouse_move, get_trigger_axis, resolve_output_code
from keyforge.keyforged.recording import RecordingManager
from keyforge.keyforged.superkey_state import SuperkeyConfig, SuperkeyMachine

log = logging.getLogger("keyforged.devices")
ACTIVE_KEY_IDLE_LOG_INTERVAL_S = 1.0
ACTIVE_KEY_IDLE_MAX_WAIT_S = 300.0
COMBO_HELD_REARM_MODIFIERS = frozenset({"shift", "ctrl", "alt", "meta"})

type BroadcastCallback = Callable[[CommandType, dict[str, object]], Awaitable[None]]
type MappingGetter = Callable[[], dict[str, MappingAction]]
type DeviceEventCallback = Callable[..., Awaitable[ComboDecision | bool | None]]
type MacroPlayer = Callable[..., Awaitable[dict[str, object]]]
type RapidfireTaskFactory = Callable[[], asyncio.Task[None]]


class _DeviceInfo(Protocol):
    vendor: int
    product: int


class _ManagedInputDevice(Protocol):
    path: str
    name: str | None
    info: _DeviceInfo

    def grab(self) -> None: ...

    def ungrab(self) -> None: ...

    def capabilities(self) -> dict[int, Sequence[object]]: ...

    def async_read_loop(self) -> AsyncIterator[evdev.InputEvent]: ...

    def fileno(self) -> int: ...

    def read_one(self) -> evdev.InputEvent | None: ...

    def active_keys(self) -> Sequence[int]: ...

    def close(self) -> None: ...


class _WritableUInput(Protocol):
    def write(self, event_type: int, code: int, value: int) -> None: ...

    def syn(self) -> None: ...

    def close(self) -> None: ...


def _device_input(path: str) -> _ManagedInputDevice:
    return cast(_ManagedInputDevice, evdev.InputDevice(path))


def _uinput_writer(device: evdev.UInput | None) -> _WritableUInput | None:
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


async def event_loop(device_runtime: Any, *, asyncio_mod: Any, log: Any) -> None:
    error_backoff = 0.01
    device = device_runtime.device
    if device is None:
        return

    try:
        async for event in device.async_read_loop():
            if not device_runtime._running:
                break
            try:
                await device_runtime._process_event(event)
                error_backoff = 0.01
            except Exception as exc:
                if device_runtime._running:
                    await device_runtime._recover_from_event_processing_error()
                    log.warning(
                        "Event processing error on %s: %s (backoff %.3fs)",
                        device_runtime.path,
                        exc,
                        error_backoff,
                    )
                    await asyncio_mod.sleep(error_backoff)
                    error_backoff = min(0.5, error_backoff * 2)
    except asyncio_mod.CancelledError:
        pass
    except OSError as exc:
        if device_runtime._running:
            await device_runtime._cleanup_runtime_failure()
            log.warning("Device read error on %s: %s", device_runtime.path, exc)


async def cleanup_runtime_failure(device_runtime: Any, *, log: Any) -> None:
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
    device_runtime._release_all_keys()


async def recover_from_event_processing_error(device_runtime: Any) -> None:
    await device_runtime._cleanup_runtime_failure()


def get_event_name(event: Any, *, evdev_mod: Any) -> str:
    try:
        code_name = evdev_mod.ecodes.bytype[event.type].get(event.code, str(event.code))
        if isinstance(code_name, tuple):
            code_name = code_name[0] if code_name else str(event.code)
        return str(code_name).lower()
    except Exception:
        return str(event.code)


def get_key_name(code: int, *, evdev_mod: Any) -> str | None:
    try:
        code_name = evdev_mod.ecodes.bytype[evdev_mod.ecodes.EV_KEY].get(code, str(code))
        if isinstance(code_name, tuple):
            code_name = code_name[0] if code_name else str(code)
        return str(code_name).lower()
    except Exception:
        return None


async def broadcast_grab_status(
    device_runtime: Any,
    state: str,
    active_names: list[str],
    *,
    waited_s: float,
    command_type: Any,
    log: Any,
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
    device_runtime: Any, timeout_s: float, *, asyncio_mod: Any, errno_mod: Any, log: Any
) -> bool:
    if device_runtime.device is None:
        return False

    loop = asyncio_mod.get_running_loop()
    readable = asyncio_mod.Event()
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
    device_runtime: Any,
    *,
    asyncio_mod: Any,
    time_mod: Any,
    log: Any,
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
                await device_runtime._broadcast_grab_status("ready", [], waited_s=now - started_at)
                log.info(
                    "[%s] active keys cleared, proceeding with grab", device_runtime.hardware_id
                )
            return
        active_names = [
            event_name
            for code in active_codes
            if (event_name := device_runtime._get_key_name(int(code))) is not None
        ]
        summary = (
            ", ".join(active_names)
            if active_names
            else ", ".join(str(int(code)) for code in active_codes)
        )
        if now - started_at >= active_key_idle_max_wait_s:
            await device_runtime._broadcast_grab_status(
                "timed_out", active_names, waited_s=now - started_at
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
            await device_runtime._broadcast_grab_status(
                "waiting", active_names, waited_s=now - started_at
            )
            log.warning(
                "[%s] delaying grab until keys are released: %s",
                device_runtime.hardware_id,
                summary,
            )
            warned = True
            last_log_at = now
        elif now - last_log_at >= active_key_idle_log_interval_s:
            await device_runtime._broadcast_grab_status(
                "waiting", active_names, waited_s=now - started_at
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
        await device_runtime._wait_for_active_key_activity(wait_timeout)


def seed_startup_held_actions(device_runtime: Any) -> None:
    if device_runtime.device is None:
        return

    try:
        active_codes = list(device_runtime.device.active_keys() or [])
    except Exception:
        return

    mapping = device_runtime.mapping_getter()
    for code in active_codes:
        event_name = device_runtime._get_key_name(int(code))
        if not event_name or event_name in device_runtime._held_source_actions:
            continue
        action = device_runtime._find_action_for_code(int(code), event_name, mapping)
        device_runtime._held_source_actions[event_name] = action
        device_runtime._reconcile_startup_held_action(action)


def reconcile_startup_held_action(
    device_runtime: Any, action: Any, *, action_type_enum: Any
) -> None:
    if action is None or not action.target:
        return

    if action.action_type == action_type_enum.KEYBOARD:
        code = device_runtime._resolve_code(action.target)
        if code is not None:
            device_runtime._ensure_key_released(code, device_runtime.keyboard_uinput)
        return

    if action.action_type == action_type_enum.MOUSE:
        code = device_runtime._resolve_code(action.target)
        if code is not None:
            device_runtime._ensure_key_released(code, device_runtime.mouse_uinput)
        return

    if action.action_type == action_type_enum.GAMEPAD:
        is_trigger, axis_code = device_runtime._get_trigger_axis(action.target)
        if is_trigger and axis_code is not None:
            device_runtime._ensure_trigger_released(axis_code)
            return
        code = device_runtime._resolve_code(action.target)
        if code is not None:
            device_runtime._ensure_key_released(code, device_runtime.gamepad_uinput)


async def process_event(
    device_runtime: Any,
    event: Any,
    *,
    evdev_mod: Any,
    time_mod: Any,
    log: Any,
    combo_decision_cls: Any,
    classify_event_device_type_fn: Any,
    action_type_enum: Any,
) -> None:
    started_ns = time_mod.perf_counter_ns()
    diag_label = "unknown"
    combo_consumed = False
    combo_passthrough_requested = False

    event_name = device_runtime._get_event_name(event)
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
                    event_name in device_runtime._held_source_actions
                    or event_name in device_runtime._combo_passthrough_held
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
        device_runtime._passthrough(event)
        diag_label = "passthrough_other"
        if device_runtime.diagnostics_recorder:
            device_runtime.diagnostics_recorder(
                diag_label,
                (time_mod.perf_counter_ns() - started_ns) / 1000.0,
            )
        return

    if (
        event.type == evdev_mod.ecodes.EV_KEY
        and event_name in device_runtime._combo_passthrough_held
    ):
        device_runtime._passthrough(event)
        if int(event.value) == 0:
            device_runtime._combo_passthrough_held.discard(event_name)
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
        event.type == evdev_mod.ecodes.EV_KEY and event_name in device_runtime._held_source_actions
    )
    if not mapping and not recording_active and not has_held_source_action:
        if (
            combo_passthrough_requested
            and event.type == evdev_mod.ecodes.EV_KEY
            and int(event.value) == 1
        ):
            device_runtime._combo_passthrough_held.add(event_name)
        device_runtime._passthrough(event)
        diag_label = "combo_passthrough" if combo_passthrough_requested else "passthrough_fast"
        if device_runtime.diagnostics_recorder:
            device_runtime.diagnostics_recorder(
                diag_label,
                (time_mod.perf_counter_ns() - started_ns) / 1000.0,
            )
        return

    action = device_runtime._find_action_for_event(event, mapping)
    if event.type == evdev_mod.ecodes.EV_KEY:
        held_action = device_runtime._held_source_actions.get(event_name)
        if int(event.value) == 1 and event_name not in device_runtime._held_source_actions:
            device_runtime._held_source_actions[event_name] = action
        elif int(event.value) in (0, 2) and event_name in device_runtime._held_source_actions:
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
            recording_manager.record_event(
                classify_event_device_type_fn(event, device_runtime.device_types),
                event,
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
        await device_runtime._execute_action(action, event, event_name)
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
            device_runtime._combo_passthrough_held.add(event_name)
        device_runtime._passthrough(event)
        diag_label = "combo_passthrough" if combo_passthrough_requested else "passthrough_mapped"

    if event.type == evdev_mod.ecodes.EV_KEY and int(event.value) == 0:
        device_runtime._held_source_actions.pop(event_name, None)

    if device_runtime.diagnostics_recorder:
        device_runtime.diagnostics_recorder(
            diag_label, (time_mod.perf_counter_ns() - started_ns) / 1000.0
        )


def find_action_for_event(device_runtime: Any, event: Any, mapping: dict[str, Any]) -> Any:
    event_name = device_runtime._get_event_name(event)
    return device_runtime._find_action_for_code(int(event.code), event_name, mapping)


def find_action_for_code(
    device_runtime: Any, event_code: int, event_name: str, mapping: dict[str, Any]
) -> Any:
    button_id = device_runtime.evdev_code_to_button.get(int(event_code))
    if button_id and button_id in mapping:
        return mapping[button_id]
    return device_runtime._find_action_for_name(event_name, mapping)


def find_action_for_name(
    device_runtime: Any,
    event_name: str,
    mapping: dict[str, Any],
    *,
    canonical_gamepad_button_name_fn: Any,
) -> Any:
    button_id = device_runtime.evdev_to_button.get(event_name.lower())
    if not button_id:
        canonical_name = canonical_gamepad_button_name_fn(event_name)
        if canonical_name != event_name.lower():
            button_id = device_runtime.evdev_to_button.get(canonical_name)

    if button_id and button_id in mapping:
        return mapping[button_id]

    return None


async def execute_action(
    device_runtime: Any,
    action: Any,
    event: Any,
    event_name: str,
    *,
    asyncio_mod: Any,
    command_type: Any,
    fire_and_observe_fn: Any,
    action_type_enum: Any,
    superkey_machine_cls: Any,
    superkey_config_cls: Any,
    evdev_mod: Any,
    uinput_writer: Any,
) -> None:
    if action.action_type == action_type_enum.PASSTHROUGH:
        device_runtime._passthrough(event)

    elif action.action_type == action_type_enum.SUPPRESS:
        pass

    elif action.action_type == action_type_enum.KEYBOARD:
        await _execute_key_action(
            device_runtime,
            action,
            event,
            event_name,
            asyncio_mod=asyncio_mod,
            fire_and_observe_fn=fire_and_observe_fn,
            uinput_dev=device_runtime.keyboard_uinput,
            target_kind="key",
            trigger_kind="key",
        )

    elif action.action_type == action_type_enum.MOUSE:
        await _execute_key_action(
            device_runtime,
            action,
            event,
            event_name,
            asyncio_mod=asyncio_mod,
            fire_and_observe_fn=fire_and_observe_fn,
            uinput_dev=device_runtime.mouse_uinput,
            target_kind="key",
            trigger_kind="key",
        )

    elif action.action_type == action_type_enum.GAMEPAD:
        if action.target:
            is_trigger, axis_code = device_runtime._get_trigger_axis(action.target)
            if is_trigger:
                if axis_code is None:
                    return
                if action.rapidfire_enabled:
                    if event.value == 1:
                        device_runtime._start_rapidfire_task(
                            event_name,
                            "trigger",
                            lambda: asyncio_mod.create_task(
                                device_runtime._rapidfire_trigger(
                                    axis_code,
                                    action.rapidfire_hold_ms,
                                    action.rapidfire_wait_ms,
                                    event_name,
                                )
                            ),
                            axis_code=axis_code,
                        )
                    elif event.value == 0:
                        await device_runtime._stop_rapidfire_async(event_name)
                elif action.tap_enabled:
                    if event.value == 1 and not device_runtime._tap_active.get(event_name, False):
                        device_runtime._tap_active[event_name] = True
                        fire_and_observe_fn(
                            device_runtime._tap_trigger(axis_code, action.tap_hold_ms, event_name),
                            f"tap action {event_name}",
                        )
                else:
                    gamepad_uinput = uinput_writer(device_runtime.gamepad_uinput)
                    if gamepad_uinput is None:
                        return
                    gamepad_uinput.write(
                        evdev_mod.ecodes.EV_ABS,
                        axis_code,
                        255 if event.value else 0,
                    )
                    gamepad_uinput.syn()
            else:
                await _execute_key_action(
                    device_runtime,
                    action,
                    event,
                    event_name,
                    asyncio_mod=asyncio_mod,
                    fire_and_observe_fn=fire_and_observe_fn,
                    uinput_dev=device_runtime.gamepad_uinput,
                    target_kind="key",
                    trigger_kind="key",
                )

    elif action.action_type == action_type_enum.EXEC:
        if event.value == 1 and action.exec_ref is not None and device_runtime.broadcast_callback:
            fire_and_observe_fn(
                device_runtime.broadcast_callback(
                    command_type.ACTION_TRIGGER,
                    {
                        "action_type": "exec",
                        "exec_ref": action.exec_ref,
                        "source_device": device_runtime.hardware_id,
                        "source_button": event_name,
                    },
                ),
                f"exec action {event_name}",
            )

    elif action.action_type == action_type_enum.COMPOSITOR_DISPATCH:
        if event.value == 1 and device_runtime.broadcast_callback:
            fire_and_observe_fn(
                device_runtime.broadcast_callback(
                    command_type.ACTION_TRIGGER,
                    {
                        "action_type": "compositor_dispatch",
                        "compositor": action.compositor_id or "",
                        "dispatcher": action.compositor_dispatcher or "",
                        "args": action.compositor_args or "",
                        "source_device": device_runtime.hardware_id,
                        "source_button": event_name,
                    },
                ),
                f"compositor action {event_name}",
            )

    elif action.action_type == action_type_enum.START_MACRO_RECORDING:
        if event.value == 1 and device_runtime.broadcast_callback:
            fire_and_observe_fn(
                device_runtime.broadcast_callback(
                    command_type.ACTION_TRIGGER,
                    {
                        "action_type": "start_macro_recording",
                        "source_device": device_runtime.hardware_id,
                        "source_button": event_name,
                    },
                ),
                f"start recording action {event_name}",
            )

    elif action.action_type == action_type_enum.STOP_MACRO_RECORDING:
        if event.value == 1 and device_runtime.broadcast_callback:
            fire_and_observe_fn(
                device_runtime.broadcast_callback(
                    command_type.ACTION_TRIGGER,
                    {
                        "action_type": "stop_macro_recording",
                        "source_device": device_runtime.hardware_id,
                        "source_button": event_name,
                    },
                ),
                f"stop recording action {event_name}",
            )

    elif action.action_type == action_type_enum.CANCEL_MACRO_PLAYBACK:
        if event.value == 1 and device_runtime.broadcast_callback:
            fire_and_observe_fn(
                device_runtime.broadcast_callback(
                    command_type.ACTION_TRIGGER,
                    {
                        "action_type": "cancel_macro_playback",
                        "source_device": device_runtime.hardware_id,
                        "source_button": event_name,
                    },
                ),
                f"cancel macro action {event_name}",
            )

    elif action.action_type in (
        action_type_enum.PROFILE_ENABLE,
        action_type_enum.PROFILE_DISABLE,
        action_type_enum.PROFILE_TOGGLE,
    ):
        if event.value == 1 and device_runtime.broadcast_callback:
            fire_and_observe_fn(
                device_runtime.broadcast_callback(
                    command_type.ACTION_TRIGGER,
                    {
                        "action_type": action.action_type.value,
                        "profile_name": action.profile_name or action.target or "",
                        "source_device": device_runtime.hardware_id,
                        "source_button": event_name,
                    },
                ),
                f"profile action {event_name}",
            )

    elif action.action_type == action_type_enum.MACRO:
        if (
            event.value in (0, 1)
            and (action.macro_events or action.macro_name)
            and device_runtime.macro_player
        ):
            fire_and_observe_fn(
                device_runtime.macro_player(
                    macro_events=action.macro_events or [],
                    macro_name=action.macro_name or "",
                    replay_mouse_movement=action.macro_replay_mouse_movement,
                    replay_mouse_clicks=action.macro_replay_mouse_clicks,
                    speed=action.macro_speed,
                    loop_mode=action.macro_loop_mode,
                    loop_count=action.macro_loop_count,
                    move_to_start=action.macro_move_to_start,
                    start_x=action.macro_start_x,
                    start_y=action.macro_start_y,
                    block_mouse_movement=action.macro_block_mouse_movement,
                    source_device=device_runtime.hardware_id,
                    source_button=event_name,
                    trigger_value=int(event.value),
                ),
                f"macro action {event_name}",
            )

    elif action.action_type in (action_type_enum.MOUSE_MOVE_REL, action_type_enum.MOUSE_MOVE_ABS):
        await _execute_move_action(
            device_runtime,
            action,
            event,
            event_name,
            asyncio_mod=asyncio_mod,
            fire_and_observe_fn=fire_and_observe_fn,
        )

    elif action.action_type == action_type_enum.SUPERKEY:
        if action.superkey_config:
            machine = device_runtime._superkey_machines.get(event_name)
            if event.value == 1 and not machine:

                async def superkey_broadcast(data: dict[str, object]) -> None:
                    if device_runtime.broadcast_callback:
                        fire_and_observe_fn(
                            device_runtime.broadcast_callback(command_type.ACTION_TRIGGER, data),
                            f"superkey action {event_name}",
                        )

                machine = superkey_machine_cls(
                    config=action.superkey_config,
                    event_name=event_name,
                    keyboard_uinput=device_runtime.keyboard_uinput,
                    mouse_uinput=device_runtime.mouse_uinput,
                    gamepad_uinput=device_runtime.gamepad_uinput,
                    broadcast_callback=superkey_broadcast,
                    key_event_tracker=device_runtime._track_superkey_output,
                )
                device_runtime._superkey_machines[event_name] = machine

            if event.value == 1 and machine is not None:
                await machine.on_down()
            elif event.value == 0 and machine is not None:
                await machine.on_up()


async def _execute_key_action(
    device_runtime: Any,
    action: Any,
    event: Any,
    event_name: str,
    *,
    asyncio_mod: Any,
    fire_and_observe_fn: Any,
    uinput_dev: Any,
    target_kind: str,
    trigger_kind: str,
) -> None:
    if not action.target:
        return
    code = device_runtime._resolve_code(action.target)
    if not code:
        return
    if action.rapidfire_enabled:
        if event.value == 1:
            device_runtime._start_rapidfire_task(
                event_name,
                trigger_kind,
                lambda: asyncio_mod.create_task(
                    device_runtime._rapidfire_key(
                        code,
                        action.rapidfire_hold_ms,
                        action.rapidfire_wait_ms,
                        event_name,
                        uinput_dev,
                    )
                ),
                code=code,
                uinput=uinput_dev,
            )
        elif event.value == 0:
            await device_runtime._stop_rapidfire_async(event_name)
    elif action.tap_enabled:
        if event.value == 1 and not device_runtime._tap_active.get(event_name, False):
            device_runtime._tap_active[event_name] = True
            fire_and_observe_fn(
                device_runtime._tap_key(code, action.tap_hold_ms, event_name, uinput_dev),
                f"tap action {event_name}",
            )
    else:
        device_runtime._write_key(uinput_dev, code, int(event.value))


async def _execute_move_action(
    device_runtime: Any,
    action: Any,
    event: Any,
    event_name: str,
    *,
    asyncio_mod: Any,
    fire_and_observe_fn: Any,
) -> None:
    if action.rapidfire_enabled:
        if event.value == 1:
            device_runtime._start_rapidfire_task(
                event_name,
                "move",
                lambda: asyncio_mod.create_task(
                    device_runtime._rapidfire_move(
                        action,
                        event_name,
                        action.rapidfire_hold_ms,
                        action.rapidfire_wait_ms,
                    )
                ),
            )
        elif event.value == 0:
            await device_runtime._stop_rapidfire_async(event_name)
    elif action.tap_enabled:
        if event.value == 1 and not device_runtime._tap_active.get(event_name, False):
            device_runtime._tap_active[event_name] = True
            fire_and_observe_fn(
                device_runtime._tap_move(action, event_name, action.tap_hold_ms),
                f"tap move {event_name}",
            )
    elif event.value == 1:
        device_runtime._emit_mouse_move(action)


def start_rapidfire_task(
    device_runtime: Any,
    event_name: str,
    kind: str,
    task_factory: Any,
    *,
    code: int | None,
    uinput: Any,
    axis_code: int | None,
) -> None:
    device_runtime._stop_rapidfire(event_name)
    task = task_factory()
    device_runtime._rapidfire_active[event_name] = True
    device_runtime._rapidfire_tasks[event_name] = task
    state: dict[str, object] = {"kind": kind}
    if code is not None:
        state["code"] = int(code)
    if uinput is not None:
        state["uinput"] = uinput
    if axis_code is not None:
        state["axis_code"] = int(axis_code)
    device_runtime._rapidfire_outputs[event_name] = state


def stop_rapidfire(device_runtime: Any, event_name: str) -> None:
    device_runtime._rapidfire_active[event_name] = False
    task = device_runtime._rapidfire_tasks.pop(event_name, None)
    if task is not None and not task.done():
        task.cancel()
    state = device_runtime._rapidfire_outputs.pop(event_name, None)
    if not state:
        return
    kind = str(state.get("kind", "") or "")
    if kind == "trigger":
        axis_code = state.get("axis_code")
        if axis_code is not None:
            device_runtime._ensure_trigger_released(cast(int, axis_code))
        return
    if kind == "key":
        code = state.get("code")
        uinput = state.get("uinput")
        if code is not None:
            device_runtime._ensure_key_released(cast(int, code), uinput)


async def stop_rapidfire_async(
    device_runtime: Any, event_name: str, *, asyncio_mod: Any, contextlib_mod: Any
) -> None:
    task = device_runtime._rapidfire_tasks.get(event_name)
    device_runtime._stop_rapidfire(event_name)
    if task is not None and not task.done():
        with contextlib_mod.suppress(asyncio_mod.CancelledError):
            await task


def finish_rapidfire_task(device_runtime: Any, event_name: str, task: Any) -> None:
    if device_runtime._rapidfire_tasks.get(event_name) is not task:
        return
    device_runtime._rapidfire_tasks.pop(event_name, None)
    device_runtime._rapidfire_active.pop(event_name, None)
    state = device_runtime._rapidfire_outputs.pop(event_name, None)
    if not state:
        return
    kind = str(state.get("kind", "") or "")
    if kind == "trigger":
        axis_code = state.get("axis_code")
        if axis_code is not None:
            device_runtime._ensure_trigger_released(cast(int, axis_code))
        return
    if kind == "key":
        code = state.get("code")
        uinput = state.get("uinput")
        if code is not None:
            device_runtime._ensure_key_released(cast(int, code), uinput)


def bucket_for_uinput(device_runtime: Any, uinput_dev: Any) -> str | None:
    if uinput_dev is None:
        return None
    if device_runtime.uinput is not None and uinput_dev is device_runtime.uinput:
        return "passthrough"
    if device_runtime.keyboard_uinput is not None and uinput_dev is device_runtime.keyboard_uinput:
        return "keyboard"
    if device_runtime.mouse_uinput is not None and uinput_dev is device_runtime.mouse_uinput:
        return "mouse"
    if device_runtime.gamepad_uinput is not None and uinput_dev is device_runtime.gamepad_uinput:
        return "gamepad"
    return None


def track_key_state(device_runtime: Any, uinput_dev: Any, code: int, value: int) -> None:
    bucket = device_runtime._bucket_for_uinput(uinput_dev)
    if not bucket:
        return
    held = device_runtime._held_output_keys[bucket]
    if int(value) == 1:
        held.add(int(code))
    elif int(value) == 0:
        held.discard(int(code))


def write_key(
    device_runtime: Any,
    uinput_dev: Any,
    code: int,
    value: int,
    *,
    evdev_mod: Any,
    uinput_writer: Any,
) -> None:
    writer = uinput_writer(uinput_dev)
    if writer is None:
        return
    writer.write(evdev_mod.ecodes.EV_KEY, int(code), int(value))
    writer.syn()
    device_runtime._track_key_state(uinput_dev, int(code), int(value))


def track_superkey_output(device_runtime: Any, action_type: str, code: int, value: int) -> bool:
    bucket = action_type if action_type in device_runtime._superkey_output_refcounts else None
    if bucket is None:
        return True

    refcounts = device_runtime._superkey_output_refcounts[bucket]
    current = refcounts.get(int(code), 0)

    if int(value) == 1:
        refcounts[int(code)] = current + 1
        device_runtime._held_output_keys[bucket].add(int(code))
        return current == 0

    if int(value) == 0:
        if current <= 1:
            refcounts.pop(int(code), None)
            device_runtime._held_output_keys[bucket].discard(int(code))
            return current == 1

        refcounts[int(code)] = current - 1
        return False

    return True


def passthrough(device_runtime: Any, event: Any, *, evdev_mod: Any, uinput_writer: Any) -> None:
    if (
        device_runtime.suppress_rel_getter
        and event.type == evdev_mod.ecodes.EV_REL
        and event.code in (evdev_mod.ecodes.REL_X, evdev_mod.ecodes.REL_Y)
        and device_runtime.suppress_rel_getter()
    ):
        return
    if event.type == evdev_mod.ecodes.EV_KEY:
        device_runtime._write_key(device_runtime.uinput, int(event.code), int(event.value))
        return
    uinput = device_runtime.uinput
    writer = uinput_writer(uinput)
    if writer is None:
        return
    writer.write(event.type, event.code, event.value)
    writer.syn()


async def rapidfire_trigger(
    device_runtime: Any,
    axis_code: int,
    hold_ms: int,
    wait_ms: int,
    event_name: str,
    *,
    asyncio_mod: Any,
    evdev_mod: Any,
    uinput_writer: Any,
) -> None:
    hold = hold_ms / 1000.0
    wait = wait_ms / 1000.0
    task = asyncio_mod.current_task()
    pressed = False

    try:
        while device_runtime._rapidfire_active.get(event_name, False) and device_runtime._running:
            gamepad_uinput = uinput_writer(device_runtime.gamepad_uinput)
            if gamepad_uinput is None:
                return
            gamepad_uinput.write(evdev_mod.ecodes.EV_ABS, axis_code, 255)
            gamepad_uinput.syn()
            pressed = True
            await asyncio_mod.sleep(hold)

            if pressed:
                gamepad_uinput.write(evdev_mod.ecodes.EV_ABS, axis_code, 0)
                gamepad_uinput.syn()
                pressed = False
            if not device_runtime._rapidfire_active.get(event_name, False):
                break

            await asyncio_mod.sleep(wait)
    except Exception:
        pass
    finally:
        if pressed:
            device_runtime._ensure_trigger_released(axis_code)
        if task is not None:
            device_runtime._finish_rapidfire_task(event_name, task)


def ensure_trigger_released(
    device_runtime: Any, axis_code: int, *, evdev_mod: Any, uinput_writer: Any
) -> None:
    try:
        gamepad_uinput = uinput_writer(device_runtime.gamepad_uinput)
        if gamepad_uinput is not None:
            gamepad_uinput.write(evdev_mod.ecodes.EV_ABS, axis_code, 0)
            gamepad_uinput.syn()
    except Exception:
        pass


async def tap_trigger(
    device_runtime: Any,
    axis_code: int,
    hold_ms: int,
    event_name: str,
    *,
    asyncio_mod: Any,
    evdev_mod: Any,
    uinput_writer: Any,
) -> None:
    hold = hold_ms / 1000.0

    try:
        gamepad_uinput = uinput_writer(device_runtime.gamepad_uinput)
        if gamepad_uinput is None:
            return
        gamepad_uinput.write(evdev_mod.ecodes.EV_ABS, axis_code, 255)
        gamepad_uinput.syn()
        await asyncio_mod.sleep(hold)
        gamepad_uinput.write(evdev_mod.ecodes.EV_ABS, axis_code, 0)
        gamepad_uinput.syn()
    except Exception:
        pass
    finally:
        device_runtime._tap_active.pop(event_name, None)


async def rapidfire_key(
    device_runtime: Any,
    code: int,
    hold_ms: int,
    wait_ms: int,
    event_name: str,
    uinput_dev: Any,
    *,
    asyncio_mod: Any,
) -> None:
    hold = hold_ms / 1000.0
    wait = wait_ms / 1000.0
    task = asyncio_mod.current_task()
    pressed = False

    try:
        while device_runtime._rapidfire_active.get(event_name, False) and device_runtime._running:
            device_runtime._write_key(uinput_dev, code, 1)
            pressed = True
            await asyncio_mod.sleep(hold)

            if pressed:
                device_runtime._write_key(uinput_dev, code, 0)
                pressed = False
            if not device_runtime._rapidfire_active.get(event_name, False):
                break

            await asyncio_mod.sleep(wait)
    except Exception:
        pass
    finally:
        if pressed:
            device_runtime._ensure_key_released(code, uinput_dev)
        if task is not None:
            device_runtime._finish_rapidfire_task(event_name, task)


def ensure_key_released(device_runtime: Any, code: int, uinput_dev: Any) -> None:
    try:
        if uinput_dev:
            device_runtime._write_key(uinput_dev, code, 0)
    except Exception:
        pass


def release_all_keys(device_runtime: Any, *, evdev_mod: Any, uinput_writer: Any) -> None:
    devices = {
        "passthrough": device_runtime.uinput,
        "keyboard": device_runtime.keyboard_uinput,
        "mouse": device_runtime.mouse_uinput,
        "gamepad": device_runtime.gamepad_uinput,
    }
    for bucket, uinput_dev in devices.items():
        writer = uinput_writer(uinput_dev)
        if writer is None:
            continue
        held = sorted(device_runtime._held_output_keys.get(bucket, set()))
        if not held:
            continue
        try:
            for code in held:
                writer.write(evdev_mod.ecodes.EV_KEY, int(code), 0)
            writer.syn()
        except Exception:
            pass
        finally:
            device_runtime._held_output_keys[bucket].clear()
            if bucket in device_runtime._superkey_output_refcounts:
                device_runtime._superkey_output_refcounts[bucket].clear()

    gamepad_uinput = uinput_writer(device_runtime.gamepad_uinput)
    if gamepad_uinput is not None:
        try:
            gamepad_uinput.write(evdev_mod.ecodes.EV_ABS, evdev_mod.ecodes.ABS_Z, 0)
            gamepad_uinput.write(evdev_mod.ecodes.EV_ABS, evdev_mod.ecodes.ABS_RZ, 0)
            gamepad_uinput.syn()
        except Exception:
            pass

    for task in list(device_runtime._rapidfire_tasks.values()):
        if not task.done():
            task.cancel()
    device_runtime._rapidfire_tasks.clear()
    device_runtime._rapidfire_outputs.clear()
    device_runtime._rapidfire_active.clear()
    device_runtime._tap_active.clear()
    device_runtime._combo_passthrough_held.clear()
    device_runtime._held_source_actions.clear()


async def tap_key(
    device_runtime: Any,
    code: int,
    hold_ms: int,
    event_name: str,
    uinput_dev: Any,
    *,
    asyncio_mod: Any,
) -> None:
    hold = hold_ms / 1000.0

    try:
        device_runtime._write_key(uinput_dev, code, 1)
        await asyncio_mod.sleep(hold)
        device_runtime._write_key(uinput_dev, code, 0)
    except Exception:
        pass
    finally:
        device_runtime._tap_active.pop(event_name, None)


async def rapidfire_move(
    device_runtime: Any,
    action: Any,
    event_name: str,
    hold_ms: int,
    wait_ms: int,
    *,
    asyncio_mod: Any,
) -> None:
    hold = hold_ms / 1000.0
    wait = wait_ms / 1000.0
    task = asyncio_mod.current_task()

    try:
        while device_runtime._rapidfire_active.get(event_name, False) and device_runtime._running:
            device_runtime._emit_mouse_move(action)
            await asyncio_mod.sleep(hold)

            if not device_runtime._rapidfire_active.get(event_name, False):
                break

            await asyncio_mod.sleep(wait)
    except Exception:
        pass
    finally:
        if task is not None:
            device_runtime._finish_rapidfire_task(event_name, task)


async def tap_move(
    device_runtime: Any, action: Any, event_name: str, hold_ms: int, *, asyncio_mod: Any
) -> None:
    hold = hold_ms / 1000.0

    try:
        device_runtime._emit_mouse_move(action)
        await asyncio_mod.sleep(hold)
    except Exception:
        pass
    finally:
        device_runtime._tap_active.pop(event_name, None)


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
        self._rapidfire_active: dict[str, bool] = {}
        self._rapidfire_tasks: dict[str, asyncio.Task[None]] = {}
        self._rapidfire_outputs: dict[str, dict[str, object]] = {}
        self._tap_active: dict[str, bool] = {}
        self._superkey_machines: dict[str, SuperkeyMachine] = {}
        self._combo_passthrough_held: set[str] = set()
        self._held_output_keys: dict[str, set[int]] = {
            "passthrough": set(),
            "keyboard": set(),
            "mouse": set(),
            "gamepad": set(),
        }
        self._superkey_output_refcounts: dict[str, dict[int, int]] = {
            "keyboard": {},
            "mouse": {},
            "gamepad": {},
        }
        self._held_source_actions: dict[str, MappingAction | None] = {}

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
        for event_name in self._combo_passthrough_held:
            self._held_source_actions.setdefault(event_name, None)
        self._combo_passthrough_held.clear()
        await self.reset_superkeys()
        self._seed_startup_held_actions()

    async def reset_superkeys(self) -> None:
        for machine in self._superkey_machines.values():
            await machine.stop()
        self._superkey_machines.clear()

    async def grab(self) -> None:
        self.device = _device_input(self.path)
        caps = self.device.capabilities()
        caps.pop(evdev.ecodes.EV_SYN, None)

        self.uinput = evdev.UInput(
            events=cast(dict[int, Sequence[int]], caps),
            name=f"keyforge-{self.hardware_id}",
        )

        try:
            await self._wait_for_active_keys_to_clear()
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
        self.task = asyncio.create_task(self._event_loop())

        log.info("Grabbed %s for %s", self.path, self.hardware_id)

    async def release(self) -> None:
        self._running = False
        self._release_all_keys()
        self._held_source_actions.clear()
        self._combo_passthrough_held.clear()

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
        self._release_all_keys()

    def emit_combo_release(self, evdev_name: str) -> None:
        if not self.uinput:
            return
        code = self._resolve_code(evdev_name)
        if code is None:
            return
        self._write_key(self.uinput, code, 0)

    def has_held_source_inputs(self) -> bool:
        return bool(self._held_source_actions)

    def combo_passthrough_held_modifiers(self) -> set[str]:
        return {
            event_name
            for event_name in self._combo_passthrough_held
            if normalize_combo_evdev(event_name) in COMBO_HELD_REARM_MODIFIERS
        }

    async def _event_loop(self) -> None:
        await event_loop(self, asyncio_mod=asyncio, log=log)

    async def _cleanup_runtime_failure(self) -> None:
        await cleanup_runtime_failure(self, log=log)

    async def _recover_from_event_processing_error(self) -> None:
        await recover_from_event_processing_error(self)

    def _get_event_name(self, event: evdev.InputEvent) -> str:
        return get_event_name(event, evdev_mod=evdev)

    def _get_key_name(self, code: int) -> str | None:
        return get_key_name(code, evdev_mod=evdev)

    async def _broadcast_grab_status(
        self,
        state: str,
        active_names: list[str],
        *,
        waited_s: float,
    ) -> None:
        await broadcast_grab_status(
            self,
            state,
            active_names,
            waited_s=waited_s,
            command_type=CommandType,
            log=log,
        )

    async def _wait_for_active_key_activity(self, timeout_s: float) -> bool:
        return await wait_for_active_key_activity(
            self,
            timeout_s,
            asyncio_mod=asyncio,
            errno_mod=errno,
            log=log,
        )

    async def _wait_for_active_keys_to_clear(self) -> None:
        await wait_for_active_keys_to_clear(
            self,
            asyncio_mod=asyncio,
            time_mod=time,
            log=log,
            active_key_idle_max_wait_s=ACTIVE_KEY_IDLE_MAX_WAIT_S,
            active_key_idle_log_interval_s=ACTIVE_KEY_IDLE_LOG_INTERVAL_S,
        )

    def _seed_startup_held_actions(self) -> None:
        seed_startup_held_actions(self)

    def _reconcile_startup_held_action(self, action: MappingAction | None) -> None:
        reconcile_startup_held_action(self, action, action_type_enum=ActionType)

    async def _process_event(self, event: evdev.InputEvent) -> None:
        await process_event(
            self,
            event,
            evdev_mod=evdev,
            time_mod=time,
            log=log,
            combo_decision_cls=ComboDecision,
            classify_event_device_type_fn=classify_event_device_type,
            action_type_enum=ActionType,
        )

    def _find_action_for_event(
        self,
        event: evdev.InputEvent,
        mapping: dict[str, MappingAction],
    ) -> MappingAction | None:
        return cast(MappingAction | None, find_action_for_event(self, event, mapping))

    def _find_action_for_code(
        self,
        event_code: int,
        event_name: str,
        mapping: dict[str, MappingAction],
    ) -> MappingAction | None:
        return cast(
            MappingAction | None,
            find_action_for_code(self, event_code, event_name, mapping),
        )

    def _find_action_for_name(
        self,
        event_name: str,
        mapping: dict[str, MappingAction],
    ) -> MappingAction | None:
        return cast(
            MappingAction | None,
            find_action_for_name(
                self,
                event_name,
                mapping,
                canonical_gamepad_button_name_fn=canonical_gamepad_button_name,
            ),
        )

    async def _execute_action(
        self,
        action: MappingAction,
        event: evdev.InputEvent,
        event_name: str,
    ) -> None:
        await execute_action(
            self,
            action,
            event,
            event_name,
            asyncio_mod=asyncio,
            command_type=CommandType,
            fire_and_observe_fn=_fire_and_observe,
            action_type_enum=ActionType,
            superkey_machine_cls=SuperkeyMachine,
            superkey_config_cls=SuperkeyConfig,
            evdev_mod=evdev,
            uinput_writer=_uinput_writer,
        )

    def _start_rapidfire_task(
        self,
        event_name: str,
        kind: str,
        task_factory: RapidfireTaskFactory,
        *,
        code: int | None = None,
        uinput: evdev.UInput | None = None,
        axis_code: int | None = None,
    ) -> None:
        start_rapidfire_task(
            self,
            event_name,
            kind,
            task_factory,
            code=code,
            uinput=uinput,
            axis_code=axis_code,
        )

    def _stop_rapidfire(self, event_name: str) -> None:
        stop_rapidfire(self, event_name)

    async def _stop_rapidfire_async(self, event_name: str) -> None:
        await stop_rapidfire_async(
            self,
            event_name,
            asyncio_mod=asyncio,
            contextlib_mod=contextlib,
        )

    def _finish_rapidfire_task(self, event_name: str, task: asyncio.Task[None]) -> None:
        finish_rapidfire_task(self, event_name, task)

    def _bucket_for_uinput(self, uinput_dev: evdev.UInput | None) -> str | None:
        return bucket_for_uinput(self, uinput_dev)

    def _track_key_state(self, uinput_dev: evdev.UInput | None, code: int, value: int) -> None:
        track_key_state(self, uinput_dev, code, value)

    def _write_key(self, uinput_dev: evdev.UInput | None, code: int, value: int) -> None:
        write_key(
            self,
            uinput_dev,
            code,
            value,
            evdev_mod=evdev,
            uinput_writer=_uinput_writer,
        )

    def _track_superkey_output(self, action_type: str, code: int, value: int) -> bool:
        return track_superkey_output(self, action_type, code, value)

    def _passthrough(self, event: evdev.InputEvent) -> None:
        passthrough(self, event, evdev_mod=evdev, uinput_writer=_uinput_writer)

    def _resolve_code(self, key_name: str) -> int | None:
        return resolve_output_code(key_name)

    def _get_trigger_axis(self, target: str) -> tuple[bool, int | None]:
        return get_trigger_axis(target)

    async def _rapidfire_trigger(
        self, axis_code: int, hold_ms: int, wait_ms: int, event_name: str
    ) -> None:
        await rapidfire_trigger(
            self,
            axis_code,
            hold_ms,
            wait_ms,
            event_name,
            asyncio_mod=asyncio,
            evdev_mod=evdev,
            uinput_writer=_uinput_writer,
        )

    def _ensure_trigger_released(self, axis_code: int) -> None:
        ensure_trigger_released(
            self,
            axis_code,
            evdev_mod=evdev,
            uinput_writer=_uinput_writer,
        )

    async def _tap_trigger(self, axis_code: int, hold_ms: int, event_name: str) -> None:
        await tap_trigger(
            self,
            axis_code,
            hold_ms,
            event_name,
            asyncio_mod=asyncio,
            evdev_mod=evdev,
            uinput_writer=_uinput_writer,
        )

    async def _rapidfire_key(
        self, code: int, hold_ms: int, wait_ms: int, event_name: str, uinput_dev: evdev.UInput
    ) -> None:
        await rapidfire_key(
            self,
            code,
            hold_ms,
            wait_ms,
            event_name,
            uinput_dev,
            asyncio_mod=asyncio,
        )

    def _ensure_key_released(self, code: int, uinput_dev: evdev.UInput | None) -> None:
        ensure_key_released(self, code, uinput_dev)

    def _release_all_keys(self) -> None:
        release_all_keys(self, evdev_mod=evdev, uinput_writer=_uinput_writer)

    async def _tap_key(
        self, code: int, hold_ms: int, event_name: str, uinput_dev: evdev.UInput
    ) -> None:
        await tap_key(self, code, hold_ms, event_name, uinput_dev, asyncio_mod=asyncio)

    def _emit_mouse_move(self, action: MappingAction) -> None:
        emit_mouse_move(
            self.mouse_uinput,
            int(action.move_x),
            int(action.move_y),
            absolute=action.action_type == ActionType.MOUSE_MOVE_ABS,
        )

    async def _rapidfire_move(
        self,
        action: MappingAction,
        event_name: str,
        hold_ms: int,
        wait_ms: int,
    ) -> None:
        await rapidfire_move(
            self,
            action,
            event_name,
            hold_ms,
            wait_ms,
            asyncio_mod=asyncio,
        )

    async def _tap_move(self, action: MappingAction, event_name: str, hold_ms: int) -> None:
        await tap_move(self, action, event_name, hold_ms, asyncio_mod=asyncio)
