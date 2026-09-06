"""Target-device and axis metadata resolution for analog output routing."""

from typing import cast

from keymasq.common.devices import capability_name, resolve_evdev_code
from keymasq.common.model.analog import SAME_DEVICE_OUTPUT_ID, AnalogControlConfig
from keymasq.common.output_axes import (
    STANDARD_OUTPUT_AXES,
    OutputAxis,
    find_output_axis,
    learned_output_axes,
)
from keymasq.keymasqd.runtime.analog.curves import (
    DEFAULT_STICK_MAX,
    DEFAULT_STICK_MIN,
)
from keymasq.keymasqd.runtime.grabbed_device.types import (
    ActionExecutionDeps,
    GrabbedDeviceRuntime,
)

STICK_OUTPUT_AXES = {
    "left_stick": ("ABS_X", "ABS_Y"),
    "right_stick": ("ABS_RX", "ABS_RY"),
}
TRIGGER_OUTPUT_AXES = {
    "left_trigger": "ABS_Z",
    "right_trigger": "ABS_RZ",
}


def output_axis_specs(target: object) -> tuple[OutputAxis, ...]:
    supplied = getattr(target, "output_axes", None)
    if supplied is not None:
        return cast(tuple[OutputAxis, ...], supplied)
    analog_inputs = target_analog_inputs(target)
    if analog_inputs:
        return learned_output_axes(analog_inputs.values())
    return STANDARD_OUTPUT_AXES if bool(getattr(target, "is_virtual", True)) else ()


def resolve_output_axis(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    config: AnalogControlConfig,
    *,
    target: object,
    deps: ActionExecutionDeps,
) -> OutputAxis | None:
    output = config.gamepad_output
    if output.target == "axis":
        return find_output_axis(output_axis_specs(target), output.target_axis or "")
    if output.target == "analog":
        analog = target_analog_input(target, config, expected_type="axis")
        if analog is None:
            return None
        axis = target_axis(analog, "x")
        if axis is None:
            return None
        code = axis_evdev_code(axis)
        if code is None:
            return None
        minimum, maximum = axis_min_max(axis, 0, 255)
        neutral = axis_int(axis, "rest")
        if neutral is None:
            neutral = max(minimum, min(0, maximum))
        name = str(axis.get("evdev") or "") or capability_name(deps.evdev_mod.ecodes.EV_ABS, code)
        if not name:
            return None
        try:
            return OutputAxis(
                name,
                str(analog.get("label", "Axis")),
                minimum,
                maximum,
                max(minimum, min(maximum, neutral)),
            )
        except ValueError:
            return None
    code = trigger_outputaxis_code(device_runtime, source_id, config, deps=deps)
    if code is None:
        return None
    return next((axis for axis in output_axis_specs(target) if axis.code == code), None)


def _gamepad_output_stick_axis_inverted(
    config: AnalogControlConfig,
    role: str,
) -> bool:
    if role == "x":
        return bool(config.gamepad_output.output_invert_x)
    if role == "y":
        return bool(config.gamepad_output.output_invert_y)
    return False


def resolve_gamepad_output_target(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    config: AnalogControlConfig,
) -> object | None:
    return device_runtime.resolve_gamepad_output(
        resolved_gamepad_output_id(device_runtime, config),
        f"{source_id} analog output",
    )


def resolved_gamepad_output_id(
    device_runtime: GrabbedDeviceRuntime,
    config: AnalogControlConfig,
) -> str | None:
    if config.gamepad_output.output_id == SAME_DEVICE_OUTPUT_ID:
        return device_runtime.hardware_id
    return config.gamepad_output.output_id


def target_analog_input(
    target: object,
    config: AnalogControlConfig,
    *,
    expected_type: str,
) -> dict[str, object] | None:
    target_analog_id = config.gamepad_output.target_analog_id
    if not target_analog_id:
        return None
    typed_analog_inputs = target_analog_inputs(target)
    if typed_analog_inputs is None:
        return None
    raw_analog = typed_analog_inputs.get(target_analog_id)
    analog = _typed_analog_input(raw_analog, expected_type=expected_type)
    return analog


def target_analog_inputs(target: object) -> dict[str, object] | None:
    analog_inputs = getattr(target, "analog_inputs", None)
    if not isinstance(analog_inputs, dict):
        return None
    return cast(dict[str, object], analog_inputs)


def _typed_analog_input(
    raw_analog: object,
    *,
    expected_type: str,
) -> dict[str, object] | None:
    if not isinstance(raw_analog, dict):
        return None
    analog = cast(dict[str, object], raw_analog)
    if str(analog.get("type", "") or "") != expected_type:
        return None
    return analog


def target_axes(analog: dict[str, object]) -> list[dict[str, object]]:
    raw_axes = analog.get("axes")
    if not isinstance(raw_axes, list):
        return []
    axes = cast(list[object], raw_axes)
    return [cast(dict[str, object], axis) for axis in axes if isinstance(axis, dict)]


def target_axis(analog: dict[str, object], role: str) -> dict[str, object] | None:
    for axis in target_axes(analog):
        if str(axis.get("role", "") or "") == role:
            return axis
    return None


def axis_int(axis: dict[str, object], key: str) -> int | None:
    value = axis.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            return None
    return None


