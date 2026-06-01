import asyncio
import contextlib

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
    ActionRuntime,
    AsyncioEvent,
    AsyncioModule,
    EvdevModule,
    OutputTracker,
    RapidfireOutputState,
    TaskFactory,
    UInputWriter,
    identity_uinput_writer,
    runtime_is_running,
)
from keymasq.keymasqd.runtime.mouse_actions import (
    rapidfire_relative_pulses,
    tap_relative_pulse,
    write_relative_pulse,
)


async def emit_move_action(
    device_runtime: ActionRuntime,
    action: MappingAction,
) -> None:
    if action.action_type == ActionType.MOUSE_MOVE_ABS:
        cursor_position_setter = device_runtime.cursor_position_setter
        if cursor_position_setter is not None:
            await cursor_position_setter(int(action.move_x), int(action.move_y))
            return
    emit_configured_mouse_move(device_runtime, action)


def start_rapidfire_task(
    device_runtime: ActionRuntime,
    event_name: str,
    kind: str,
    task_factory: TaskFactory,
    *,
    code: int | None,
    uinput: object | None,
    axis_code: int | None,
    bucket: str | None = None,
    axis_release_value: int = 0,
    output_tracker: OutputTracker | None = None,
) -> None:
    stop_rapidfire(device_runtime, event_name)
    task = task_factory()
    device_runtime.state.rapidfire_active[event_name] = True
    device_runtime.state.rapidfire_tasks[event_name] = task

    state = RapidfireOutputState(kind=kind)
    if code is not None:
        state.code = int(code)
    if uinput is not None:
        state.uinput = uinput
    if axis_code is not None:
        state.axis_code = int(axis_code)
        state.axis_release_value = int(axis_release_value)
    state.bucket = bucket
    state.output_tracker = output_tracker
    device_runtime.state.rapidfire_outputs[event_name] = state


def stop_rapidfire(device_runtime: ActionRuntime, event_name: str) -> None:
    device_runtime.state.rapidfire_active[event_name] = False
    task = device_runtime.state.rapidfire_tasks.pop(event_name, None)
    if task is not None and not task.done():
        task.cancel()
    state = device_runtime.state.rapidfire_outputs.pop(event_name, None)
    if not state:
        return
    kind = state.kind
    if kind == "axis":
        axis_code = state.axis_code
        if axis_code is not None:
            _release_rapidfire_abs_axis(device_runtime, state, axis_code)
        return
    if kind == "relative":
        return
    if kind == "key":
        code = state.code
        uinput = state.uinput
        if code is not None:
            _release_rapidfire_key(device_runtime, state, code, uinput)
            return


