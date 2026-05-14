import errno
import logging
from typing import cast

import evdev

from keymasq.common.ipc import CommandType
from keymasq.common.models import ActionType, MappingAction
from keymasq.keymasqd.output_helpers import (
    resolve_gamepad_axis_code,
    resolve_output_code,
)
from keymasq.keymasqd.runtime import grabbed_device_events as runtime_events
from keymasq.keymasqd.runtime import grabbed_device_outputs as runtime_outputs
from keymasq.keymasqd.runtime.grabbed_device_types import (
    AsyncioModule,
    ErrnoModule,
    GrabbedDeviceRuntime,
    TimeModule,
    WritableUInput,
)


def _uinput_writer(device: object | None) -> WritableUInput | None:
    return cast(WritableUInput | None, device)


async def broadcast_grab_status(
    device_runtime: GrabbedDeviceRuntime,
    state: str,
    active_names: list[str],
    *,
    waited_s: float,
    log: logging.Logger,
) -> None:
    if device_runtime.broadcast_callback is None:
        return
    try:
        await device_runtime.broadcast_callback(
            CommandType.DEVICE_GRAB_STATUS,
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
    device_runtime: GrabbedDeviceRuntime,
    timeout_s: float,
    *,
    asyncio_mod: AsyncioModule,
    errno_mod: ErrnoModule,
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
    device_runtime: GrabbedDeviceRuntime,
    *,
    asyncio_mod: AsyncioModule,
    time_mod: TimeModule,
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
                    log=log,
                )
                log.info(
                    "[%s] active keys cleared, proceeding with grab", device_runtime.hardware_id
                )
            return

        active_names = [
            event_name
            for code in active_codes
            if (event_name := runtime_events.get_key_name(int(code), evdev_mod=evdev)) is not None
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


def seed_startup_held_actions(device_runtime: GrabbedDeviceRuntime) -> None:
    if device_runtime.device is None:
        return

    try:
        active_codes = list(device_runtime.device.active_keys() or [])
    except Exception:
        return

    mapping = device_runtime.mapping_getter()
    for code in active_codes:
        event_name = runtime_events.get_key_name(int(code), evdev_mod=evdev)
        if not event_name or event_name in device_runtime.state.held_source_actions:
            continue
        device_runtime.state.held_source_keys.add(event_name)
        action = runtime_events.find_action_for_code(
            device_runtime,
            evdev.ecodes.EV_KEY,
            int(code),
            1,
            event_name,
            mapping,
        )
        device_runtime.state.held_source_actions[event_name] = action
        reconcile_startup_held_action(device_runtime, action)


def reconcile_startup_held_action(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction | None,
) -> None:
    if action is None or not action.target:
        return

    if action.action_type == ActionType.KEYBOARD:
        code = resolve_output_code(action.target)
        if code is not None:
            runtime_outputs.ensure_key_released(
                device_runtime,
                code,
                device_runtime.keyboard_uinput,
            )
        return

    if action.action_type == ActionType.MOUSE:
        code = resolve_output_code(action.target)
        if code is not None:
            runtime_outputs.ensure_key_released(
                device_runtime,
                code,
                device_runtime.mouse_uinput,
            )
        return

    if action.action_type == ActionType.GAMEPAD:
        code = resolve_output_code(action.target)
        if code is not None:
            runtime_outputs.ensure_key_released(
                device_runtime,
                code,
                device_runtime.gamepad_uinput,
            )
        return

    if action.action_type == ActionType.GAMEPAD_AXIS:
        axis_code = resolve_gamepad_axis_code(action.target)
        if axis_code is not None:
            runtime_outputs.ensure_abs_axis_released(
                device_runtime,
                axis_code,
                evdev_mod=evdev,
                uinput_writer=_uinput_writer,
            )
