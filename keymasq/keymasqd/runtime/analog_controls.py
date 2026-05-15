import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import cast

from keymasq.common.devices import resolve_evdev_code
from keymasq.common.models import (
    ActionType,
    AnalogActionThreshold,
    AnalogControlConfig,
    MappingAction,
    analog_gamepad_output_distance,
)
from keymasq.keymasqd.runtime import grabbed_device_actions as runtime_actions
from keymasq.keymasqd.runtime.grabbed_device_outputs import track_abs_state
from keymasq.keymasqd.runtime.grabbed_device_types import (
    ActionExecutionDeps,
    AnalogGamepadOutputState,
    GrabbedDeviceRuntime,
    InputEventLike,
)

log = logging.getLogger("keymasqd.runtime.analog_controls")
DEFAULT_STICK_MIN = -32768
DEFAULT_STICK_MAX = 32767
DEFAULT_TRIGGER_MIN = 0
DEFAULT_TRIGGER_MAX = 255
STICK_OUTPUT_AXES = {
    "left_stick": ("ABS_X", "ABS_Y"),
    "right_stick": ("ABS_RX", "ABS_RY"),
}
TRIGGER_OUTPUT_AXES = {
    "left_trigger": "ABS_Z",
    "right_trigger": "ABS_RZ",
}


@dataclass
class _SyntheticInputEvent:
    type: int
    code: int
    value: int


def normalize_axis_value(
    raw_value: int,
    minimum: int,
    maximum: int,
    *,
    center: int | None = None,
    invert: bool = False,
) -> float:
    if minimum >= maximum:
        minimum = DEFAULT_STICK_MIN
        maximum = DEFAULT_STICK_MAX

    midpoint = float(center) if center is not None else (float(minimum) + float(maximum)) / 2.0
    raw = float(raw_value)
    if raw < midpoint:
        span = max(1.0, midpoint - float(minimum))
        normalized = (raw - midpoint) / span
    else:
        span = max(1.0, float(maximum) - midpoint)
        normalized = (raw - midpoint) / span
    if invert:
        normalized = -normalized
    return max(-1.0, min(1.0, normalized))


def normalize_control_axis_value(
    raw_value: int,
    minimum: int,
    maximum: int,
    *,
    rest: int | None = None,
) -> float:
    if minimum >= maximum:
        minimum = DEFAULT_TRIGGER_MIN
        maximum = DEFAULT_TRIGGER_MAX
    if rest is None:
        rest = minimum if minimum >= 0 else 0
    positive_span = float(maximum) - float(rest)
    negative_span = float(minimum) - float(rest)
    active_span = positive_span if abs(positive_span) >= abs(negative_span) else negative_span
    if abs(active_span) < 1.0:
        active_span = float(maximum) - float(minimum)
    if abs(active_span) < 1.0:
        return 0.0
    normalized = (float(raw_value) - float(rest)) / active_span
    return max(0.0, min(1.0, normalized))


def denormalize_axis_value(
    value: float,
    minimum: int,
    maximum: int,
    *,
    center: int | None = None,
    invert: bool = False,
) -> int:
    if minimum >= maximum:
        minimum = DEFAULT_STICK_MIN
        maximum = DEFAULT_STICK_MAX
    normalized = max(-1.0, min(1.0, float(value)))
    if invert:
        normalized = -normalized
    midpoint = int(center) if center is not None else int(round((minimum + maximum) / 2.0))
    if normalized >= 0.0:
        return min(maximum, int(round(midpoint + normalized * (maximum - midpoint))))
    return max(minimum, int(round(midpoint + normalized * (midpoint - minimum))))