async def stop_rapidfire_async(
    device_runtime: ActionRuntime,
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
    device_runtime: ActionRuntime, event_name: str, task: object
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
    if kind == "axis":
        axis_code = state.axis_code
        if axis_code is not None:
            _release_rapidfire_abs_axis(device_runtime, state, axis_code)
        return
    if kind == "relative":
        return
    if kind == "key":
        code = state.code
        uinput = state.uinput
        if code is not None:
            _release_rapidfire_key(device_runtime, state, code, uinput)


def _release_rapidfire_abs_axis(
    device_runtime: ActionRuntime,
    rapidfire_state: RapidfireOutputState,
    axis_code: int,
) -> None:
    if rapidfire_state.output_tracker is not None:
        if not rapidfire_state.pressed:
            return
        should_emit = rapidfire_state.output_tracker(
            str(rapidfire_state.bucket or "gamepad"),
            int(axis_code),
            int(rapidfire_state.axis_release_value),
        )
        rapidfire_state.pressed = False
        if not should_emit:
            return
    ensure_abs_axis_released(
        device_runtime,
        axis_code,
        evdev_mod=evdev,
        uinput_writer=identity_uinput_writer,
        uinput_dev=rapidfire_state.uinput,
        bucket=rapidfire_state.bucket,
        release_value=rapidfire_state.axis_release_value,
    )


def _release_rapidfire_key(
    device_runtime: ActionRuntime,
    rapidfire_state: RapidfireOutputState,
    code: int,
    uinput: object | None,
) -> None:
    if rapidfire_state.output_tracker is not None:
        if not rapidfire_state.pressed:
            return
        should_emit = rapidfire_state.output_tracker(
            str(rapidfire_state.bucket or "keyboard"),
            int(code),
            0,
        )
        rapidfire_state.pressed = False
        if not should_emit:
            return
    ensure_key_released(device_runtime, code, uinput, bucket=rapidfire_state.bucket)


async def rapidfire_abs_axis(
    device_runtime: ActionRuntime,
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
    started: AsyncioEvent | None = None,
    output_tracker: OutputTracker | None = None,
) -> None:
    hold = clamp_rapidfire_hold_ms(hold_ms) / 1000.0
    wait = clamp_rapidfire_wait_ms(wait_ms) / 1000.0
    task = asyncio_mod.current_task()
    pressed = False
    started_set = False

    try:
        while (
            device_runtime.state.rapidfire_active.get(event_name, False)
            and runtime_is_running(device_runtime)
        ):
            gamepad_uinput = uinput_writer(uinput_dev)
            if gamepad_uinput is None:
                return
            should_emit = True
            state = device_runtime.state.rapidfire_outputs.get(event_name)
            if output_tracker is not None:
                should_emit = output_tracker(
                    str(bucket or "gamepad"),
                    int(axis_code),
                    int(active_value),
                )
                if state is not None:
                    state.pressed = True
            if should_emit:
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
            if not started_set:
                _mark_started(started)
                started_set = True
            await asyncio_mod.sleep(hold)

            if pressed:
                should_emit = True
                state = device_runtime.state.rapidfire_outputs.get(event_name)
                if output_tracker is not None:
                    should_emit = output_tracker(
                        str(bucket or "gamepad"),
                        int(axis_code),
                        int(release_value),
                    )
                    if state is not None:
                        state.pressed = False
                if should_emit:
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
    finally:
        if pressed and event_name in device_runtime.state.rapidfire_outputs:
            state = device_runtime.state.rapidfire_outputs[event_name]
            _release_rapidfire_abs_axis(device_runtime, state, axis_code)
        if task is not None:
            finish_rapidfire_task(device_runtime, event_name, task)
        if not started_set:
            _mark_started(started)


async def tap_abs_axis(
    device_runtime: ActionRuntime,
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
    started: AsyncioEvent | None = None,
) -> None:
    hold = hold_ms / 1000.0
    pressed = False
    started_set = False

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
        pressed = True
        _mark_started(started)
        started_set = True
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
        pressed = False
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
        device_runtime.state.tap_active.pop(event_name, None)
        if not started_set:
            _mark_started(started)


async def rapidfire_key(
    device_runtime: ActionRuntime,
    code: int,
    hold_ms: int,
    wait_ms: int,
    event_name: str,
    uinput_dev: object | None,
    *,
    asyncio_mod: AsyncioModule,
    bucket: str | None = None,
    started: AsyncioEvent | None = None,
    output_tracker: OutputTracker | None = None,
) -> None:
    hold = clamp_rapidfire_hold_ms(hold_ms) / 1000.0
    wait = clamp_rapidfire_wait_ms(wait_ms) / 1000.0
    task = asyncio_mod.current_task()
    pressed = False
    started_set = False

    try:
        while (
            device_runtime.state.rapidfire_active.get(event_name, False)
            and runtime_is_running(device_runtime)
        ):
            should_emit = True
            state = device_runtime.state.rapidfire_outputs.get(event_name)
            if output_tracker is not None:
                should_emit = output_tracker(str(bucket or "keyboard"), int(code), 1)
                if state is not None:
                    state.pressed = True
            if should_emit:
                write_key(
                    device_runtime,
                    uinput_dev,
                    code,
                    1,
                    evdev_mod=evdev,
                    uinput_writer=identity_uinput_writer,
                    bucket=bucket,
                )
            pressed = True
            if not started_set:
                _mark_started(started)
                started_set = True
            await asyncio_mod.sleep(hold)

            if pressed:
                should_emit = True
                state = device_runtime.state.rapidfire_outputs.get(event_name)
                if output_tracker is not None:
                    should_emit = output_tracker(str(bucket or "keyboard"), int(code), 0)
                    if state is not None:
                        state.pressed = False
                if should_emit:
                    write_key(
                        device_runtime,
                        uinput_dev,
                        code,
                        0,
                        evdev_mod=evdev,
                        uinput_writer=identity_uinput_writer,
                        bucket=bucket,
                    )
                pressed = False
            if not device_runtime.state.rapidfire_active.get(event_name, False):
                break

            await asyncio_mod.sleep(wait)
    finally:
        if pressed and event_name in device_runtime.state.rapidfire_outputs:
            state = device_runtime.state.rapidfire_outputs[event_name]
            _release_rapidfire_key(device_runtime, state, code, uinput_dev)
        if task is not None:
            finish_rapidfire_task(device_runtime, event_name, task)
        if not started_set:
            _mark_started(started)


async def tap_key(
    device_runtime: ActionRuntime,
    code: int,
    hold_ms: int,
    event_name: str,
    uinput_dev: object | None,
    *,
    asyncio_mod: AsyncioModule,
    bucket: str | None = None,
    started: AsyncioEvent | None = None,
) -> None:
    hold = hold_ms / 1000.0
    pressed = False
    started_set = False

    try:
        write_key(
            device_runtime,
            uinput_dev,
            code,
            1,
            evdev_mod=evdev,
            uinput_writer=identity_uinput_writer,
            bucket=bucket,
        )
        pressed = True
        _mark_started(started)
        started_set = True
        await asyncio_mod.sleep(hold)
        write_key(
            device_runtime,
            uinput_dev,
            code,
            0,
            evdev_mod=evdev,
            uinput_writer=identity_uinput_writer,
            bucket=bucket,
        )
        pressed = False
    finally:
        if pressed:
            ensure_key_released(device_runtime, code, uinput_dev, bucket=bucket)
        device_runtime.state.tap_active.pop(event_name, None)
        if not started_set:
            _mark_started(started)


async def rapidfire_relative(
    device_runtime: ActionRuntime,
    code: int,
    value: int,
    hold_ms: int,
    wait_ms: int,
    event_name: str,
    uinput_dev: object | None,
    *,
    asyncio_mod: AsyncioModule,
    started: AsyncioEvent | None = None,
) -> None:
    hold = clamp_rapidfire_hold_ms(hold_ms) / 1000.0
    wait = clamp_rapidfire_wait_ms(wait_ms) / 1000.0
    task = asyncio_mod.current_task()
    started_set = False

    def emit_started_pulse() -> None:
        nonlocal started_set
        write_relative_pulse(
            uinput_dev,
            code,
            value,
            ev_rel_code=evdev.ecodes.EV_REL,
            uinput_writer=identity_uinput_writer,
        )
        if not started_set:
            _mark_started(started)
            started_set = True

    try:
        await rapidfire_relative_pulses(
            emit_pulse=emit_started_pulse,
            is_active=lambda: (
                device_runtime.state.rapidfire_active.get(event_name, False)
                and runtime_is_running(device_runtime)
            ),
            hold_s=hold,
            wait_s=wait,
            asyncio_mod=asyncio_mod,
        )
    finally:
        if task is not None:
            finish_rapidfire_task(device_runtime, event_name, task)
        if not started_set:
            _mark_started(started)


async def tap_relative(
    device_runtime: ActionRuntime,
    code: int,
    value: int,
    hold_ms: int,
    event_name: str,
    uinput_dev: object | None,
    *,
    asyncio_mod: AsyncioModule,
    started: AsyncioEvent | None = None,
) -> None:
    hold = hold_ms / 1000.0
    started_set = False

    def emit_started_pulse() -> None:
        nonlocal started_set
        write_relative_pulse(
            uinput_dev,
            code,
            value,
            ev_rel_code=evdev.ecodes.EV_REL,
            uinput_writer=identity_uinput_writer,
        )
        if not started_set:
            _mark_started(started)
            started_set = True

    try:
        await tap_relative_pulse(
            emit_pulse=emit_started_pulse,
            hold_s=hold,
            asyncio_mod=asyncio_mod,
        )
    finally:
        device_runtime.state.tap_active.pop(event_name, None)
        if not started_set:
            _mark_started(started)


async def rapidfire_move(
    device_runtime: ActionRuntime,
    action: MappingAction,
    event_name: str,
    hold_ms: int,
    wait_ms: int,
    *,
    asyncio_mod: AsyncioModule,
    started: AsyncioEvent | None = None,
) -> None:
    hold = clamp_rapidfire_hold_ms(hold_ms) / 1000.0
    wait = clamp_rapidfire_wait_ms(wait_ms) / 1000.0
    task = asyncio_mod.current_task()
    started_set = False

    try:
        while (
            device_runtime.state.rapidfire_active.get(event_name, False)
            and runtime_is_running(device_runtime)
        ):
            await emit_move_action(device_runtime, action)
            if not started_set:
                _mark_started(started)
                started_set = True
            await asyncio_mod.sleep(hold)

            if not device_runtime.state.rapidfire_active.get(event_name, False):
                break

            await asyncio_mod.sleep(wait)
    finally:
        if task is not None:
            finish_rapidfire_task(device_runtime, event_name, task)
        if not started_set:
            _mark_started(started)


async def tap_move(
    device_runtime: ActionRuntime,
    action: MappingAction,
    event_name: str,
    hold_ms: int,
    *,
    asyncio_mod: AsyncioModule,
    started: AsyncioEvent | None = None,
) -> None:
    hold = hold_ms / 1000.0
    started_set = False

    try:
        await emit_move_action(device_runtime, action)
        _mark_started(started)
        started_set = True
        await asyncio_mod.sleep(hold)
    finally:
        device_runtime.state.tap_active.pop(event_name, None)
        if not started_set:
            _mark_started(started)


def _mark_started(started: AsyncioEvent | None) -> None:
    if started is not None:
        started.set()
