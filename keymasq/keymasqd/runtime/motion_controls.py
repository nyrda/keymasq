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
from keymasq.keymasqd.runtime.analog_controls import process_normalized_analog_values
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
    if device_runtime.state.motion_resyncing:
        if (
            int(event.type) == int(deps.evdev_mod.ecodes.EV_SYN)
            and int(event.code) == 0  # SYN_REPORT ends the incomplete frame.
        ):
            initialize_motion_state(device_runtime, mapping)
        return bool(device_runtime.motion_axis_bindings)

    binding = device_runtime.motion_axis_bindings.get((int(event.type), int(event.code)))
    if binding is not None:
        _record_motion_value(device_runtime, binding, float(event.value))
        return _motion_action_consumes(mapping.get(binding[0]))

    if int(event.type) != int(deps.evdev_mod.ecodes.EV_SYN):
        return False
    if int(event.code) == int(deps.evdev_mod.ecodes.SYN_DROPPED):
        device_runtime.reset_motion_controls()
        device_runtime.state.motion_resyncing = True
        await device_runtime.reset_analog_controls(state_key_prefix="motion:")
        return bool(device_runtime.motion_axis_bindings)
    if int(event.code) != 0:  # SYN_REPORT
        return False

    consumed = False
    now_ns = _motion_timestamp_ns(event)
    for sensor_id in list(device_runtime.state.motion_frame_values):
        action = mapping.get(sensor_id)
        if not _motion_action_consumes(action):
            continue
        consumed = True
        if action is None or action.action_type != ActionType.MOTION_CONTROL:
            continue
        configs = _action_motion_control_configs(action)
        for index, config in enumerate(configs):
            await _emit_motion_control(
                device_runtime,
                sensor_id,
                _motion_control_state_key(sensor_id, index, len(configs)),
                config,
                now_ns,
                event,
                source_profile_name=action.source_profile_name,
                deps=deps,
            )
    return consumed


def _motion_timestamp_ns(event: InputEventLike) -> int:
    sec = getattr(event, "sec", None)
    usec = getattr(event, "usec", None)
    if isinstance(sec, int) and isinstance(usec, int):
        timestamp = sec * 1_000_000_000 + usec * 1_000
        if timestamp > 0:
            return timestamp
    # Synthetic events have no evdev timestamp.
    return time.monotonic_ns()


def _record_motion_value(
    device_runtime: GrabbedDeviceRuntime,
    binding: tuple[str, str, str, float, float, bool, float],
    raw_value: float,
) -> None:
    sensor_id, kind, role, offset, scale, invert, noise = binding
    value = (raw_value - offset) * scale
    if invert:
        value = -value
    if abs(value) < noise:
        value = 0.0
    sensor_frame = device_runtime.state.motion_frame_values.setdefault(sensor_id, {})
    sensor_frame.setdefault(kind, {})[role] = value


def _resync_motion_values(device_runtime: GrabbedDeviceRuntime) -> bool:
    device = device_runtime.device
    if device is None:
        return False
    device_runtime.state.motion_frame_values.clear()
    for (_event_type, code), binding in device_runtime.motion_axis_bindings.items():
        try:
            value = getattr(device.absinfo(code), "value", None)
        except (OSError, KeyError):
            return False
        if not isinstance(value, int):
            return False
        _record_motion_value(device_runtime, binding, float(value))
    return True


def initialize_motion_state(
    device_runtime: GrabbedDeviceRuntime,
    mapping: dict[str, MappingAction],
) -> None:
    """Read unchanged axes and capture the activation pose before processing events."""
    if not device_runtime.motion_axis_bindings or device_runtime.device is None:
        return
    device_runtime.state.motion_resyncing = not _resync_motion_values(device_runtime)
    if device_runtime.state.motion_resyncing:
        return
    for sensor_id, frame in device_runtime.state.motion_frame_values.items():
        action = mapping.get(sensor_id)
        if action is None:
            continue
        angles = _accelerometer_angles(frame.get("accelerometer", {}))
        if angles is None:
            continue
        pitch, roll = angles
        configs = _action_motion_control_configs(action)
        for index, config in enumerate(configs):
            state_key = _motion_control_state_key(sensor_id, index, len(configs))
            if config.mode == "analog":
                if config.analog.source != "tilt" or config.analog.reference != "activation":
                    continue
                signals = {"yaw": 0.0, "pitch": -pitch, "roll": -roll}
                center = (
                    _selected_motion_signal(signals, config.analog.x_axis),
                    _selected_motion_signal(signals, config.analog.y_axis),
                )
            elif config.mode in {"tilt_mouse", "tilt_gamepad", "area_mouse"}:
                if config.tilt.reference != "activation":
                    continue
                center = _routed_tilt_angles(pitch, roll, config)
            else:
                continue
            device_runtime.state.motion_tilt_centers[state_key] = center


def _motion_action_consumes(action: MappingAction | None) -> bool:
    return action is not None and action.action_type != ActionType.PASSTHROUGH


