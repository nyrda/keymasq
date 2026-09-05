"""Virtual-gamepad routing and axis emission for analog controls."""

from keymasq.common.model.analog import AnalogControlConfig
from keymasq.keymasqd.runtime.analog.curves import (
    DEFAULT_STICK_MAX,
    DEFAULT_STICK_MIN,
    DEFAULT_TRIGGER_MAX,
    DEFAULT_TRIGGER_MIN,
    apply_control_axis_output_curve,
    apply_signed_axis_output_curve,
    apply_stick_output_curve,
    denormalize_axis_value,
    denormalize_control_axis_value,
)
from keymasq.keymasqd.runtime.analog.metadata import (
    axis_evdev_code,
    axis_int,
    axis_min_max,
    resolve_gamepad_output_target,
    stick_axis_center,
    stick_output_axis_specs,
    stick_output_reset_axes,
    target_analog_input,
    target_axes,
    target_axis,
    trigger_outputaxis_code,
)
from keymasq.keymasqd.runtime.analog.output_state import write_gamepad_axes
from keymasq.keymasqd.runtime.grabbed_device.types import (
    ActionExecutionDeps,
    GrabbedDeviceRuntime,
)


def emit_gamepad_output(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    source_id: str,
    config: AnalogControlConfig,
    *,
    gyro: bool = False,
    minimum_output: float = 0.0,
    deps: ActionExecutionDeps,
) -> None:
    if not config.gamepad_output.enabled:
        return
    if config.input_type == "axis":
        _emit_trigger_gamepad_output(device_runtime, state_key, source_id, config, deps=deps)
        return
    _emit_stick_gamepad_output(
        device_runtime,
        state_key,
        source_id,
        config,
        gyro=gyro,
        minimum_output=minimum_output,
        deps=deps,
    )


def _gamepad_output_direction(config: AnalogControlConfig) -> str:
    direction = str(config.gamepad_output.output_direction or "").lower()
    if direction in {"min", "max", "both"}:
        return direction
    return "min" if config.gamepad_output.output_invert else "max"


def _gamepad_output_stick_axis_inverted(config: AnalogControlConfig, role: str) -> bool:
    if role == "x":
        return bool(config.gamepad_output.output_invert_x)
    if role == "y":
        return bool(config.gamepad_output.output_invert_y)
    return False


def _emit_stick_gamepad_output(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    source_id: str,
    config: AnalogControlConfig,
    *,
    gyro: bool = False,
    minimum_output: float = 0.0,
    deps: ActionExecutionDeps,
) -> None:
    if config.gamepad_output.target == "analog":
        _emit_analog_stick_output(
            device_runtime,
            state_key,
            source_id,
            config,
            gyro=gyro,
            minimum_output=minimum_output,
            deps=deps,
        )
        return
    target = resolve_gamepad_output_target(device_runtime, source_id, config)
    if target is None:
        return
    axis_specs = stick_output_axis_specs(
        device_runtime,
        source_id,
        config,
        deps=deps,
        target=target,
    )
    if axis_specs is None:
        return
    axis_values = device_runtime.state.analog_axis_values.get(state_key, {})
    x = float(axis_values.get("x", 0.0))
    y = float(axis_values.get("y", 0.0))
    x, y = apply_stick_output_curve(
        x,
        y,
        deadzone=float(config.gamepad_output.deadzone),
        sensitivity=float(config.gamepad_output.sensitivity),
        response_curve=float(config.gamepad_output.response_curve),
    )
    write_gamepad_axes(
        device_runtime,
        state_key,
        source_id,
        config,
        tuple(
            (
                axis_code,
                denormalize_axis_value(
                    x if role == "x" else y,
                    minimum,
                    maximum,
                    center=center,
                    invert=invert,
                ),
            )
            for role, axis_code, minimum, maximum, center, invert in axis_specs
        ),
        reset_axes=stick_output_reset_axes(axis_specs),
        gyro_axes=(
            {
                code: (minimum, maximum, center, (x if role == "x" else y) * (-1 if invert else 1))
                for role, code, minimum, maximum, center, invert in axis_specs
            }
            if gyro
            else None
        ),
        minimum_output=minimum_output,
        deps=deps,
        target=target,
    )


