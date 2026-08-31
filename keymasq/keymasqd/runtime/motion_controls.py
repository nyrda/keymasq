"""Frame-based normalized motion input processing."""

import math
import time

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.analog import (
    AnalogControlConfig,
    AnalogGamepadOutputConfig,
)
from keymasq.common.model.core import ActionType
from keymasq.common.model.motion import MotionAxisRoutingConfig, MotionControlConfig
from keymasq.keymasqd.output_helpers import emit_mouse_move
from keymasq.keymasqd.runtime.adapters import identity_uinput_writer
from keymasq.keymasqd.runtime.analog.gamepad import emit_gamepad_output
from keymasq.keymasqd.runtime.grabbed_device.types import (
    ActionExecutionDeps,
    GrabbedDeviceRuntime,
    InputEventLike,
)


async def dispatch_motion_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    mapping: dict[str, MappingAction],
    *,
    deps: ActionExecutionDeps,
) -> bool:
    binding = device_runtime.motion_axis_bindings.get((int(event.type), int(event.code)))
    if binding is not None:
        sensor_id, kind, role, offset, scale, invert, noise = binding
        value = (float(event.value) - offset) * scale
        if invert:
            value = -value
        if abs(value) < noise:
            value = 0.0
        sensor_frame = device_runtime.state.motion_frame_values.setdefault(sensor_id, {})
        sensor_frame.setdefault(kind, {})[role] = value
        return _motion_action_consumes(mapping.get(sensor_id))

    if int(event.type) != int(deps.evdev_mod.ecodes.EV_SYN):
        return False
    if int(event.code) == 3:  # SYN_DROPPED
        device_runtime.state.motion_frame_values.clear()
        device_runtime.state.motion_smoothed_values.clear()
        device_runtime.state.motion_last_frame_ns.clear()
        device_runtime.state.motion_mouse_accumulators.clear()
        await device_runtime.reset_analog_controls()
        return bool(device_runtime.motion_axis_bindings)
    if int(event.code) != 0:  # SYN_REPORT
        return False

    consumed = False
    now_ns = time.monotonic_ns()
    for sensor_id in list(device_runtime.state.motion_frame_values):
        action = mapping.get(sensor_id)
        if not _motion_action_consumes(action):
            continue
        consumed = True
        if action is None or action.action_type != ActionType.MOTION_CONTROL:
            continue
        config = action.motion_control_config
        if config is not None:
            _emit_motion_control(
                device_runtime,
                sensor_id,
                config,
                now_ns,
                deps=deps,
            )
    return consumed


def _motion_action_consumes(action: MappingAction | None) -> bool:
    return action is not None and action.action_type != ActionType.PASSTHROUGH


def _emit_motion_control(
    device_runtime: GrabbedDeviceRuntime,
    sensor_id: str,
    config: MotionControlConfig,
    now_ns: int,
    *,
    deps: ActionExecutionDeps,
) -> None:
    gyro = device_runtime.state.motion_frame_values.get(sensor_id, {}).get("gyro", {})
    if not gyro:
        return
    state_key = f"motion:{sensor_id}"
    last_ns = device_runtime.state.motion_last_frame_ns.get(state_key, now_ns)
    device_runtime.state.motion_last_frame_ns[state_key] = now_ns
    dt = min(0.1, max(0.0, (now_ns - last_ns) / 1_000_000_000.0))
    raw_x, raw_y = _routed_gyro_rates(gyro, config.axis_routing)

    if config.mode == "mouse":
        x, y = _filtered_axes(
            device_runtime,
            state_key,
            raw_x,
            raw_y,
            config.mouse.smoothing,
        )
        dx = _rate_curve(x, config.mouse.deadzone_dps, config.mouse.response_curve)
        dy = _rate_curve(y, config.mouse.deadzone_dps, config.mouse.response_curve)
        if config.mouse.invert_x:
            dx = -dx
        if config.mouse.invert_y:
            dy = -dy
        _emit_mouse(
            device_runtime,
            state_key,
            dx * config.mouse.sensitivity_x * dt,
            dy * config.mouse.sensitivity_y * dt,
        )
        return

    x, y = _filtered_axes(
        device_runtime,
        state_key,
        raw_x,
        raw_y,
        config.gamepad.smoothing,
    )
    x = _normalized_rate(
        x,
        config.gamepad.deadzone_dps,
        config.gamepad.max_rate_dps,
        config.gamepad.response_curve,
    )
    y = _normalized_rate(
        y,
        config.gamepad.deadzone_dps,
        config.gamepad.max_rate_dps,
        config.gamepad.response_curve,
    )
    if config.gamepad.invert_x:
        x = -x
    if config.gamepad.invert_y:
        y = -y
    device_runtime.state.analog_axis_values[state_key] = {"x": x, "y": y}
    analog_config = AnalogControlConfig(
        name=config.name,
        input_type="stick",
        gamepad_output=AnalogGamepadOutputConfig(
            enabled=True,
            output_id=config.gamepad.output_id,
            target=config.gamepad.target,
            target_analog_id=config.gamepad.target_analog_id,
        ),
    )
    emit_gamepad_output(device_runtime, state_key, sensor_id, analog_config, deps=deps)


def _routed_gyro_rates(
    gyro: dict[str, float],
    routing: MotionAxisRoutingConfig,
) -> tuple[float, float]:
    horizontal = 0.0
    vertical = 0.0
    for axis, output in (
        ("yaw", routing.yaw),
        ("pitch", routing.pitch),
        ("roll", routing.roll),
    ):
        rate = -math.degrees(float(gyro.get(axis, 0.0)))
        if output == "horizontal":
            horizontal += rate
        elif output == "vertical":
            vertical += rate
    return horizontal, vertical


def _filtered_axes(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    x: float,
    y: float,
    smoothing: float,
) -> tuple[float, float]:
    previous = device_runtime.state.motion_smoothed_values.get(state_key, {})
    smoothed_x = x * (1.0 - smoothing) + float(previous.get("x", x)) * smoothing
    smoothed_y = y * (1.0 - smoothing) + float(previous.get("y", y)) * smoothing
    device_runtime.state.motion_smoothed_values[state_key] = {
        "x": smoothed_x,
        "y": smoothed_y,
    }
    return smoothed_x, smoothed_y


def _rate_curve(value: float, deadzone: float, curve: float) -> float:
    magnitude = abs(value)
    if magnitude <= deadzone:
        return 0.0
    adjusted = magnitude - deadzone
    curved = math.pow(adjusted / 180.0, curve) * 180.0
    return math.copysign(curved, value)


def _normalized_rate(value: float, deadzone: float, maximum: float, curve: float) -> float:
    magnitude = abs(value)
    if magnitude <= deadzone:
        return 0.0
    span = max(1.0, maximum - deadzone)
    normalized = min(1.0, (magnitude - deadzone) / span)
    return math.copysign(math.pow(normalized, curve), value)


def _emit_mouse(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    dx: float,
    dy: float,
) -> None:
    old_x, old_y = device_runtime.state.motion_mouse_accumulators.get(state_key, (0.0, 0.0))
    total_x = old_x + dx
    total_y = old_y + dy
    move_x = math.trunc(total_x)
    move_y = math.trunc(total_y)
    device_runtime.state.motion_mouse_accumulators[state_key] = (
        total_x - move_x,
        total_y - move_y,
    )
    if move_x or move_y:
        emit_mouse_move(identity_uinput_writer(device_runtime.mouse_uinput), move_x, move_y)
