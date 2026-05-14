import asyncio
import contextlib
from typing import cast

import evdev

from keymasq.common.models import (
    ActionType,
    MappingAction,
    clamp_rapidfire_hold_ms,
    clamp_rapidfire_wait_ms,
)
from keymasq.keymasqd.runtime.grabbed_device_outputs import (
    emit_configured_mouse_move,
    ensure_abs_axis_released,
    ensure_key_released,
    write_abs_axis,
    write_key,
)
from keymasq.keymasqd.runtime.grabbed_device_types import (
    AsyncioModule,
    EvdevModule,
    GrabbedDeviceRuntime,
    TaskFactory,
    UInputWriter,
    WritableUInput,
    runtime_is_running,
)
from keymasq.keymasqd.runtime.mouse_actions import (
    rapidfire_relative_pulses,
    tap_relative_pulse,
    write_relative_pulse,
)


async def emit_move_action(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction,
) -> None:
    if action.action_type == ActionType.MOUSE_MOVE_ABS:
        cursor_position_setter = device_runtime.cursor_position_setter
        if cursor_position_setter is not None:
            await cursor_position_setter(int(action.move_x), int(action.move_y))
            return
    emit_configured_mouse_move(device_runtime, action)


def start_rapidfire_task(
    device_runtime: GrabbedDeviceRuntime,
    event_name: str,
    kind: str,
    task_factory: TaskFactory,
    *,
    code: int | None,
    uinput: object | None,
    axis_code: int | None,
    bucket: str | None = None,
    axis_release_value: int = 0,
) -> None:
    stop_rapidfire(device_runtime, event_name)
    task = task_factory()
    device_runtime.state.rapidfire_active[event_name] = True
    device_runtime.state.rapidfire_tasks[event_name] = task
    from keymasq.keymasqd.runtime.grabbed_device_types import RapidfireOutputState

    state = RapidfireOutputState(kind=kind)
    if code is not None:
        state.code = int(code)
    if uinput is not None:
        state.uinput = uinput
    if axis_code is not None:
        state.axis_code = int(axis_code)
        state.axis_release_value = int(axis_release_value)
    state.bucket = bucket
    device_runtime.state.rapidfire_outputs[event_name] = state


def stop_rapidfire(device_runtime: GrabbedDeviceRuntime, event_name: str) -> None:
    device_runtime.state.rapidfire_active[event_name] = False
    task = device_runtime.state.rapidfire_tasks.pop(event_name, None)
    if task is not None and not task.done():
        task.cancel()
    state = device_runtime.state.rapidfire_outputs.pop(event_name, None)
    if not state:
        return
    kind = state.kind
    if kind in {"trigger", "axis"}:
        axis_code = state.axis_code
        if axis_code is not None:
            ensure_abs_axis_released(
                device_runtime,
                axis_code,
                evdev_mod=evdev,
                uinput_writer=_uinput_writer,
                uinput_dev=state.uinput,
                bucket=state.bucket,
                release_value=state.axis_release_value,
            )
        return
    if kind == "relative":
        return
    if kind == "key":
        code = state.code
        uinput = state.uinput
        if code is not None:
            ensure_key_released(device_runtime, code, uinput, bucket=state.bucket)
            return


async def stop_rapidfire_async(
    device_runtime: GrabbedDeviceRuntime,
    event_name: str,
    *,
    asyncio_mod: AsyncioModule,
) -> None:
    task = device_runtime.state.rapidfire_tasks.get(event_name)
    stop_rapidfire(device_runtime, event_name)
    if task is not None and not task.done():
        with contextlib.suppress(asyncio.CancelledError):
            await task


def finish_rapidfire_task(
    device_runtime: GrabbedDeviceRuntime, event_name: str, task: object
) -> None:
    active_task = device_runtime.state.rapidfire_tasks.get(event_name)
    if active_task is not task:
        return
    device_runtime.state.rapidfire_tasks.pop(event_name, None)
    device_runtime.state.rapidfire_active.pop(event_name, None)
    state = device_runtime.state.rapidfire_outputs.pop(event_name, None)
    if not state:
        return
    kind = state.kind
    if kind in {"trigger", "axis"}:
        axis_code = state.axis_code
        if axis_code is not None:
            ensure_abs_axis_released(
                device_runtime,
                axis_code,
                evdev_mod=evdev,
                uinput_writer=_uinput_writer,
                uinput_dev=state.uinput,
                bucket=state.bucket,
                release_value=state.axis_release_value,
            )
        return
    if kind == "relative":
        return
    if kind == "key":
        code = state.code
        uinput = state.uinput
        if code is not None:
            ensure_key_released(device_runtime, code, uinput, bucket=state.bucket)


