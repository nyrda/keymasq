"""Controller naming and a shared capability-backed output picker description."""

from collections.abc import Iterable
from dataclasses import replace

from keymasq.common.devices import (
    canonical_gamepad_button_name,
    gamepad_button_label,
    resolve_evdev_code,
)
from keymasq.common.gamepad_axes import gamepad_axis_range
from keymasq.common.model.hardware import HardwareConfig
from keymasq.common.output_axes import OutputAxis, learned_output_axes
from keymasq.common.types import JsonObject
from keymasq.common.virtual_device_templates import (
    VirtualAxis,
    VirtualButton,
    VirtualDeviceTemplate,
)


def is_flight_stick(names: Iterable[str]) -> bool:
    capabilities = {name.lower() for name in names}
    return bool(
        capabilities & {"btn_trigger", "btn_joystick", "btn_thumb", "btn_top", "btn_pinkie"}
    )


def controller_button_name(name: str) -> str:
    name = canonical_gamepad_button_name(name)
    return {
        "btn_joystick": "btn_trigger",
        "btn_trigger_happy": "btn_trigger_happy1",
        "btn_misc": "btn_0",
        "btn_mouse": "btn_left",
    }.get(name, name)


def controller_control_label(name: str) -> str:
    return gamepad_button_label(name) or {
        "btn_tl2": "LT",
        "btn_tr2": "RT",
        "btn_thumb2": "Thumb 2",
        "btn_top2": "Top 2",
        "abs_rx": "Rotation X",
        "abs_ry": "Rotation Y",
        "abs_rz": "Rotation Z",
    }.get(
        name,
        name.removeprefix("btn_")
        .removeprefix("key_")
        .removeprefix("abs_")
        .replace("_", " ")
        .title(),
    )


def _hardware_picker_axes(config: HardwareConfig) -> tuple[OutputAxis, ...]:
    result: list[OutputAxis] = []
    seen: set[int] = set()
    for analog in config.analog_inputs:
        known = {axis.code: axis for axis in learned_output_axes([analog])}
        for axis in analog.axes:
            code = resolve_evdev_code(axis.evdev)
            if code is None or code in seen:
                continue
            spec = known.get(code)
            if spec is None and (axis.minimum is None or axis.maximum is None):
                # Older setup saved axis bindings without ranges. Preserve the
                # previous picker's standard ranges without changing calibration.
                fallback = gamepad_axis_range(axis.evdev)
                if fallback is not None:
                    label = (
                        f"{analog.label} {axis.role.upper()}"
                        if analog.type == "stick"
                        else analog.label
                    )
                    spec = OutputAxis(
                        axis.evdev, label, fallback.minimum, fallback.maximum, fallback.neutral
                    )
            if spec is not None:
                seen.add(code)
                result.append(spec)
    return tuple(result)


def hardware_controller_template(config: HardwareConfig) -> VirtualDeviceTemplate:
    """Use saved physical controls, never a virtual device's assumed capabilities."""
    names = [name for device in config.evdev_devices for name in device.capabilities]
    names.extend(button.evdev for button in config.buttons)
    flight = is_flight_stick(names)
    buttons: dict[int, VirtualButton] = {}
    for button in config.buttons:
        code = resolve_evdev_code(button.evdev)
        if code is not None and button.evdev_value is None:
            buttons.setdefault(code, VirtualButton(button.id, button.label, button.evdev.lower()))
    return VirtualDeviceTemplate(
        id=config.hardware_id,
        label="Flight stick" if flight else "Gamepad",
        name=config.name,
        vendor_id=int(config.vendor_id, 16),
        product_id=int(config.product_id, 16),
        version=0,
        bustype=0,
        buttons=tuple(buttons.values()),
        axes=tuple(
            VirtualAxis(
                axis.evdev.lower(),
                axis.label,
                axis.evdev.lower(),
                axis.minimum,
                axis.maximum,
                rest=axis.neutral,
            )
            for axis in _hardware_picker_axes(config)
        ),
        layout="flight-stick" if flight else "gamepad",
    )


def routed_controller_template(
    config: HardwareConfig, inventory: JsonObject
) -> tuple[VirtualDeviceTemplate, frozenset[str]]:
    """Describe only the live interface selected by the physical output router."""
    source = str(inventory.get("source_interface_id", "") or "").lower()
    capabilities = set(inventory.get("capabilities", []))

    def advertised(name: str, event_type: str) -> bool:
        return (
            name.lower() in capabilities
            or f"{event_type}_{resolve_evdev_code(name)}" in capabilities
        )

    config = replace(
        config,
        buttons=[
            button
            for button in config.buttons
            if (not button.source or button.source.lower() == source)
            and advertised(button.evdev, "EV_KEY")
        ],
        analog_inputs=[],
    )
    template = hardware_controller_template(config)
    analogs = inventory["gamepad_output"].get("analog_inputs", {})
    axes = learned_output_axes(analogs.values())
    unknown_rest_codes = {
        resolve_evdev_code(axis.get("evdev")) if axis.get("evdev") else axis.get("evdev_code")
        for analog in analogs.values()
        if analog.get("type") != "stick"
        for axis in analog.get("axes", [])
        if axis.get("rest") is None
    }
    return replace(
        template,
        axes=tuple(
            VirtualAxis(
                axis.evdev.lower(),
                axis.label,
                axis.evdev.lower(),
                axis.minimum,
                axis.maximum,
                rest=axis.neutral,
            )
            for axis in axes
            if advertised(axis.evdev, "EV_ABS")
        ),
    ), frozenset(axis.evdev.lower() for axis in axes if axis.code in unknown_rest_codes)