def _action_motion_control_configs(action: MappingAction) -> list[MotionControlConfig]:
    if action.action_type != ActionType.MOTION_CONTROL:
        return []
    if action.motion_control_configs:
        return list(action.motion_control_configs)
    if action.motion_control_config is not None:
        return [action.motion_control_config]
    return []


def _motion_control_state_key(sensor_id: str, index: int, total: int) -> str:
    return f"motion:{sensor_id}" if total == 1 else f"motion:{sensor_id}:control:{index}"


async def _emit_motion_control(
    device_runtime: GrabbedDeviceRuntime,
    sensor_id: str,
    state_key: str,
    config: MotionControlConfig,
    now_ns: int,
    event: InputEventLike,
    *,
    source_profile_name: str | None,
    deps: ActionExecutionDeps,
) -> None:
    frame = device_runtime.state.motion_frame_values.get(sensor_id, {})
    last_ns = device_runtime.state.motion_last_frame_ns.get(state_key, now_ns)
    device_runtime.state.motion_last_frame_ns[state_key] = now_ns
    dt = min(0.1, max(0.0, (now_ns - last_ns) / 1_000_000_000.0))

    if config.mode in {"mouse", "gamepad"}:
        gyro = frame.get("gyro", {})
        if not gyro:
            return
        raw_x, raw_y = _routed_gyro_rates(gyro, config.axis_routing)
        _emit_gyro_control(
            device_runtime,
            sensor_id,
            state_key,
            config,
            raw_x,
            raw_y,
            dt,
            deps=deps,
        )
        return

    if config.mode == "analog":
        await _emit_motion_analog(
            device_runtime,
            sensor_id,
            state_key,
            config,
            frame,
            event,
            source_profile_name=source_profile_name,
            deps=deps,
        )
        return

    accelerometer = frame.get("accelerometer", {})
    angles = _accelerometer_angles(accelerometer)
    if angles is None:
        return
    pitch, roll = angles
    absolute_x, absolute_y = _routed_tilt_angles(pitch, roll, config)
    center = device_runtime.state.motion_tilt_centers.get(state_key)
    if center is None:
        center = (absolute_x, absolute_y) if config.tilt.reference == "activation" else (0.0, 0.0)
        device_runtime.state.motion_tilt_centers[state_key] = center
    raw_x = absolute_x - center[0]
    raw_y = absolute_y - center[1]
    if config.mode == "area_mouse" and config.tilt.drag_center:
        center, raw_x, raw_y = _drag_tilt_center(
            center,
            raw_x,
            raw_y,
            config.tilt.full_scale_deg,
        )
        device_runtime.state.motion_tilt_centers[state_key] = center
    x, y = _filtered_axes(
        device_runtime,
        state_key,
        raw_x,
        raw_y,
        config.tilt.smoothing,
    )
    x, y = _normalized_tilt(
        x,
        y,
        config.tilt.deadzone_deg,
        config.tilt.full_scale_deg,
        config.tilt.response_curve,
    )
    if config.tilt.invert_x:
        x = -x
    if config.tilt.invert_y:
        y = -y

    if config.mode == "tilt_mouse":
        _emit_mouse(
            device_runtime,
            state_key,
            x * config.tilt.speed_x * dt,
            y * config.tilt.speed_y * dt,
        )
    elif config.mode == "area_mouse":
        _emit_mouse_area(
            device_runtime,
            state_key,
            x * config.tilt.area_radius_x,
            y * config.tilt.area_radius_y,
        )
    else:
        _emit_gamepad_axes(
            device_runtime,
            sensor_id,
            state_key,
            config,
            x,
            y,
            deps=deps,
        )


async def _emit_motion_analog(
    device_runtime: GrabbedDeviceRuntime,
    sensor_id: str,
    state_key: str,
    config: MotionControlConfig,
    frame: dict[str, dict[str, float]],
    event: InputEventLike,
    *,
    source_profile_name: str | None,
    deps: ActionExecutionDeps,
) -> None:
    analog_control = config.analog.analog_control_config
    if analog_control is None:
        return

    if config.analog.source == "gyro":
        gyro = frame.get("gyro", {})
        if not gyro:
            return
        signals = {
            axis: -math.degrees(float(gyro.get(axis, 0.0))) for axis in ("yaw", "pitch", "roll")
        }
        maximum = config.analog.full_scale_dps
    else:
        angles = _accelerometer_angles(frame.get("accelerometer", {}))
        if angles is None:
            return
        pitch, roll = angles
        signals = {"yaw": 0.0, "pitch": -pitch, "roll": -roll}
        center = device_runtime.state.motion_tilt_centers.get(state_key)
        absolute = (
            _selected_motion_signal(signals, config.analog.x_axis),
            _selected_motion_signal(signals, config.analog.y_axis),
        )
        if center is None:
            center = absolute if config.analog.reference == "activation" else (0.0, 0.0)
            device_runtime.state.motion_tilt_centers[state_key] = center
        signals = {
            config.analog.x_axis: absolute[0] - center[0],
            config.analog.y_axis: absolute[1] - center[1],
        }
        maximum = config.analog.full_scale_deg

    raw_x = _selected_motion_signal(signals, config.analog.x_axis)
    raw_y = _selected_motion_signal(signals, config.analog.y_axis)
    x, y = _filtered_axes(
        device_runtime,
        state_key,
        raw_x,
        raw_y,
        config.analog.smoothing,
    )
    x = max(-1.0, min(1.0, x / maximum))
    y = max(-1.0, min(1.0, y / maximum))
    if config.analog.invert_x:
        x = -x
    if config.analog.invert_y:
        y = -y
    await process_normalized_analog_values(
        device_runtime,
        state_key,
        sensor_id,
        analog_control,
        event,
        x,
        y,
        source_profile_name=source_profile_name,
        deps=deps,
    )


