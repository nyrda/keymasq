import logging
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import evdev
import pytest

from keymasq.common.model.analog import AnalogControlConfig, AnalogGamepadOutputConfig
from keymasq.common.virtual_device_templates import (
    LOGITECH_EXTREME_3D_TEMPLATE,
    VirtualAxis,
    VirtualDeviceConfig,
    VirtualDeviceInstance,
    VirtualStick,
    resolve_virtual_devices,
    validate_template,
)
from keymasq.keymasqd.runtime.analog.gamepad import emit_gamepad_output, reset_gamepad_output
from keymasq.keymasqd.runtime.analog.output_state import reset_recorded_gamepad_outputs
from keymasq.keymasqd.runtime.virtual_gamepads import GamepadOutputRouter


@pytest.fixture
def flight_output(request):
    template = LOGITECH_EXTREME_3D_TEMPLATE
    if getattr(request, "param", "builtin") == "custom":
        template = replace(
            template,
            id="custom-flight",
            builtin=False,
            axes=(*template.axes, VirtualAxis("brake", "Brake", "abs_brake", 10, 900, 100)),
        )
        validate_template(template)
    writer = Mock()
    router = GamepadOutputRouter(logging.getLogger(__name__))
    config = VirtualDeviceConfig(
        templates=() if template.builtin else (template,),
        devices=(VirtualDeviceInstance("flight-test", template.id),),
    )
    outputs = SimpleNamespace(
        virtual_gamepad_uinputs={"flight-test": writer},
        virtual_device_specs={
            device.output_id: device for device in resolve_virtual_devices(0, config)
        },
    )
    target = router.resolve(outputs, {}, "flight-test")
    assert target is not None
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


@pytest.mark.parametrize(
    "flight_output, axis, direction, value, expected, neutral",
    [
        ("builtin", "abs_x", "both", -1.0, 0, 511),
        ("builtin", "abs_x", "both", 1.0, 1023, 511),
        ("builtin", "abs_y", "both", 0.0, 511, 511),
        ("builtin", "abs_rz", "both", -1.0, 0, 127),
        ("builtin", "abs_throttle", "min", 1.0, 0, 255),
        ("custom", "abs_brake", "max", 1.0, 900, 100),
        ("custom", "abs_brake", "min", 1.0, 10, 100),
    ],
    indirect=["flight_output"],
)
@pytest.mark.parametrize("recorded_reset", [False, True])
def test_individual_template_axis_emits_and_resets_only_selected_axis(
    flight_output, axis, direction, value, expected, neutral, recorded_reset
):
    runtime, deps, writer = flight_output
    config = AnalogControlConfig(
        name="Single template axis",
        input_type="axis",
        gamepad_output=AnalogGamepadOutputConfig(
            enabled=True,
            output_id="flight-test",
            target="axis",
            target_axis=axis,
            output_direction=direction,
        ),
    )
    runtime.state.analog_axis_values["axis"] = {"x": value, "x_signed": value}
    emit_gamepad_output(runtime, "axis", "left_trigger", config, deps=deps)
    if recorded_reset:
        reset_recorded_gamepad_outputs(runtime, deps=deps)
    else:
        reset_gamepad_output(runtime, "axis", "left_trigger", config, deps=deps)
    code = getattr(evdev.ecodes, axis.upper())
    assert [call.args for call in writer.write.call_args_list] == [
        (evdev.ecodes.EV_ABS, code, expected),
        (evdev.ecodes.EV_ABS, code, neutral),
    ]


@pytest.mark.parametrize("target", ["axis", "same", "left"])
def test_flight_output_rejects_unadvertised_trigger_axis(flight_output, target):
    runtime, deps, writer = flight_output
    config = AnalogControlConfig(
        name="Unsupported trigger",
        input_type="axis",
        gamepad_output=AnalogGamepadOutputConfig(
            enabled=True,
            output_id="flight-test",
            target=target,
            target_axis="abs_z" if target == "axis" else None,
        ),
    )
    runtime.state.analog_axis_values["axis"] = {"x": 1.0}
    emit_gamepad_output(runtime, "axis", "left_trigger", config, deps=deps)
    reset_gamepad_output(runtime, "axis", "left_trigger", config, deps=deps)
    writer.write.assert_not_called()