async def rapidfire_abs_axis(
    device_runtime: GrabbedDeviceRuntime,
    axis_code: int,
    active_value: int,
    release_value: int,
    hold_ms: int,
    wait_ms: int,
    event_name: str,
    uinput_dev: object | None,
    *,
    asyncio_mod: AsyncioModule,
    evdev_mod: EvdevModule,
    uinput_writer: UInputWriter,
    bucket: str | None = None,
) -> None:
    hold = clamp_rapidfire_hold_ms(hold_ms) / 1000.0
    wait = clamp_rapidfire_wait_ms(wait_ms) / 1000.0
    task = asyncio_mod.current_task()
    pressed = False

    try:
        while (
            device_runtime.state.rapidfire_active.get(event_name, False)
            and runtime_is_running(device_runtime)
        ):
            gamepad_uinput = uinput_writer(uinput_dev)
            if gamepad_uinput is None:
                return
            write_abs_axis(
                device_runtime,
                uinput_dev,
                axis_code,
                active_value,
                evdev_mod=evdev_mod,
                uinput_writer=uinput_writer,
                bucket=bucket,
            )
            pressed = True
            await asyncio_mod.sleep(hold)

            if pressed:
                write_abs_axis(
                    device_runtime,
                    uinput_dev,
                    axis_code,
                    release_value,
                    evdev_mod=evdev_mod,
                    uinput_writer=uinput_writer,
                    bucket=bucket,
                )
                pressed = False
            if not device_runtime.state.rapidfire_active.get(event_name, False):
                break

            await asyncio_mod.sleep(wait)
    except Exception:
        pass
    finally:
        if pressed:
            ensure_abs_axis_released(
                device_runtime,
                axis_code,
                evdev_mod=evdev_mod,
                uinput_writer=uinput_writer,
                uinput_dev=uinput_dev,
                bucket=bucket,
                release_value=release_value,
            )
        if task is not None:
            finish_rapidfire_task(device_runtime, event_name, task)


async def tap_abs_axis(
    device_runtime: GrabbedDeviceRuntime,
    axis_code: int,
    active_value: int,
    release_value: int,
    hold_ms: int,
    event_name: str,
    uinput_dev: object | None,
    *,
    asyncio_mod: AsyncioModule,
    evdev_mod: EvdevModule,
    uinput_writer: UInputWriter,
    bucket: str | None = None,
) -> None:
    hold = hold_ms / 1000.0

    try:
        gamepad_uinput = uinput_writer(uinput_dev)
        if gamepad_uinput is None:
            return
        write_abs_axis(
            device_runtime,
            uinput_dev,
            axis_code,
            active_value,
            evdev_mod=evdev_mod,
            uinput_writer=uinput_writer,
            bucket=bucket,
        )
        await asyncio_mod.sleep(hold)
        write_abs_axis(
            device_runtime,
            uinput_dev,
            axis_code,
            release_value,
            evdev_mod=evdev_mod,
            uinput_writer=uinput_writer,
            bucket=bucket,
        )
    except Exception:
        pass
    finally:
        device_runtime.state.tap_active.pop(event_name, None)


async def rapidfire_trigger(
    device_runtime: GrabbedDeviceRuntime,
    axis_code: int,
    hold_ms: int,
    wait_ms: int,
    event_name: str,
    uinput_dev: object | None,
    *,
    asyncio_mod: AsyncioModule,
    evdev_mod: EvdevModule,
    uinput_writer: UInputWriter,
    bucket: str | None = None,
) -> None:
    await rapidfire_abs_axis(
        device_runtime,
        axis_code,
        255,
        0,
        hold_ms,
        wait_ms,
        event_name,
        uinput_dev,
        asyncio_mod=asyncio_mod,
        evdev_mod=evdev_mod,
        uinput_writer=uinput_writer,
        bucket=bucket,
    )


async def tap_trigger(
    device_runtime: GrabbedDeviceRuntime,
    axis_code: int,
    hold_ms: int,
    event_name: str,
    uinput_dev: object | None,
    *,
    asyncio_mod: AsyncioModule,
    evdev_mod: EvdevModule,
    uinput_writer: UInputWriter,
    bucket: str | None = None,
) -> None:
    await tap_abs_axis(
        device_runtime,
        axis_code,
        255,
        0,
        hold_ms,
        event_name,
        uinput_dev,
        asyncio_mod=asyncio_mod,
        evdev_mod=evdev_mod,
        uinput_writer=uinput_writer,
        bucket=bucket,
    )


