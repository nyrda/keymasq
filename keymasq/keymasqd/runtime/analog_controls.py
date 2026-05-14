import asyncio
import logging
import math
import time
from dataclasses import dataclass

from keymasq.common.models import (
    ActionType,
    AnalogActionThreshold,
    AnalogControlConfig,
    MappingAction,
)
from keymasq.keymasqd.runtime import grabbed_device_actions as runtime_actions
from keymasq.keymasqd.runtime.grabbed_device_types import (
    ActionExecutionDeps,
    GrabbedDeviceRuntime,
    InputEventLike,
)

log = logging.getLogger("keymasqd.runtime.analog_controls")
DEFAULT_STICK_MIN = -32768
DEFAULT_STICK_MAX = 32767
DEFAULT_TRIGGER_MIN = 0
DEFAULT_TRIGGER_MAX = 255


@dataclass
class _SyntheticInputEvent:
    type: int
    code: int
    value: int


def normalize_axis_value(raw_value: int, minimum: int, maximum: int) -> float:
    if minimum >= maximum:
        minimum = DEFAULT_STICK_MIN
        maximum = DEFAULT_STICK_MAX

    midpoint = (float(minimum) + float(maximum)) / 2.0
    raw = float(raw_value)
    if raw < midpoint:
        span = max(1.0, midpoint - float(minimum))
        normalized = (raw - midpoint) / span
    else:
        span = max(1.0, float(maximum) - midpoint)
        normalized = (raw - midpoint) / span
    return max(-1.0, min(1.0, normalized))


def normalize_trigger_value(raw_value: int, minimum: int, maximum: int) -> float:
    if minimum >= maximum:
        minimum = DEFAULT_TRIGGER_MIN
        maximum = DEFAULT_TRIGGER_MAX
    span = max(1.0, float(maximum) - float(minimum))
    normalized = (float(raw_value) - float(minimum)) / span
    return max(0.0, min(1.0, normalized))


async def process_analog_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    mapping: dict[str, MappingAction],
    *,
    deps: ActionExecutionDeps,
) -> bool:
    binding = device_runtime.analog_axis_bindings.get((int(event.type), int(event.code)))
    if binding is None:
        return False

    analog_id, axis_role = binding
    action = mapping.get(analog_id)
    if action is None or action.action_type == ActionType.PASSTHROUGH:
        return False
    if action.action_type == ActionType.SUPPRESS:
        return True
    if action.action_type != ActionType.ANALOG_CONTROL:
        return True

    config = action.analog_control_config
    if config is None:
        return True

    fallback_range = (
        (DEFAULT_TRIGGER_MIN, DEFAULT_TRIGGER_MAX)
        if config.input_type == "trigger"
        else (DEFAULT_STICK_MIN, DEFAULT_STICK_MAX)
    )
    minimum, maximum = device_runtime.analog_axis_ranges.get((analog_id, axis_role), fallback_range)
    normalized = (
        normalize_trigger_value(int(event.value), minimum, maximum)
        if config.input_type == "trigger"
        else normalize_axis_value(int(event.value), minimum, maximum)
    )
    axis_values = device_runtime.state.analog_axis_values.setdefault(analog_id, {})
    axis_values[axis_role] = normalized

    await _evaluate_thresholds(
        device_runtime,
        analog_id,
        config,
        event,
        deps=deps,
    )
    _ensure_mouse_task(device_runtime, analog_id, deps=deps)
    return True


async def reset_analog_controls(
    device_runtime: GrabbedDeviceRuntime,
    *,
    deps: ActionExecutionDeps,
) -> None:
    mapping = device_runtime.mapping_getter()
    for source_id, active in list(device_runtime.state.analog_active_thresholds.items()):
        action = mapping.get(source_id)
        config = action.analog_control_config if action else None
        if action is None or config is None:
            continue
        for threshold_key in list(active):
            index = _threshold_index(threshold_key)
            if index is None or index >= len(config.thresholds):
                continue
            await _release_threshold_actions(
                device_runtime,
                source_id,
                index,
                config.thresholds[index],
                deps=deps,
            )

    for task in list(device_runtime.state.analog_mouse_tasks.values()):
        if not task.done():
            task.cancel()
    if device_runtime.state.analog_mouse_tasks:
        await asyncio.gather(
            *device_runtime.state.analog_mouse_tasks.values(),
            return_exceptions=True,
        )

    device_runtime.state.analog_axis_values.clear()
    device_runtime.state.analog_active_thresholds.clear()
    device_runtime.state.analog_mouse_tasks.clear()
    device_runtime.state.analog_mouse_accumulators.clear()


async def _evaluate_thresholds(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    config: AnalogControlConfig,
    event: InputEventLike,
    *,
    deps: ActionExecutionDeps,
) -> None:
    source_active = device_runtime.state.analog_active_thresholds.setdefault(source_id, set())
    axis_values = device_runtime.state.analog_axis_values.setdefault(source_id, {})
    for index, threshold in enumerate(config.thresholds):
        value = float(axis_values.get(threshold.axis, 0.0))
        key = _threshold_key(source_id, index)
        is_active = key in source_active
        if not is_active and _inside(value, threshold.trigger_min, threshold.trigger_max):
            source_active.add(key)
            await _activate_threshold_actions(
                device_runtime,
                source_id,
                index,
                threshold,
                event,
                deps=deps,
            )
        elif is_active and not _inside(value, threshold.release_min, threshold.release_max):
            source_active.discard(key)
            await _release_threshold_actions(
                device_runtime,
                source_id,
                index,
                threshold,
                deps=deps,
                event_type=int(event.type),
                event_code=int(event.code),
            )