def test_template_hat_preserves_hysteresis(flight_output):
    runtime, deps, writer = flight_output
    config = AnalogControlConfig(
        name="Hat",
        input_type="axis",
        gamepad_output=AnalogGamepadOutputConfig(
            enabled=True,
            output_id="flight-test",
            target="axis",
            target_axis="abs_hat0x",
            output_direction="both",
        ),
    )
    for value in (0.5, 0.6, 0.5, 0.4, -0.6):
        runtime.state.analog_axis_values["axis"] = {"x_signed": value}
        emit_gamepad_output(runtime, "axis", "left_trigger", config, deps=deps)
    reset_gamepad_output(runtime, "axis", "left_trigger", config, deps=deps)
    assert [call.args for call in writer.write.call_args_list] == [
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_HAT0X, value) for value in (0, 1, 1, 0, -1, 0)
    ]


@pytest.fixture
def hat_stick_output(flight_output):
    """A pad whose right stick is on Z/RZ and whose extra stick is a pair of hat axes."""
    template = replace(
        LOGITECH_EXTREME_3D_TEMPLATE,
        id="hat-stick-pad",
        builtin=False,
        axes=(
            VirtualAxis("lx", "LX", "abs_x", 0, 255, 128),
            VirtualAxis("ly", "LY", "abs_y", 0, 255, 128),
            VirtualAxis("rx", "RX", "abs_z", 0, 255, 128),
            VirtualAxis("ry", "RY", "abs_rz", 0, 255, 128),
            VirtualAxis("pad-x", "Pad X", "abs_hat1x", -1000, 1000),
            VirtualAxis("pad-y", "Pad Y", "abs_hat1y", -1000, 1000),
        ),
        sticks=(
            VirtualStick("right-stick", "Right stick", "rx", "ry", "right"),
            VirtualStick("pad", "Pad", "pad-x", "pad-y"),
        ),
    )
    validate_template(template)
    runtime, deps, _writer = flight_output
    writer = Mock()
    config = VirtualDeviceConfig(
        templates=(template,), devices=(VirtualDeviceInstance("pad-test", template.id),)
    )
    outputs = SimpleNamespace(
        virtual_gamepad_uinputs={"pad-test": writer},
        virtual_device_specs={
            device.output_id: device for device in resolve_virtual_devices(0, config)
        },
    )
    target = GamepadOutputRouter(logging.getLogger(__name__)).resolve(outputs, {}, "pad-test")
    assert target is not None
    runtime.resolve_gamepad_output = lambda *_args: target
    runtime.analog_axis_output_codes[("right_stick", "x")] = evdev.ecodes.ABS_RX
    runtime.analog_axis_output_codes[("right_stick", "y")] = evdev.ecodes.ABS_RY
    return runtime, deps, writer


@pytest.mark.parametrize("target", ["same", "right"])
def test_declared_side_routes_a_stick_to_non_standard_codes(hat_stick_output, target):
    runtime, deps, writer = hat_stick_output
    config = AnalogControlConfig(
        name="Stick",
        gamepad_output=AnalogGamepadOutputConfig(
            enabled=True, output_id="pad-test", target=target
        ),
    )
    runtime.state.analog_axis_values["stick"] = {"x": 1.0, "y": 0.0}
    emit_gamepad_output(runtime, "stick", "right_stick", config, deps=deps)
    assert [call.args for call in writer.write.call_args_list] == [
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 255),
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RZ, 128),
    ]