def denormalize_control_axis_value(
    value: float,
    minimum: int,
    maximum: int,
    *,
    rest: int | None = None,
    invert: bool = False,
) -> int:
    if minimum >= maximum:
        minimum = DEFAULT_TRIGGER_MIN
        maximum = DEFAULT_TRIGGER_MAX
    if rest is None:
        rest = minimum if minimum >= 0 else 0
    normalized = max(0.0, min(1.0, float(value)))
    endpoint = minimum if invert else maximum
    return max(minimum, min(maximum, int(round(float(rest) + normalized * (endpoint - rest)))))


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
        if config.input_type == "axis"
        else (DEFAULT_STICK_MIN, DEFAULT_STICK_MAX)
    )
    minimum, maximum = device_runtime.analog_axis_ranges.get((analog_id, axis_role), fallback_range)
    calibration = device_runtime.analog_axis_calibrations.get((analog_id, axis_role), {})
    rest_value = calibration.get("rest")
    center_value = calibration.get("center")
    if config.input_type == "axis":
        rest = rest_value if isinstance(rest_value, int) else (minimum if minimum >= 0 else 0)
        normalized = normalize_control_axis_value(
            int(event.value),
            minimum,
            maximum,
            rest=rest,
        )
        signed_normalized = normalize_axis_value(
            int(event.value),
            minimum,
            maximum,
            center=rest,
        )
    else:
        normalized = normalize_axis_value(
            int(event.value),
            minimum,
            maximum,
            center=center_value if isinstance(center_value, int) else None,
            invert=bool(calibration.get("invert", False)),
        )
        signed_normalized = normalized
    axis_values = device_runtime.state.analog_axis_values.setdefault(analog_id, {})
    axis_values[axis_role] = normalized
    axis_values[f"{axis_role}_signed"] = signed_normalized

    await _evaluate_thresholds(
        device_runtime,
        analog_id,
        config,
        event,
        deps=deps,
    )
    _emit_gamepad_output(device_runtime, analog_id, config, deps=deps)
    _ensure_mouse_task(device_runtime, analog_id, deps=deps)
    return True


async def reset_analog_controls(
    device_runtime: GrabbedDeviceRuntime,
    *,
    deps: ActionExecutionDeps,
) -> None:
    mapping = device_runtime.mapping_getter()
    _reset_recorded_gamepad_outputs(device_runtime, deps=deps)
    for source_id, active in list(device_runtime.state.analog_active_thresholds.items()):
        action = mapping.get(source_id)
        config = action.analog_control_config if action else None
        for threshold_key in list(active):
            index = _threshold_index(threshold_key)
            actions = device_runtime.state.analog_active_threshold_actions.get(threshold_key)
            threshold = (
                config.thresholds[index]
                if config is not None and index is not None and index < len(config.thresholds)
                else None
            )
            if index is None or (threshold is None and actions is None):
                continue
            await _release_threshold_actions(
                device_runtime,
                source_id,
                index,
                threshold,
                deps=deps,
                actions=actions,
            )

    for source_id, action in mapping.items():
        config = action.analog_control_config
        if (
            action.action_type == ActionType.ANALOG_CONTROL
            and config is not None
            and config.gamepad_output.enabled
        ):
            _reset_gamepad_output(device_runtime, source_id, config, deps=deps)

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
    device_runtime.state.analog_active_threshold_actions.clear()
    device_runtime.state.analog_mouse_tasks.clear()
    device_runtime.state.analog_mouse_accumulators.clear()
    device_runtime.state.analog_gamepad_outputs.clear()


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
            device_runtime.state.analog_active_threshold_actions.pop(key, None)


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
    threshold_key = _threshold_key(source_id, index)
    executable_actions: list[tuple[int, MappingAction]] = []
    for action_index, action in enumerate(threshold.actions):
        if action.action_type in {
            ActionType.PASSTHROUGH,
            ActionType.SUPPRESS,
            ActionType.ANALOG_CONTROL,
        }:
            continue
        executable_actions.append((action_index, action))
        await runtime_actions.execute_action(
            device_runtime,
            action,
            synthetic,
            _child_event_name(source_id, index, action_index),
            deps=deps,
            shared_output_tracker=lambda bucket, code, value: _track_threshold_output(
                device_runtime,
                bucket,
                code,
                value,
            ),
            shared_abs_output_tracker=lambda bucket, axis_code, value: _track_threshold_abs_output(
                device_runtime,
                bucket,
                axis_code,
                value,
            ),
        )
    device_runtime.state.analog_active_threshold_actions[threshold_key] = tuple(
        executable_actions
    )