def axis_evdev_code(axis: dict[str, object]) -> int | None:
    code = axis_int(axis, "evdev_code")
    if code is not None:
        return code
    evdev_name = str(axis.get("evdev", "") or "")
    return resolve_evdev_code(evdev_name)


def axis_min_max(
    axis: dict[str, object],
    fallback_minimum: int,
    fallback_maximum: int,
) -> tuple[int, int]:
    minimum = axis_int(axis, "minimum")
    maximum = axis_int(axis, "maximum")
    if minimum is None or maximum is None or minimum >= maximum:
        return fallback_minimum, fallback_maximum
    return minimum, maximum


def stick_axis_center(axis: dict[str, object], minimum: int, maximum: int) -> int:
    center = axis_int(axis, "center")
    if center is not None:
        return center
    return int(round((minimum + maximum) / 2.0))


def _gamepad_output_stick_id(source_id: str, config: AnalogControlConfig) -> str:
    if config.gamepad_output.target == "left":
        return "left_stick"
    if config.gamepad_output.target == "right":
        return "right_stick"
    return source_id


def stick_output_axis_specs(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    config: AnalogControlConfig,
    *,
    deps: ActionExecutionDeps,
    target: object,
) -> tuple[tuple[str, int, int, int, int, bool], ...] | None:
    analog = _standard_stick_output_analog(device_runtime, source_id, config, target)
    if analog is not None:
        specs: list[tuple[str, int, int, int, int, bool]] = []
        for role in ("x", "y"):
            axis = target_axis(analog, role)
            if axis is None:
                return None
            axis_code = axis_evdev_code(axis)
            if axis_code is None:
                return None
            minimum, maximum = axis_min_max(axis, DEFAULT_STICK_MIN, DEFAULT_STICK_MAX)
            center = stick_axis_center(axis, minimum, maximum)
            invert = bool(axis.get("invert", False)) ^ _gamepad_output_stick_axis_inverted(
                config,
                role,
            )
            specs.append((role, int(axis_code), minimum, maximum, center, invert))
        return tuple(specs)

    axis_codes = _stick_outputaxis_codes(device_runtime, source_id, config, deps=deps)
    if axis_codes is None:
        return None
    return (
        (
            "x",
            axis_codes[0],
            DEFAULT_STICK_MIN,
            DEFAULT_STICK_MAX,
            0,
            bool(config.gamepad_output.output_invert_x),
        ),
        (
            "y",
            axis_codes[1],
            DEFAULT_STICK_MIN,
            DEFAULT_STICK_MAX,
            0,
            bool(config.gamepad_output.output_invert_y),
        ),
    )


def stick_output_reset_axes(
    axis_specs: tuple[tuple[str, int, int, int, int, bool], ...],
) -> tuple[tuple[int, int], ...]:
    return tuple(
        (axis_code, center) for _role, axis_code, _min, _max, center, _invert in axis_specs
    )


def _standard_stick_output_analog(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
    config: AnalogControlConfig,
    target: object,
) -> dict[str, object] | None:
    if bool(getattr(target, "is_virtual", False)):
        return None
    analog_inputs = target_analog_inputs(target)
    if analog_inputs is None:
        return None

    if config.gamepad_output.target == "same":
        analog = _typed_analog_input(analog_inputs.get(source_id), expected_type="stick")
        if analog is not None:
            return analog
        side = _source_stick_side(device_runtime, source_id)
    else:
        side = (
            config.gamepad_output.target
            if config.gamepad_output.target in {"left", "right"}
            else None
        )

    direct_id = f"{side}_stick" if side in {"left", "right"} else None
    if direct_id is not None:
        analog = _typed_analog_input(analog_inputs.get(direct_id), expected_type="stick")
        if analog is not None:
            return analog

    if side is None:
        return None
    for analog_id, raw_analog in analog_inputs.items():
        analog = _typed_analog_input(raw_analog, expected_type="stick")
        if analog is not None and _analog_stick_side(str(analog_id), analog) == side:
            return analog
    return None


def _source_stick_side(
    device_runtime: GrabbedDeviceRuntime,
    source_id: str,
) -> str | None:
    raw_analog = device_runtime.analog_inputs.get(source_id)
    analog = _typed_analog_input(raw_analog, expected_type="stick")
    if analog is not None:
        return _analog_stick_side(source_id, analog)
    return _analog_stick_side(source_id, {})


def _analog_stick_side(analog_id: str, analog: dict[str, object]) -> str | None:
    label = str(analog.get("label", "") or "")
    text = f"{analog_id} {label}".lower().replace("-", "_").replace(" ", "_")
    if "left_stick" in text or "stick_left" in text:
        return "left"
    if "right_stick" in text or "stick_right" in text:
        return "right"
    tokens = {token for token in text.split("_") if token}
    if "stick" in tokens:
        if tokens & {"left", "l", "ls"}:
            return "left"
        if tokens & {"right", "r", "rs"}:
            return "right"
    return None


def _stick_outputaxis_codes(
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


def trigger_outputaxis_code(
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
    if "left_trigger" in text or text.endswith("_lt") or "_lt_" in text or "l2" in text:
        return "left_trigger"
    if "right_trigger" in text or text.endswith("_rt") or "_rt_" in text or "r2" in text:
        return "right_trigger"
    return None