def test_same_prefers_matching_codes_over_a_declared_side(hat_stick_output):
    runtime, deps, writer = hat_stick_output
    config = AnalogControlConfig(
        name="Stick",
        gamepad_output=AnalogGamepadOutputConfig(enabled=True, output_id="pad-test"),
    )
    runtime.state.analog_axis_values["stick"] = {"x": -1.0, "y": 0.0}
    emit_gamepad_output(runtime, "stick", "left_stick", config, deps=deps)
    assert [call.args for call in writer.write.call_args_list] == [
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 0),
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Y, 128),
    ]


def test_named_hat_pair_stick_emits_and_resets_both_axes(hat_stick_output):
    runtime, deps, writer = hat_stick_output
    config = AnalogControlConfig(
        name="Stick",
        gamepad_output=AnalogGamepadOutputConfig(
            enabled=True, output_id="pad-test", target="analog", target_analog_id="pad"
        ),
    )
    runtime.state.analog_axis_values["stick"] = {"x": 0.0, "y": 1.0}
    emit_gamepad_output(runtime, "stick", "left_stick", config, deps=deps)
    assert [call.args for call in writer.write.call_args_list] == [
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_HAT1X, 0),
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_HAT1Y, 1000),
    ]
    writer.reset_mock()
    reset_gamepad_output(runtime, "stick", "left_stick", config, deps=deps)
    assert [call.args for call in writer.write.call_args_list] == [
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_HAT1X, 0),
        (evdev.ecodes.EV_ABS, evdev.ecodes.ABS_HAT1Y, 0),
    ]


@pytest.mark.parametrize(
    ("codes", "declared", "requested", "source"),
    [
        (("abs_x", "abs_y"), "right", "left", "left_stick"),
        (("abs_rx", "abs_ry"), "left", "right", "right_stick"),
    ],
)
def test_explicit_side_never_falls_back_to_the_opposite_stick(
    flight_output, codes, declared, requested, source
):
    axes = [
        VirtualAxis("x", "X", "abs_x", 0, 255, 128),
        VirtualAxis("y", "Y", "abs_y", 0, 255, 128),
        VirtualAxis("u", "U", "abs_rx", 0, 255, 128),
        VirtualAxis("v", "V", "abs_ry", 0, 255, 128),
    ]
    ids = [axis.id for axis in axes if axis.evdev in codes]
    # Pair the other two axes side-less so no stick exists for the requested side.
    others = [axis.id for axis in axes if axis.evdev not in codes]
    template = replace(
        LOGITECH_EXTREME_3D_TEMPLATE,
        id="one-sided",
        builtin=False,
        axes=tuple(axes),
        sticks=(
            VirtualStick("only", "Only", ids[0], ids[1], declared),
            VirtualStick("spare", "Spare", others[1], others[0]),
        ),
    )
    validate_template(template)
    runtime, deps, _writer = flight_output
    writer = Mock()
    config = VirtualDeviceConfig(
        templates=(template,), devices=(VirtualDeviceInstance("one-sided-test", template.id),)
    )
    outputs = SimpleNamespace(
        virtual_gamepad_uinputs={"one-sided-test": writer},
        virtual_device_specs={
            device.output_id: device for device in resolve_virtual_devices(0, config)
        },
    )
    target = GamepadOutputRouter(logging.getLogger(__name__)).resolve(
        outputs, {}, "one-sided-test"
    )
    runtime.resolve_gamepad_output = lambda *_args: target
    runtime.state.analog_axis_values["stick"] = {"x": 1.0, "y": 0.0}

    def emit(side):
        writer.reset_mock()
        control = AnalogControlConfig(
            name="Stick",
            gamepad_output=AnalogGamepadOutputConfig(
                enabled=True, output_id="one-sided-test", target=side
            ),
        )
        emit_gamepad_output(runtime, "stick", source, control, deps=deps)
        return [call.args[1] for call in writer.write.call_args_list]

    assert emit(requested) == []
    assert emit(declared) == [getattr(evdev.ecodes, code.upper()) for code in codes]