async def _release_threshold_actions(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    index: int,
    threshold: AnalogActionThreshold | None,
    *,
    deps: ActionExecutionDeps,
    event_type: int = 0,
    event_code: int = 0,
    actions: tuple[tuple[int, MappingAction], ...] | None = None,
) -> None:
    synthetic = _SyntheticInputEvent(event_type, event_code, 0)
    action_entries = (
        actions
        if actions is not None
        else tuple(enumerate(threshold.actions)) if threshold is not None else ()
    )
    for action_index, action in action_entries:
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
            shared_output_tracker=lambda bucket, code, value: _track_threshold_output(
                device_runtime,
                bucket,
                code,
                value,
            ),
            shared_abs_output_tracker=lambda bucket, axis_code, value: _track_threshold_abs_output(
                device_runtime,
                bucket,
                axis_code,
                value,
            ),
        )


def _track_threshold_output(
    device_runtime: GrabbedDeviceRuntime,
    bucket: str,
    code: int,
    value: int,
) -> bool:
    refcounts = device_runtime.state.analog_threshold_output_refcounts.setdefault(bucket, {})
    held = device_runtime.state.held_output_keys.setdefault(bucket, set())
    current = refcounts.get(int(code), 0)
    if int(value) == 1:
        refcounts[int(code)] = current + 1
        held.add(int(code))
        return current == 0
    if int(value) == 0:
        if current <= 1:
            refcounts.pop(int(code), None)
            held.discard(int(code))
            return current == 1
        refcounts[int(code)] = current - 1
        return False
    return True


def _track_threshold_abs_output(
    device_runtime: GrabbedDeviceRuntime,
    bucket: str,
    axis_code: int,
    value: int,
) -> bool:
    refcounts = device_runtime.state.analog_threshold_abs_refcounts.setdefault(bucket, {})
    held = device_runtime.state.held_output_abs.setdefault(bucket, set())
    current = refcounts.get(int(axis_code), 0)
    if int(value) != 0:
        refcounts[int(axis_code)] = current + 1
        held.add(int(axis_code))
        return current == 0
    if current <= 1:
        refcounts.pop(int(axis_code), None)
        held.discard(int(axis_code))
        return current == 1
    refcounts[int(axis_code)] = current - 1
    return False


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