async def _activate_threshold_actions(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    index: int,
    threshold: AnalogActionThreshold,
    event: InputEventLike,
    *,
    deps: ActionExecutionDeps,
) -> None:
    synthetic = _SyntheticInputEvent(int(event.type), int(event.code), 1)
    for action_index, action in enumerate(threshold.actions):
        if action.action_type in {
            ActionType.PASSTHROUGH,
            ActionType.SUPPRESS,
            ActionType.ANALOG_CONTROL,
        }:
            continue
        await runtime_actions.execute_action(
            device_runtime,
            action,
            synthetic,
            _child_event_name(source_id, index, action_index),
            deps=deps,
        )


async def _release_threshold_actions(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    index: int,
    threshold: AnalogActionThreshold,
    *,
    deps: ActionExecutionDeps,
    event_type: int = 0,
    event_code: int = 0,
) -> None:
    synthetic = _SyntheticInputEvent(event_type, event_code, 0)
    for action_index, action in enumerate(threshold.actions):
        if action.action_type in {
            ActionType.PASSTHROUGH,
            ActionType.SUPPRESS,
            ActionType.ANALOG_CONTROL,
        }:
            continue
        await runtime_actions.execute_action(
            device_runtime,
            action,
            synthetic,
            _child_event_name(source_id, index, action_index),
            deps=deps,
        )


def _ensure_mouse_task(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    *,
    deps: ActionExecutionDeps,
) -> None:
    action = device_runtime.mapping_getter().get(source_id)
    config = action.analog_control_config if action else None
    if config is None or config.input_type != "stick" or not config.mouse_motion.enabled:
        return

    task = device_runtime.state.analog_mouse_tasks.get(source_id)
    if task is not None and not task.done():
        return

    device_runtime.state.analog_mouse_tasks[source_id] = deps.asyncio_mod.create_task(
        _mouse_motion_loop(device_runtime, source_id, deps=deps)
    )


async def _mouse_motion_loop(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    *,
    deps: ActionExecutionDeps,
) -> None:
    previous = time.monotonic()
    try:
        while True:
            action = device_runtime.mapping_getter().get(source_id)
            config = action.analog_control_config if action else None
            if (
                action is None
                or action.action_type != ActionType.ANALOG_CONTROL
                or config is None
                or config.input_type != "stick"
                or not config.mouse_motion.enabled
            ):
                return

            tick_s = max(0.001, float(config.mouse_motion.tick_ms) / 1000.0)
            await deps.asyncio_mod.sleep(tick_s)
            now = time.monotonic()
            dt = max(0.0, now - previous)
            previous = now

            axis_values = device_runtime.state.analog_axis_values.get(source_id, {})
            x = float(axis_values.get("x", 0.0))
            y = float(axis_values.get("y", 0.0))
            if config.mouse_motion.invert_x:
                x = -x
            if config.mouse_motion.invert_y:
                y = -y

            dx, dy = _motion_delta(
                x,
                y,
                speed=float(config.mouse_motion.speed),
                deadzone=float(config.mouse_motion.deadzone),
                curve=config.mouse_motion.curve,
                dt=dt,
            )
            await _emit_mouse_delta(device_runtime, source_id, dx, dy, deps=deps)
    except asyncio.CancelledError:
        raise
    finally:
        task = device_runtime.state.analog_mouse_tasks.get(source_id)
        if task is asyncio.current_task():
            device_runtime.state.analog_mouse_tasks.pop(source_id, None)
        device_runtime.state.analog_mouse_accumulators.pop(source_id, None)


def _motion_delta(
    x: float,
    y: float,
    *,
    speed: float,
    deadzone: float,
    curve: str,
    dt: float,
) -> tuple[float, float]:
    magnitude = math.sqrt(x * x + y * y)
    if magnitude <= deadzone:
        return 0.0, 0.0
    scaled = max(0.0, min(1.0, (magnitude - deadzone) / max(0.001, 1.0 - deadzone)))
    if curve == "linear":
        curved = scaled
    elif curve == "fast":
        curved = scaled**0.65
    else:
        curved = scaled**1.8
    direction_x = x / magnitude
    direction_y = y / magnitude
    distance = curved * max(0.0, speed) * dt
    return direction_x * distance, direction_y * distance


async def _emit_mouse_delta(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    dx: float,
    dy: float,
    *,
    deps: ActionExecutionDeps,
) -> None:
    old_x, old_y = device_runtime.state.analog_mouse_accumulators.get(source_id, (0.0, 0.0))
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


def _inside(value: float, minimum: float, maximum: float) -> bool:
    return minimum <= value <= maximum


def _threshold_key(source_id: str, index: int) -> str:
    return f"{source_id}:{index}"


def _threshold_index(threshold_key: str) -> int | None:
    try:
        return int(threshold_key.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


def _child_event_name(source_id: str, threshold_index: int, action_index: int) -> str:
    return f"{source_id}#analog_threshold#{threshold_index}#{action_index}"


def _whole_step(value: float) -> int:
    if value >= 0:
        return int(math.floor(value))
    return int(math.ceil(value))
