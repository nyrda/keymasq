from keymasq.common.model.core import DeviceType
from keymasq.common.model.hardware import (
    AnalogInputDefinition,
    ButtonDefinition,
    EvdevDevice,
    HardwareConfig,
)
from keymasq.common.model.pointer import pointer_interface_ids


def _mouse(
    buttons: tuple[ButtonDefinition, ...] = (),
    analog_inputs: tuple[AnalogInputDefinition, ...] = (),
) -> HardwareConfig:
    return HardwareConfig(
        "cafe",
        "0004",
        "Mouse",
        [EvdevDevice("/dev/input/event4", DeviceType.MOUSE, "mouse")],
        list(buttons),
        analog_inputs=list(analog_inputs),
    )


def test_mouse_interface_provides_the_pointer_source():
    assert pointer_interface_ids(_mouse()) == ["mouse"]


def test_configured_control_named_pointer_keeps_the_id_and_hides_the_pointer_source():
    button = ButtonDefinition("pointer", "Side", "btn_side")
    analog = AnalogInputDefinition("pointer", "Stick", "stick")

    assert pointer_interface_ids(_mouse(buttons=(button,))) == []
    assert pointer_interface_ids(_mouse(analog_inputs=(analog,))) == []