def _selected_motion_signal(signals: dict[str, float], axis: str) -> float:
    return 0.0 if axis == "none" else float(signals.get(axis, 0.0))


def _emit_gyro_control(
    device_runtime: GrabbedDeviceRuntime,
    sensor_id: str,
    state_key: str,
    config: MotionControlConfig,
    raw_x: float,
    raw_y: float,
    dt: float,
    *,
    deps: ActionExecutionDeps,
) -> None:
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
        neutral_deadzone=config.gamepad.deadzone_dps,
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
    _emit_gamepad_axes(
        device_runtime,
        sensor_id,
        state_key,
        config,
        x,
        y,
        deps=deps,
    )


def _emit_gamepad_axes(
    device_runtime: GrabbedDeviceRuntime,
    sensor_id: str,
    state_key: str,
    config: MotionControlConfig,
    x: float,
    y: float,
    *,
    deps: ActionExecutionDeps,
) -> None:
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
    emit_gamepad_output(
        device_runtime,
        state_key,
        sensor_id,
        analog_config,
        gyro=config.mode == "gamepad",
        minimum_output=config.gamepad.minimum_output,
        deps=deps,
    )


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


def _accelerometer_angles(
    accelerometer: dict[str, float],
) -> tuple[float, float] | None:
    if not {"x", "y", "z"}.issubset(accelerometer):
        return None
    x = float(accelerometer["x"])
    y = float(accelerometer["y"])
    z = float(accelerometer["z"])
    if math.sqrt(x * x + y * y + z * z) < 0.001:
        return None
    pitch = math.degrees(math.atan2(y, math.hypot(x, z)))
    roll = math.degrees(math.atan2(x, math.hypot(y, z)))
    return pitch, roll


def _routed_tilt_angles(
    pitch: float,
    roll: float,
    config: MotionControlConfig,
) -> tuple[float, float]:
    horizontal = 0.0
    vertical = 0.0
    for angle, output in (
        (-pitch, config.tilt.pitch),
        (-roll, config.tilt.roll),
    ):
        if output == "horizontal":
            horizontal += angle
        elif output == "vertical":
            vertical += angle
    return horizontal, vertical


def _drag_tilt_center(
    center: tuple[float, float],
    x: float,
    y: float,
    maximum: float,
) -> tuple[tuple[float, float], float, float]:
    center_x, center_y = center
    if abs(x) > maximum:
        overflow = abs(x) - maximum
        shift = math.copysign(overflow, x)
        center_x += shift
        x -= shift
    if abs(y) > maximum:
        overflow = abs(y) - maximum
        shift = math.copysign(overflow, y)
        center_y += shift
        y -= shift
    return (center_x, center_y), x, y


def _filtered_axes(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    x: float,
    y: float,
    smoothing: float,
    *,
    neutral_deadzone: float | None = None,
) -> tuple[float, float]:
    previous = device_runtime.state.motion_smoothed_values.get(state_key, {})
    smoothed_x = x * (1.0 - smoothing) + float(previous.get("x", x)) * smoothing
    smoothed_y = y * (1.0 - smoothing) + float(previous.get("y", y)) * smoothing
    # Minimum stick output must not amplify a smoothing tail after input stops.
    if neutral_deadzone is not None:
        if abs(x) <= neutral_deadzone:
            smoothed_x = 0.0
        if abs(y) <= neutral_deadzone:
            smoothed_y = 0.0
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


def _normalized_tilt(
    x: float,
    y: float,
    deadzone: float,
    maximum: float,
    curve: float,
) -> tuple[float, float]:
    magnitude = math.hypot(x, y)
    if magnitude <= deadzone or magnitude <= 0.0:
        return 0.0, 0.0
    span = max(0.1, maximum - deadzone)
    normalized = min(1.0, (magnitude - deadzone) / span)
    scaled = math.pow(normalized, curve)
    return x / magnitude * scaled, y / magnitude * scaled


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


def _emit_mouse_area(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    target_x: float,
    target_y: float,
) -> None:
    old_x, old_y = device_runtime.state.motion_mouse_area_offsets.get(
        state_key,
        (0.0, 0.0),
    )
    device_runtime.state.motion_mouse_area_offsets[state_key] = (target_x, target_y)
    _emit_mouse(device_runtime, state_key, target_x - old_x, target_y - old_y)
