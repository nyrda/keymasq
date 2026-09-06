import pytest

from keymasq.common.model.hardware import AnalogAxisDefinition, AnalogInputDefinition
from keymasq.common.output_axes import (
    STANDARD_OUTPUT_AXES,
    OutputAxis,
    find_output_axis,
    learned_output_axes,
)


def test_standard_output_axes_include_sticks_triggers_and_hats() -> None:
    assert len(STANDARD_OUTPUT_AXES) == 8
    assert find_output_axis(STANDARD_OUTPUT_AXES, "x") == OutputAxis(
        "ABS_X", "Left Stick X", -32768, 32767
    )
    hat = find_output_axis(STANDARD_OUTPUT_AXES, "abs_hat0y")
    assert hat is not None and hat.discrete
    assert find_output_axis(STANDARD_OUTPUT_AXES, "ABS_THROTTLE") is None


def test_learned_axes_include_stick_components_and_custom_ranges() -> None:
    controls = [
        AnalogInputDefinition(
            "stick",
            "Stick",
            "stick",
            axes=[
                AnalogAxisDefinition("x", "ABS_X", minimum=0, maximum=1023, center=512),
                AnalogAxisDefinition("y", "ABS_Y", minimum=-100, maximum=100, center=5),
            ],
        )
    ]
    assert learned_output_axes(controls) == (
        OutputAxis("ABS_X", "Stick X", 0, 1023, 512),
        OutputAxis("ABS_Y", "Stick Y", -100, 100, 5),
    )
    assert learned_output_axes(
        [
            {
                "type": "axis",
                "label": "Throttle",
                "axes": [
                    {"evdev": "ABS_THROTTLE", "minimum": 10, "maximum": 500, "rest": 500},
                    {"evdev": "ABS_RUDDER"},
                ],
            }
        ]
    ) == (OutputAxis("ABS_THROTTLE", "Throttle", 10, 500, 500),)


def test_provider_axes_require_valid_range_and_neutral() -> None:
    with pytest.raises(ValueError):
        OutputAxis("ABS_X", "X", 0, 10, 20)
    with pytest.raises(ValueError):
        OutputAxis("KEY_A", "X", 0, 10)