def _emit_trigger_gamepad_output(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    source_id: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    if config.gamepad_output.target == "analog":
        _emit_analog_axis_output(device_runtime, state_key, source_id, config, deps=deps)
        return
    axis_code = trigger_outputaxis_code(device_runtime, source_id, config, deps=deps)
    if axis_code is None:
        return
    axis_values = device_runtime.state.analog_axis_values.get(state_key, {})
    if _gamepad_output_direction(config) == "both":
        value = float(axis_values.get("x_signed", 0.0))
        value = apply_signed_axis_output_curve(
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
            invert=config.gamepad_output.output_invert,
        )
    else:
        value = float(axis_values.get("x", 0.0))
        value = apply_control_axis_output_curve(
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
    write_gamepad_axes(
        device_runtime,
        state_key,
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


def reset_gamepad_output(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    source_id: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    if config.gamepad_output.target == "analog":
        _reset_analog_gamepad_output(device_runtime, state_key, source_id, config, deps=deps)
        return
    target: object | None = None
    if config.input_type == "axis":
        axis_code = trigger_outputaxis_code(device_runtime, source_id, config, deps=deps)
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
        target = resolve_gamepad_output_target(device_runtime, source_id, config)
        if target is None:
            return
        axis_specs = stick_output_axis_specs(
            device_runtime,
            source_id,
            config,
            deps=deps,
            target=target,
        )
        if axis_specs is None:
            return
        axes = stick_output_reset_axes(axis_specs)
    write_gamepad_axes(
        device_runtime,
        state_key,
        source_id,
        config,
        axes,
        reset_axes=axes,
        releasing=True,
        deps=deps,
        target=target,
    )


def _emit_analog_axis_output(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    source_id: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    target = resolve_gamepad_output_target(device_runtime, source_id, config)
    if target is None:
        return
    analog = target_analog_input(target, config, expected_type="axis")
    axis = target_axis(analog, "x") if analog is not None else None
    if axis is None:
        return
    axis_values = device_runtime.state.analog_axis_values.get(state_key, {})
    axis_code = axis_evdev_code(axis)
    if axis_code is None:
        return
    minimum, maximum = axis_min_max(axis, DEFAULT_TRIGGER_MIN, DEFAULT_TRIGGER_MAX)
    output_rest = (
        config.gamepad_output.output_rest
        if config.gamepad_output.output_rest is not None
        else axis_int(axis, "rest")
    )
    reset_value = output_rest if output_rest is not None else (minimum if minimum >= 0 else 0)
    if _gamepad_output_direction(config) == "both":
        value = float(axis_values.get("x_signed", 0.0))
        value = apply_signed_axis_output_curve(
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
            invert=config.gamepad_output.output_invert,
        )
    else:
        value = float(axis_values.get("x", 0.0))
        value = apply_control_axis_output_curve(
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
    write_gamepad_axes(
        device_runtime,
        state_key,
        source_id,
        config,
        ((axis_code, raw_value),),
        reset_axes=((axis_code, reset_value),),
        deps=deps,
        target=target,
    )


def _emit_analog_stick_output(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    source_id: str,
    config: AnalogControlConfig,
    *,
    gyro: bool = False,
    minimum_output: float = 0.0,
    deps: ActionExecutionDeps,
) -> None:
    target = resolve_gamepad_output_target(device_runtime, source_id, config)
    if target is None:
        return
    analog = target_analog_input(target, config, expected_type="stick")
    if analog is None:
        return
    axes: list[tuple[int, int]] = []
    reset_axes: list[tuple[int, int]] = []
    gyro_axes: dict[int, tuple[int, int, int, float]] = {}
    axis_values = device_runtime.state.analog_axis_values.get(state_key, {})
    x = float(axis_values.get("x", 0.0))
    y = float(axis_values.get("y", 0.0))
    x, y = apply_stick_output_curve(
        x,
        y,
        deadzone=float(config.gamepad_output.deadzone),
        sensitivity=float(config.gamepad_output.sensitivity),
        response_curve=float(config.gamepad_output.response_curve),
    )
    for role, normalized in (("x", x), ("y", y)):
        axis = target_axis(analog, role)
        if axis is None:
            return
        axis_code = axis_evdev_code(axis)
        if axis_code is None:
            return
        minimum, maximum = axis_min_max(axis, DEFAULT_STICK_MIN, DEFAULT_STICK_MAX)
        reset_value = stick_axis_center(axis, minimum, maximum)
        invert = bool(axis.get("invert", False)) ^ _gamepad_output_stick_axis_inverted(config, role)
        gyro_axes[axis_code] = (
            minimum,
            maximum,
            reset_value,
            -normalized if invert else normalized,
        )
        axes.append(
            (
                axis_code,
                denormalize_axis_value(
                    normalized,
                    minimum,
                    maximum,
                    center=reset_value,
                    invert=invert,
                ),
            )
        )
        reset_axes.append((axis_code, reset_value))
    write_gamepad_axes(
        device_runtime,
        state_key,
        source_id,
        config,
        tuple(axes),
        reset_axes=tuple(reset_axes),
        gyro_axes=gyro_axes if gyro else None,
        minimum_output=minimum_output,
        deps=deps,
        target=target,
    )


def _reset_analog_gamepad_output(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    source_id: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
) -> None:
    target = resolve_gamepad_output_target(device_runtime, source_id, config)
    if target is None:
        return
    analog = target_analog_input(target, config, expected_type=config.input_type)
    if analog is None:
        return
    axes: list[tuple[int, int]] = []
    for axis in target_axes(analog):
        axis_code = axis_evdev_code(axis)
        if axis_code is None:
            continue
        if config.input_type == "axis":
            axes.append(
                (
                    axis_code,
                    config.gamepad_output.output_rest
                    if config.gamepad_output.output_rest is not None
                    else axis_int(axis, "rest") or DEFAULT_TRIGGER_MIN,
                )
            )
        else:
            minimum, maximum = axis_min_max(axis, DEFAULT_STICK_MIN, DEFAULT_STICK_MAX)
            axes.append((axis_code, stick_axis_center(axis, minimum, maximum)))
    write_gamepad_axes(
        device_runtime,
        state_key,
        source_id,
        config,
        tuple(axes),
        reset_axes=tuple(axes),
        releasing=True,
        deps=deps,
        target=target,
    )
