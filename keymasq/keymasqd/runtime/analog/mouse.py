"""Mouse-motion calculations, area mode, and continuous task lifecycle."""

import asyncio
import math
import time

from keymasq.common.model.analog import (
    AnalogControlConfig,
    AnalogMouseMotionConfig,
    analog_gamepad_output_distance,
)
from keymasq.keymasqd.runtime.analog.curves import (
    apply_control_axis_output_curve,
    apply_signed_axis_output_curve,
)
from keymasq.keymasqd.runtime.grabbed_device.types import (
    ActionExecutionDeps,
    GrabbedDeviceRuntime,
)


def ensure_mouse_task(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    if not config.mouse_motion.enabled or config.mouse_motion.mode == "area":
        return

    task = device_runtime.state.analog_mouse_tasks.get(state_key)
    if task is not None and not task.done():
        return

    device_runtime.state.analog_mouse_tasks[state_key] = deps.asyncio_mod.create_task(
        _mouse_motion_loop(device_runtime, state_key, config, deps=deps)
    )


async def cancel_mouse_tasks(
    device_runtime: GrabbedDeviceRuntime,
    *,
    preserve_state_keys: set[str],
    state_key_prefix: str | None = None,
) -> None:
    tasks = [
        task
        for state_key, task in list(device_runtime.state.analog_mouse_tasks.items())
        if state_key not in preserve_state_keys
        and (state_key_prefix is None or state_key.startswith(state_key_prefix))
    ]
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _mouse_motion_loop(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    previous = time.monotonic()
    try:
        while True:
            if not config.mouse_motion.enabled:
                return

            tick_s = max(0.001, float(config.mouse_motion.tick_ms) / 1000.0)
            await deps.asyncio_mod.sleep(tick_s)
            now = time.monotonic()
            dt = max(0.0, now - previous)
            previous = now

            axis_values = device_runtime.state.analog_axis_values.get(state_key, {})
            if config.input_type == "axis":
                dx, dy = axis_motion_delta(
                    float(axis_values.get("x", 0.0)),
                    signed_value=float(axis_values.get("x_signed", 0.0)),
                    direction=config.mouse_motion.direction,
                    speed=float(config.mouse_motion.speed),
                    deadzone=float(config.mouse_motion.deadzone),
                    sensitivity=float(config.mouse_motion.sensitivity),
                    response_curve=float(config.mouse_motion.response_curve),
                    invert=bool(config.mouse_motion.invert_x),
                    dt=dt,
                )
            else:
                x = float(axis_values.get("x", 0.0))
                y = float(axis_values.get("y", 0.0))
                if config.mouse_motion.invert_x:
                    x = -x
                if config.mouse_motion.invert_y:
                    y = -y

                dx, dy = motion_delta(
                    x,
                    y,
                    speed_x=float(
                        config.mouse_motion.speed_x
                        if config.mouse_motion.speed_x is not None
                        else config.mouse_motion.speed
                    ),
                    speed_y=float(
                        config.mouse_motion.speed_y
                        if config.mouse_motion.speed_y is not None
                        else config.mouse_motion.speed
                    ),
                    deadzone=float(config.mouse_motion.deadzone),
                    sensitivity=float(config.mouse_motion.sensitivity),
                    response_curve=float(config.mouse_motion.response_curve),
                    dt=dt,
                )
            await _emit_mouse_delta(device_runtime, state_key, dx, dy, deps=deps)
    except asyncio.CancelledError:
        raise
    finally:
        task = device_runtime.state.analog_mouse_tasks.get(state_key)
        if task is asyncio.current_task():
            device_runtime.state.analog_mouse_tasks.pop(state_key, None)
        device_runtime.state.analog_mouse_accumulators.pop(state_key, None)


def motion_delta(
    x: float,
    y: float,
    *,
    speed_x: float,
    speed_y: float,
    deadzone: float,
    sensitivity: float,
    response_curve: float,
    dt: float,
) -> tuple[float, float]:
    magnitude = math.sqrt(x * x + y * y)
    scaled = analog_gamepad_output_distance(
        magnitude,
        deadzone=deadzone,
        sensitivity=sensitivity,
        response_curve=response_curve,
    )
    if scaled <= 0.0 or magnitude <= 0.0:
        return 0.0, 0.0
    direction_x = x / magnitude
    direction_y = y / magnitude
    return (
        direction_x * scaled * max(0.0, speed_x) * dt,
        direction_y * scaled * max(0.0, speed_y) * dt,
    )


def mouse_area_position(
    x: float,
    y: float,
    motion: AnalogMouseMotionConfig,
) -> tuple[float, float]:
    """Transform a source position before calculating displacement."""
    if motion.area_input_style == "stick":
        x = apply_signed_axis_output_curve(
            x,
            deadzone=motion.deadzone,
            sensitivity=motion.sensitivity,
            response_curve=motion.response_curve,
        )
        y = apply_signed_axis_output_curve(
            y,
            deadzone=motion.deadzone,
            sensitivity=motion.sensitivity,
            response_curve=motion.response_curve,
        )
    return (
        x * motion.area_radius_x * (-1 if motion.invert_x else 1),
        y * motion.area_radius_y * (-1 if motion.invert_y else 1),
    )


async def emit_mouse_area_motion(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    """Consume one complete position with stick or touchpad contact semantics."""
    state = device_runtime.state
    values = state.analog_axis_values.get(state_key, {})
    if "x" not in values or "y" not in values:
        return
    x, y = values["x"], values["y"]
    motion = config.mouse_motion
    touchpad = motion.area_input_style == "touchpad"
    if touchpad and x == 0.0 and y == 0.0:
        state.analog_mouse_area_positions.pop(state_key, None)
        state.analog_mouse_accumulators.pop(state_key, None)
        state.analog_mouse_area_needs_release.discard(state_key)
        return
    if state_key in state.analog_mouse_area_needs_release:
        return

    target = mouse_area_position(x, y, motion)
    previous = state.analog_mouse_area_positions.get(state_key)
    if touchpad and previous is None:
        state.analog_mouse_area_positions[state_key] = target
        state.analog_mouse_accumulators.pop(state_key, None)
        return
    if previous is None:
        previous = (0.0, 0.0)
    if (
        not touchpad
        and target != (0.0, 0.0)
        and previous == (0.0, 0.0)
        and motion.area_start_enabled
        and device_runtime.cursor_position_setter is not None
    ):
        await device_runtime.cursor_position_setter(motion.area_start_x, motion.area_start_y)
        state.analog_mouse_accumulators.pop(state_key, None)
    state.analog_mouse_area_positions[state_key] = target
    await _emit_mouse_delta(
        device_runtime,
        state_key,
        target[0] - previous[0],
        target[1] - previous[1],
        deps=deps,
    )


def axis_motion_delta(
    value: float,
    *,
    signed_value: float,
    direction: str,
    speed: float,
    deadzone: float,
    sensitivity: float,
    response_curve: float,
    dt: float,
    invert: bool = False,
) -> tuple[float, float]:
    if direction in {"horizontal", "vertical"}:
        scaled = apply_signed_axis_output_curve(
            signed_value,
            deadzone=deadzone,
            sensitivity=sensitivity,
            response_curve=response_curve,
        )
    else:
        scaled = apply_control_axis_output_curve(
            value,
            deadzone=deadzone,
            sensitivity=sensitivity,
            response_curve=response_curve,
        )
    distance = scaled * max(0.0, speed) * dt
    if invert:
        distance = -distance
    if direction == "horizontal":
        return distance, 0.0
    if direction == "vertical":
        return 0.0, distance
    if direction == "left":
        return -distance, 0.0
    if direction == "up":
        return 0.0, -distance
    if direction == "down":
        return 0.0, distance
    return distance, 0.0


async def _emit_mouse_delta(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    dx: float,
    dy: float,
    *,
    deps: ActionExecutionDeps,
) -> None:
    old_x, old_y = device_runtime.state.analog_mouse_accumulators.get(
        source_id,
        (0.0, 0.0),
    )
    accum_x = old_x + dx
    accum_y = old_y + dy
    emit_x = _whole_step(accum_x)
    emit_y = _whole_step(accum_y)
    device_runtime.state.analog_mouse_accumulators[source_id] = (
        accum_x - emit_x,
        accum_y - emit_y,
    )
    if emit_x == 0 and emit_y == 0:
        return

    mouse = deps.uinput_writer(device_runtime.mouse_uinput)
    if mouse is None:
        return
    if emit_x:
        mouse.write(deps.evdev_mod.ecodes.EV_REL, deps.evdev_mod.ecodes.REL_X, emit_x)
    if emit_y:
        mouse.write(deps.evdev_mod.ecodes.EV_REL, deps.evdev_mod.ecodes.REL_Y, emit_y)
    mouse.syn()


def _whole_step(value: float) -> int:
    if value >= 0:
        return int(math.floor(value))
    return int(math.ceil(value))