def _emit_gamepad_output(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    if not config.gamepad_output.enabled:
        return
    if config.input_type == "axis":
        _emit_trigger_gamepad_output(device_runtime, source_id, config, deps=deps)
        return
    _emit_stick_gamepad_output(device_runtime, source_id, config, deps=deps)


def _gamepad_output_direction(config: AnalogControlConfig) -> str:
    direction = str(config.gamepad_output.output_direction or "").lower()
    if direction in {"min", "max", "both"}:
        return direction
    return "min" if config.gamepad_output.output_invert else "max"


def _emit_stick_gamepad_output(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    if config.gamepad_output.target == "analog":
        _emit_analog_stick_output(device_runtime, source_id, config, deps=deps)
        return
    axis_codes = _stick_output_axis_codes(device_runtime, source_id, config, deps=deps)
    if axis_codes is None:
        return
    axis_values = device_runtime.state.analog_axis_values.get(source_id, {})
    x = float(axis_values.get("x", 0.0))
    y = float(axis_values.get("y", 0.0))
    x, y = _apply_stick_output_curve(
        x,
        y,
        deadzone=float(config.gamepad_output.deadzone),
        sensitivity=float(config.gamepad_output.sensitivity),
        response_curve=float(config.gamepad_output.response_curve),
    )
    _write_gamepad_axes(
        device_runtime,
        source_id,
        config,
        (
            (axis_codes[0], _stick_value_to_raw(x)),
            (axis_codes[1], _stick_value_to_raw(y)),
        ),
        deps=deps,
    )


def _emit_trigger_gamepad_output(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    if config.gamepad_output.target == "analog":
        _emit_analog_axis_output(device_runtime, source_id, config, deps=deps)
        return
    axis_code = _trigger_output_axis_code(device_runtime, source_id, config, deps=deps)
    if axis_code is None:
        return
    axis_values = device_runtime.state.analog_axis_values.get(source_id, {})
    if _gamepad_output_direction(config) == "both":
        value = float(axis_values.get("x_signed", 0.0))
        value = _apply_signed_axis_output_curve(
            value,
            deadzone=float(config.gamepad_output.deadzone),
            sensitivity=float(config.gamepad_output.sensitivity),
            response_curve=float(config.gamepad_output.response_curve),
        )
        raw_value = denormalize_axis_value(
            value,
            DEFAULT_TRIGGER_MIN,
            DEFAULT_TRIGGER_MAX,
            center=(
                config.gamepad_output.output_rest
                if config.gamepad_output.output_rest is not None
                else DEFAULT_TRIGGER_MIN
            ),
        )
    else:
        value = float(axis_values.get("x", 0.0))
        value = _apply_control_axis_output_curve(
            value,
            deadzone=float(config.gamepad_output.deadzone),
            sensitivity=float(config.gamepad_output.sensitivity),
            response_curve=float(config.gamepad_output.response_curve),
        )
        raw_value = denormalize_control_axis_value(
            value,
            DEFAULT_TRIGGER_MIN,
            DEFAULT_TRIGGER_MAX,
            rest=config.gamepad_output.output_rest,
            invert=_gamepad_output_direction(config) == "min",
        )
    _write_gamepad_axes(
        device_runtime,
        source_id,
        config,
        ((axis_code, raw_value),),
        reset_axes=(
            (
                axis_code,
                config.gamepad_output.output_rest
                if config.gamepad_output.output_rest is not None
                else DEFAULT_TRIGGER_MIN,
            ),
        ),
        deps=deps,
    )


def _reset_gamepad_output(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    if config.gamepad_output.target == "analog":
        _reset_analog_gamepad_output(device_runtime, source_id, config, deps=deps)
        return
    if config.input_type == "axis":
        axis_code = _trigger_output_axis_code(device_runtime, source_id, config, deps=deps)
        if axis_code is None:
            return
        axes = (
            (
                axis_code,
                config.gamepad_output.output_rest
                if config.gamepad_output.output_rest is not None
                else 0,
            ),
        )
    else:
        axis_codes = _stick_output_axis_codes(device_runtime, source_id, config, deps=deps)
        if axis_codes is None:
            return
        axes = (
            (axis_codes[0], 0),
            (axis_codes[1], 0),
        )
    _write_gamepad_axes(
        device_runtime,
        source_id,
        config,
        axes,
        reset_axes=axes,
        releasing=True,
        deps=deps,
    )


def _emit_analog_axis_output(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    target = _resolve_gamepad_output_target(device_runtime, source_id, config)
    if target is None:
        return
    analog = _target_analog_input(target, config, expected_type="axis")
    axis = _target_axis(analog, "x") if analog is not None else None
    if axis is None:
        return
    axis_values = device_runtime.state.analog_axis_values.get(source_id, {})
    axis_code = _axis_code(axis)
    if axis_code is None:
        return
    minimum, maximum = _axis_min_max(axis, DEFAULT_TRIGGER_MIN, DEFAULT_TRIGGER_MAX)
    output_rest = (
        config.gamepad_output.output_rest
        if config.gamepad_output.output_rest is not None
        else _axis_int(axis, "rest")
    )
    reset_value = output_rest if output_rest is not None else (minimum if minimum >= 0 else 0)
    if _gamepad_output_direction(config) == "both":
        value = float(axis_values.get("x_signed", 0.0))
        value = _apply_signed_axis_output_curve(
            value,
            deadzone=float(config.gamepad_output.deadzone),
            sensitivity=float(config.gamepad_output.sensitivity),
            response_curve=float(config.gamepad_output.response_curve),
        )
        raw_value = denormalize_axis_value(
            value,
            minimum,
            maximum,
            center=reset_value,
        )
    else:
        value = float(axis_values.get("x", 0.0))
        value = _apply_control_axis_output_curve(
            value,
            deadzone=float(config.gamepad_output.deadzone),
            sensitivity=float(config.gamepad_output.sensitivity),
            response_curve=float(config.gamepad_output.response_curve),
        )
        raw_value = denormalize_control_axis_value(
            value,
            minimum,
            maximum,
            rest=output_rest,
            invert=_gamepad_output_direction(config) == "min",
        )
    _write_gamepad_axes(
        device_runtime,
        source_id,
        config,
        ((axis_code, raw_value),),
        reset_axes=((axis_code, reset_value),),
        deps=deps,
        target=target,
    )


def _emit_analog_stick_output(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    target = _resolve_gamepad_output_target(device_runtime, source_id, config)
    if target is None:
        return
    analog = _target_analog_input(target, config, expected_type="stick")
    if analog is None:
        return
    axes: list[tuple[int, int]] = []
    reset_axes: list[tuple[int, int]] = []
    axis_values = device_runtime.state.analog_axis_values.get(source_id, {})
    x = float(axis_values.get("x", 0.0))
    y = float(axis_values.get("y", 0.0))
    x, y = _apply_stick_output_curve(
        x,
        y,
        deadzone=float(config.gamepad_output.deadzone),
        sensitivity=float(config.gamepad_output.sensitivity),
        response_curve=float(config.gamepad_output.response_curve),
    )
    for role, normalized in (("x", x), ("y", y)):
        axis = _target_axis(analog, role)
        if axis is None:
            return
        axis_code = _axis_code(axis)
        if axis_code is None:
            return
        minimum, maximum = _axis_min_max(axis, DEFAULT_STICK_MIN, DEFAULT_STICK_MAX)
        reset_value = _axis_int(axis, "center") or 0
        axes.append(
            (
                axis_code,
                denormalize_axis_value(
                    normalized,
                    minimum,
                    maximum,
                    center=_axis_int(axis, "center"),
                    invert=bool(axis.get("invert", False)),
                ),
            )
        )
        reset_axes.append((axis_code, reset_value))
    _write_gamepad_axes(
        device_runtime,
        source_id,
        config,
        tuple(axes),
        reset_axes=tuple(reset_axes),
        deps=deps,
        target=target,
    )


def _reset_analog_gamepad_output(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    target = _resolve_gamepad_output_target(device_runtime, source_id, config)
    if target is None:
        return
    analog = _target_analog_input(target, config, expected_type=config.input_type)
    if analog is None:
        return
    axes: list[tuple[int, int]] = []
    for axis in _target_axes(analog):
        axis_code = _axis_code(axis)
        if axis_code is None:
            continue
        if config.input_type == "axis":
            axes.append(
                (
                    axis_code,
                    config.gamepad_output.output_rest
                    if config.gamepad_output.output_rest is not None
                    else _axis_int(axis, "rest")
                    or DEFAULT_TRIGGER_MIN,
                )
            )
        else:
            axes.append((axis_code, _axis_int(axis, "center") or 0))
    _write_gamepad_axes(
        device_runtime,
        source_id,
        config,
        tuple(axes),
        reset_axes=tuple(axes),
        releasing=True,
        deps=deps,
        target=target,
    )


def _write_gamepad_axes(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    config: AnalogControlConfig,
    axes: tuple[tuple[int, int], ...],
    *,
    reset_axes: tuple[tuple[int, int], ...] | None = None,
    releasing: bool = False,
    deps: ActionExecutionDeps,
    target: object | None = None,
) -> None:
    if not axes:
        return
    target = target or _resolve_gamepad_output_target(device_runtime, source_id, config)
    if target is None:
        return
    target_uinput = getattr(target, "uinput", None)
    target_bucket = str(getattr(target, "bucket", "gamepad"))
    writer = deps.uinput_writer(target_uinput)
    if writer is None:
        return
    for axis_code, value in axes:
        writer.write(deps.evdev_mod.ecodes.EV_ABS, int(axis_code), int(value))
        if releasing:
            _clear_tracked_abs_state(device_runtime, target_bucket, int(axis_code))
        else:
            track_abs_state(device_runtime, int(axis_code), int(value), bucket=target_bucket)
    writer.syn()
    device_runtime.state.analog_gamepad_outputs[source_id] = AnalogGamepadOutputState(
        output_id=config.gamepad_output.output_id,
        reset_axes=(
            tuple((int(axis_code), int(value)) for axis_code, value in reset_axes)
            if reset_axes is not None
            else tuple((int(axis_code), 0) for axis_code, _value in axes)
        ),
    )


def _resolve_gamepad_output_target(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    config: AnalogControlConfig,
) -> object | None:
    return device_runtime.resolve_gamepad_output(
        config.gamepad_output.output_id,
        f"{source_id} analog output",
    )


def _target_analog_input(
    target: object,
    config: AnalogControlConfig,
    *,
    expected_type: str,
) -> dict[str, object] | None:
    target_analog_id = config.gamepad_output.target_analog_id
    if not target_analog_id:
        return None
    analog_inputs = getattr(target, "analog_inputs", None)
    if not isinstance(analog_inputs, dict):
        return None
    typed_analog_inputs = cast(dict[str, object], analog_inputs)
    raw_analog = typed_analog_inputs.get(target_analog_id)
    if not isinstance(raw_analog, dict):
        return None
    analog = cast(dict[str, object], raw_analog)
    if str(analog.get("type", "") or "") != expected_type:
        return None
    return analog


def _target_axes(analog: dict[str, object]) -> list[dict[str, object]]:
    raw_axes = analog.get("axes")
    if not isinstance(raw_axes, list):
        return []
    axes = cast(list[object], raw_axes)
    return [cast(dict[str, object], axis) for axis in axes if isinstance(axis, dict)]


def _target_axis(analog: dict[str, object], role: str) -> dict[str, object] | None:
    for axis in _target_axes(analog):
        if str(axis.get("role", "") or "") == role:
            return axis
    return None


def _axis_int(axis: dict[str, object], key: str) -> int | None:
    value = axis.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            return None
    return None


def _axis_code(axis: dict[str, object]) -> int | None:
    code = _axis_int(axis, "evdev_code")
    if code is not None:
        return code
    evdev_name = str(axis.get("evdev", "") or "")
    return resolve_evdev_code(evdev_name)


def _axis_min_max(
    axis: dict[str, object],
    fallback_minimum: int,
    fallback_maximum: int,
) -> tuple[int, int]:
    minimum = _axis_int(axis, "minimum")
    maximum = _axis_int(axis, "maximum")
    if minimum is None or maximum is None or minimum >= maximum:
        return fallback_minimum, fallback_maximum
    return minimum, maximum


def _reset_recorded_gamepad_outputs(
    device_runtime: GrabbedDeviceRuntime,
    *,
    deps: ActionExecutionDeps,
) -> None:
    for source_id, output in list(device_runtime.state.analog_gamepad_outputs.items()):
        _write_recorded_gamepad_reset(device_runtime, source_id, output, deps=deps)


def _write_recorded_gamepad_reset(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    output: AnalogGamepadOutputState,
    *,
    deps: ActionExecutionDeps,
) -> None:
    target = device_runtime.resolve_gamepad_output(
        output.output_id,
        f"{source_id} analog output reset",
    )
    if target is None:
        return
    target_uinput = getattr(target, "uinput", None)
    target_bucket = str(getattr(target, "bucket", "gamepad"))
    writer = deps.uinput_writer(target_uinput)
    if writer is None:
        return
    for axis_code, value in output.reset_axes:
        writer.write(deps.evdev_mod.ecodes.EV_ABS, int(axis_code), int(value))
        _clear_tracked_abs_state(device_runtime, target_bucket, int(axis_code))
    writer.syn()


def _clear_tracked_abs_state(
    device_runtime: GrabbedDeviceRuntime,
    bucket: str,
    axis_code: int,
) -> None:
    held = device_runtime.state.held_output_abs.get(bucket)
    if held is not None:
        held.discard(int(axis_code))


def _gamepad_output_stick_id(source_id: str, config: AnalogControlConfig) -> str:
    if config.gamepad_output.target == "left":
        return "left_stick"
    if config.gamepad_output.target == "right":
        return "right_stick"
    return source_id


def _stick_output_axis_codes(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> tuple[int, int] | None:
    if config.gamepad_output.target == "same":
        axis_codes = device_runtime.analog_axis_output_codes
        x_code = axis_codes.get((source_id, "x"))
        y_code = axis_codes.get((source_id, "y"))
        if x_code is not None and y_code is not None:
            return int(x_code), int(y_code)

    axis_names = STICK_OUTPUT_AXES.get(_gamepad_output_stick_id(source_id, config))
    if axis_names is None:
        return None
    return (
        int(getattr(deps.evdev_mod.ecodes, axis_names[0])),
        int(getattr(deps.evdev_mod.ecodes, axis_names[1])),
    )


def _gamepad_output_trigger_id(source_id: str, config: AnalogControlConfig) -> str:
    if config.gamepad_output.target == "left":
        return "left_trigger"
    if config.gamepad_output.target == "right":
        return "right_trigger"
    return source_id


def _trigger_output_axis_code(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> int | None:
    if config.gamepad_output.target == "same":
        trigger_id = _same_trigger_output_id(device_runtime, source_id, deps=deps)
        if trigger_id is not None:
            return int(getattr(deps.evdev_mod.ecodes, TRIGGER_OUTPUT_AXES[trigger_id]))
        axis_code = device_runtime.analog_axis_output_codes.get((source_id, "x"))
        if axis_code is not None:
            return int(axis_code)

    axis_name = TRIGGER_OUTPUT_AXES.get(_gamepad_output_trigger_id(source_id, config))
    if axis_name is None:
        return None
    return int(getattr(deps.evdev_mod.ecodes, axis_name))


def _same_trigger_output_id(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    *,
    deps: ActionExecutionDeps,
) -> str | None:
    if source_id in TRIGGER_OUTPUT_AXES:
        return source_id

    axis_code = device_runtime.analog_axis_output_codes.get((source_id, "x"))
    if axis_code == int(deps.evdev_mod.ecodes.ABS_Z):
        return "left_trigger"
    if axis_code == int(deps.evdev_mod.ecodes.ABS_RZ):
        return "right_trigger"

    input_data = device_runtime.analog_inputs.get(source_id)
    label = ""
    if isinstance(input_data, dict):
        input_metadata = cast(dict[str, object], input_data)
        label_value = input_metadata.get("label")
        label = str(label_value or "")
    text = f"{source_id} {label}".lower().replace("-", "_").replace(" ", "_")
    if (
        "left_trigger" in text
        or text.endswith("_lt")
        or "_lt_" in text
        or "l2" in text
    ):
        return "left_trigger"
    if (
        "right_trigger" in text
        or text.endswith("_rt")
        or "_rt_" in text
        or "r2" in text
    ):
        return "right_trigger"
    return None


def _apply_stick_output_curve(
    x: float,
    y: float,
    *,
    deadzone: float,
    sensitivity: float,
    response_curve: float,
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
    return direction_x * scaled, direction_y * scaled


def _apply_control_axis_output_curve(
    value: float,
    *,
    deadzone: float,
    sensitivity: float,
    response_curve: float,
) -> float:
    return analog_gamepad_output_distance(
        max(0.0, min(1.0, value)),
        deadzone=deadzone,
        sensitivity=sensitivity,
        response_curve=response_curve,
    )


def _apply_signed_axis_output_curve(
    value: float,
    *,
    deadzone: float,
    sensitivity: float,
    response_curve: float,
) -> float:
    value = max(-1.0, min(1.0, value))
    scaled = _apply_control_axis_output_curve(
        abs(value),
        deadzone=deadzone,
        sensitivity=sensitivity,
        response_curve=response_curve,
    )
    return math.copysign(scaled, value)


def _stick_value_to_raw(value: float) -> int:
    value = max(-1.0, min(1.0, value))
    if value >= 0.0:
        return min(DEFAULT_STICK_MAX, int(round(value * DEFAULT_STICK_MAX)))
    return max(DEFAULT_STICK_MIN, int(round(value * abs(DEFAULT_STICK_MIN))))
