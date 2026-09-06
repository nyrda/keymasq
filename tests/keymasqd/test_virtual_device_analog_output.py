from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import evdev
import pytest

from keymasq.common.model.analog import AnalogControlConfig, AnalogGamepadOutputConfig
from keymasq.common.virtual_device_templates import (
    LOGITECH_EXTREME_3D_TEMPLATE,
    template_analog_inputs,
)
from keymasq.keymasqd.runtime.analog.gamepad import emit_gamepad_output, reset_gamepad_output
from keymasq.keymasqd.runtime.analog.output_state import reset_recorded_gamepad_outputs
from keymasq.keymasqd.runtime.virtual_gamepads import GamepadOutputTarget


@pytest.fixture
def flight_output():
    template = LOGITECH_EXTREME_3D_TEMPLATE
    writer = Mock()
    target = GamepadOutputTarget(
        output_id="flight-test",
        uinput=writer,
        bucket="gamepad:flight-test",
        is_virtual=True,
        analog_inputs=template_analog_inputs(template),
        axis_ranges={
            getattr(evdev.ecodes, axis.evdev.upper()): (axis.minimum, axis.maximum)
            for axis in template.axes
        },
        axis_rest_values={
            getattr(evdev.ecodes, axis.evdev.upper()): axis.rest for axis in template.axes
        },
    )
    runtime: Any = SimpleNamespace(
        hardware_id="source",
        analog_inputs={},
        analog_axis_output_codes={
            ("left_stick", "x"): evdev.ecodes.ABS_X,
            ("left_stick", "y"): evdev.ecodes.ABS_Y,
            ("throttle", "x"): evdev.ecodes.ABS_THROTTLE,
        },
        resolve_gamepad_output=lambda *_args: target,
        state=SimpleNamespace(
            analog_axis_values={},
            analog_gamepad_outputs={},
            held_output_abs={},
            passthrough_frame_output=None,
        ),
    )
    deps: Any = SimpleNamespace(evdev_mod=evdev, uinput_writer=lambda output: output)
    return runtime, deps, writer


@pytest.mark.parametrize("gyro", [False, True])
@pytest.mark.parametrize("value, expected", [(0.0, 511), (1.0, 1023), (-1.0, 0)])
def test_implicit_flight_stick_uses_destination_range_and_rest(
    flight_output, gyro, value, expected
):
    runtime, deps, writer = flight_output
    config = AnalogControlConfig(
        name="Stick",
        gamepad_output=AnalogGamepadOutputConfig(enabled=True, output_id="flight-test"),
    )
    runtime.state.analog_axis_values["stick"] = {"x": value, "y": 0.0}
    emit_gamepad_output(runtime, "stick", "left_stick", config, deps=deps, gyro=gyro)
    assert [call.args for call in writer.write.call_args_list] == [
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, expected),
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Y, 511),
    ]
    writer.reset_mock()
    if gyro:
        reset_recorded_gamepad_outputs(runtime, deps=deps)
    else:
        reset_gamepad_output(runtime, "stick", "left_stick", config, deps=deps)
    assert [call.args for call in writer.write.call_args_list] == [
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 511),
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Y, 511),
    ]


@pytest.mark.parametrize(
    "source, code, direction, expected, rest",
    [
        ("right_trigger", evdev.ecodes.ABS_RZ, "max", 255, 127),
        ("throttle", evdev.ecodes.ABS_THROTTLE, "min", 0, 255),
    ],
)
def test_implicit_flight_axis_uses_destination_rest(
    flight_output, source, code, direction, expected, rest
):
    runtime, deps, writer = flight_output
    config = AnalogControlConfig(
        name="Axis",
        input_type="axis",
        gamepad_output=AnalogGamepadOutputConfig(
            enabled=True, output_id="flight-test", output_direction=direction
        ),
    )
    runtime.state.analog_axis_values["axis"] = {"x": 1.0}
    emit_gamepad_output(runtime, "axis", source, config, deps=deps)
    writer.write.assert_called_once_with(evdev.ecodes.EV_ABS, code, expected)
    writer.reset_mock()
    reset_gamepad_output(runtime, "axis", source, config, deps=deps)
    writer.write.assert_called_once_with(evdev.ecodes.EV_ABS, code, rest)


def test_missing_destination_axes_are_not_written(flight_output):
    runtime, deps, writer = flight_output
    config = AnalogControlConfig(
        name="Stick",
        gamepad_output=AnalogGamepadOutputConfig(
            enabled=True, output_id="flight-test", target="right"
        ),
    )
    emit_gamepad_output(runtime, "stick", "left_stick", config, deps=deps)
    reset_gamepad_output(runtime, "stick", "left_stick", config, deps=deps)
    writer.write.assert_not_called()