async def rapidfire_key(
    device_runtime: GrabbedDeviceRuntime,
    code: int,
    hold_ms: int,
    wait_ms: int,
    event_name: str,
    uinput_dev: object | None,
    *,
    asyncio_mod: AsyncioModule,
    bucket: str | None = None,
) -> None:
    hold = clamp_rapidfire_hold_ms(hold_ms) / 1000.0
    wait = clamp_rapidfire_wait_ms(wait_ms) / 1000.0
    task = asyncio_mod.current_task()
    pressed = False

    try:
        while (
            device_runtime.state.rapidfire_active.get(event_name, False)
            and runtime_is_running(device_runtime)
        ):
            write_key(
                device_runtime,
                uinput_dev,
                code,
                1,
                evdev_mod=evdev,
                uinput_writer=_uinput_writer,
                bucket=bucket,
            )
            pressed = True
            await asyncio_mod.sleep(hold)

            if pressed:
                write_key(
                    device_runtime,
                    uinput_dev,
                    code,
                    0,
                    evdev_mod=evdev,
                    uinput_writer=_uinput_writer,
                    bucket=bucket,
                )
                pressed = False
            if not device_runtime.state.rapidfire_active.get(event_name, False):
                break

            await asyncio_mod.sleep(wait)
    except Exception:
        pass
    finally:
        if pressed:
            ensure_key_released(device_runtime, code, uinput_dev, bucket=bucket)
        if task is not None:
            finish_rapidfire_task(device_runtime, event_name, task)


async def tap_key(
    device_runtime: GrabbedDeviceRuntime,
    code: int,
    hold_ms: int,
    event_name: str,
    uinput_dev: object | None,
    *,
    asyncio_mod: AsyncioModule,
    bucket: str | None = None,
) -> None:
    hold = hold_ms / 1000.0

    try:
        write_key(
            device_runtime,
            uinput_dev,
            code,
            1,
            evdev_mod=evdev,
            uinput_writer=_uinput_writer,
            bucket=bucket,
        )
        await asyncio_mod.sleep(hold)
        write_key(
            device_runtime,
            uinput_dev,
            code,
            0,
            evdev_mod=evdev,
            uinput_writer=_uinput_writer,
            bucket=bucket,
        )
    except Exception:
        pass
    finally:
        device_runtime.state.tap_active.pop(event_name, None)


async def rapidfire_relative(
    device_runtime: GrabbedDeviceRuntime,
    code: int,
    value: int,
    hold_ms: int,
    wait_ms: int,
    event_name: str,
    uinput_dev: object | None,
    *,
    asyncio_mod: AsyncioModule,
) -> None:
    hold = clamp_rapidfire_hold_ms(hold_ms) / 1000.0
    wait = clamp_rapidfire_wait_ms(wait_ms) / 1000.0
    task = asyncio_mod.current_task()

    try:
        await rapidfire_relative_pulses(
            emit_pulse=lambda: write_relative_pulse(
                uinput_dev,
                code,
                value,
                ev_rel_code=evdev.ecodes.EV_REL,
                uinput_writer=_uinput_writer,
            ),
            is_active=lambda: (
                device_runtime.state.rapidfire_active.get(event_name, False)
                and runtime_is_running(device_runtime)
            ),
            hold_s=hold,
            wait_s=wait,
            asyncio_mod=asyncio_mod,
        )
    except Exception:
        pass
    finally:
        if task is not None:
            finish_rapidfire_task(device_runtime, event_name, task)


async def tap_relative(
    device_runtime: GrabbedDeviceRuntime,
    code: int,
    value: int,
    hold_ms: int,
    event_name: str,
    uinput_dev: object | None,
    *,
    asyncio_mod: AsyncioModule,
) -> None:
    hold = hold_ms / 1000.0

    try:
        await tap_relative_pulse(
            emit_pulse=lambda: write_relative_pulse(
                uinput_dev,
                code,
                value,
                ev_rel_code=evdev.ecodes.EV_REL,
                uinput_writer=_uinput_writer,
            ),
            hold_s=hold,
            asyncio_mod=asyncio_mod,
        )
    except Exception:
        pass
    finally:
        device_runtime.state.tap_active.pop(event_name, None)


async def rapidfire_move(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction,
    event_name: str,
    hold_ms: int,
    wait_ms: int,
    *,
    asyncio_mod: AsyncioModule,
) -> None:
    hold = clamp_rapidfire_hold_ms(hold_ms) / 1000.0
    wait = clamp_rapidfire_wait_ms(wait_ms) / 1000.0
    task = asyncio_mod.current_task()

    try:
        while (
            device_runtime.state.rapidfire_active.get(event_name, False)
            and runtime_is_running(device_runtime)
        ):
            await emit_move_action(device_runtime, action)
            await asyncio_mod.sleep(hold)

            if not device_runtime.state.rapidfire_active.get(event_name, False):
                break

            await asyncio_mod.sleep(wait)
    except Exception:
        pass
    finally:
        if task is not None:
            finish_rapidfire_task(device_runtime, event_name, task)


async def tap_move(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction,
    event_name: str,
    hold_ms: int,
    *,
    asyncio_mod: AsyncioModule,
) -> None:
    hold = hold_ms / 1000.0

    try:
        await emit_move_action(device_runtime, action)
        await asyncio_mod.sleep(hold)
    except Exception:
        pass
    finally:
        device_runtime.state.tap_active.pop(event_name, None)


def _uinput_writer(device: object | None) -> WritableUInput | None:
    return cast(WritableUInput | None, device)
