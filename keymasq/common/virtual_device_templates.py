from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import cast

import evdev

from keymasq.common.output_axes import OutputAxis
from keymasq.common.virtual_devices import MAX_VIRTUAL_GAMEPADS, virtual_gamepad_output_id

MAX_USER_VIRTUAL_DEVICES = 4
MAX_TEMPLATE_BUTTONS = 40
MAX_TEMPLATE_AXES = 8
XBOX_360_TEMPLATE_ID = "xbox-360"
LOGITECH_EXTREME_3D_TEMPLATE_ID = "logitech-extreme-3d-pro"

_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SDL_CLASSIFIER_AXES = {
    "abs_rx",
    "abs_ry",
    "abs_rz",
    "abs_throttle",
    "abs_rudder",
    "abs_wheel",
    "abs_gas",
    "abs_brake",
}
_BUS_TYPES = {
    "pci": 0x01,
    "usb": 0x03,
    "bluetooth": 0x05,
    "virtual": 0x06,
}


class VirtualDeviceConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class VirtualButton:
    id: str
    label: str
    evdev: str


@dataclass(frozen=True, slots=True)
class VirtualAxis:
    id: str
    label: str
    evdev: str
    minimum: int
    maximum: int
    rest: int = 0
    fuzz: int = 0
    flat: int = 0
    resolution: int = 0


@dataclass(frozen=True, slots=True)
class VirtualDeviceTemplate:
    id: str
    label: str
    name: str
    vendor_id: int
    product_id: int
    version: int
    bustype: int
    buttons: tuple[VirtualButton, ...]
    axes: tuple[VirtualAxis, ...]
    builtin: bool = False
    layout: str = "gamepad"


@dataclass(frozen=True, slots=True)
class VirtualDeviceInstance:
    output_id: str
    template_id: str
    name: str | None = None
    vendor_id: int | None = None
    product_id: int | None = None
    version: int | None = None
    bustype: int | None = None


@dataclass(frozen=True, slots=True)
class VirtualDeviceConfig:
    templates: tuple[VirtualDeviceTemplate, ...] = ()
    devices: tuple[VirtualDeviceInstance, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedVirtualDevice:
    output_id: str
    template: VirtualDeviceTemplate
    name: str
    vendor_id: int
    product_id: int
    version: int
    bustype: int


def _button(id: str, label: str, evdev_name: str) -> VirtualButton:
    return VirtualButton(id=id, label=label, evdev=evdev_name)


def _axis(
    id: str,
    label: str,
    evdev_name: str,
    minimum: int,
    maximum: int,
    *,
    rest: int = 0,
    fuzz: int = 0,
    flat: int = 0,
) -> VirtualAxis:
    return VirtualAxis(
        id=id,
        label=label,
        evdev=evdev_name,
        minimum=minimum,
        maximum=maximum,
        rest=rest,
        fuzz=fuzz,
        flat=flat,
    )


XBOX_360_TEMPLATE = VirtualDeviceTemplate(
    id=XBOX_360_TEMPLATE_ID,
    label="Standard gamepad",
    name="keymasq-gamepad",
    vendor_id=0x045E,
    product_id=0x028E,
    version=0x0110,
    bustype=_BUS_TYPES["usb"],
    buttons=(
        _button("a", "A", "btn_south"),
        _button("b", "B", "btn_east"),
        _button("x", "X", "btn_west"),
        _button("y", "Y", "btn_north"),
        _button("left-shoulder", "LB", "btn_tl"),
        _button("right-shoulder", "RB", "btn_tr"),
        _button("left-trigger-button", "LT button", "btn_tl2"),
        _button("right-trigger-button", "RT button", "btn_tr2"),
        _button("select", "Select", "btn_select"),
        _button("start", "Start", "btn_start"),
        _button("guide", "Guide", "btn_mode"),
        _button("left-stick", "LS", "btn_thumbl"),
        _button("right-stick", "RS", "btn_thumbr"),
        _button("dpad-up", "D-pad Up", "btn_dpad_up"),
        _button("dpad-down", "D-pad Down", "btn_dpad_down"),
        _button("dpad-left", "D-pad Left", "btn_dpad_left"),
        _button("dpad-right", "D-pad Right", "btn_dpad_right"),
    ),
    axes=(
        _axis("left-x", "Left Stick X", "abs_x", -32768, 32767, fuzz=16, flat=128),
        _axis("left-y", "Left Stick Y", "abs_y", -32768, 32767, fuzz=16, flat=128),
        _axis("right-x", "Right Stick X", "abs_rx", -32768, 32767, fuzz=16, flat=128),
        _axis("right-y", "Right Stick Y", "abs_ry", -32768, 32767, fuzz=16, flat=128),
        _axis("left-trigger", "Left Trigger", "abs_z", 0, 255),
        _axis("right-trigger", "Right Trigger", "abs_rz", 0, 255),
        _axis("dpad-x", "D-pad X", "abs_hat0x", -1, 1),
        _axis("dpad-y", "D-pad Y", "abs_hat0y", -1, 1),
    ),
    builtin=True,
)


LOGITECH_EXTREME_3D_TEMPLATE = VirtualDeviceTemplate(
    id=LOGITECH_EXTREME_3D_TEMPLATE_ID,
    label="Flight stick",
    name="Logitech Logitech Extreme 3D",
    vendor_id=0x046D,
    product_id=0xC215,
    version=0x0110,
    bustype=_BUS_TYPES["usb"],
    buttons=(
        _button("trigger", "Trigger", "btn_trigger"),
        _button("thumb", "Thumb", "btn_thumb"),
        _button("thumb-2", "Thumb 2", "btn_thumb2"),
        _button("top", "Top", "btn_top"),
        _button("top-2", "Top 2", "btn_top2"),
        _button("pinkie", "Pinkie", "btn_pinkie"),
        _button("base-1", "Base 1", "btn_base"),
        _button("base-2", "Base 2", "btn_base2"),
        _button("base-3", "Base 3", "btn_base3"),
        _button("base-4", "Base 4", "btn_base4"),
        _button("base-5", "Base 5", "btn_base5"),
        _button("base-6", "Base 6", "btn_base6"),
    ),
    axes=(
        _axis("stick-x", "Stick X", "abs_x", 0, 1023, rest=511, fuzz=3, flat=63),
        _axis("stick-y", "Stick Y", "abs_y", 0, 1023, rest=511, fuzz=3, flat=63),
        _axis("twist", "Twist", "abs_rz", 0, 255, rest=127, flat=15),
        _axis("throttle", "Throttle", "abs_throttle", 0, 255, rest=255, flat=15),
        _axis("hat-x", "Hat X", "abs_hat0x", -1, 1),
        _axis("hat-y", "Hat Y", "abs_hat0y", -1, 1),
    ),
    builtin=True,
    layout="flight-stick",
)

BUILTIN_VIRTUAL_DEVICE_TEMPLATES = (
    XBOX_360_TEMPLATE,
    LOGITECH_EXTREME_3D_TEMPLATE,
)


def _require_id(value: object, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _ID_PATTERN.fullmatch(normalized):
        raise VirtualDeviceConfigError(f"{field} must match {_ID_PATTERN.pattern}")
    return normalized


def _require_label(value: object, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise VirtualDeviceConfigError(f"{field} is required")
    if len(normalized.encode("utf-8")) > 80:
        raise VirtualDeviceConfigError(f"{field} must fit in 80 UTF-8 bytes")
    return normalized


def _hardware_int(value: object, field: str, *, default: int | None = None) -> int:
    if value is None and default is not None:
        return default
    try:
        parsed = int(str(value), 16) if isinstance(value, str) else int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise VirtualDeviceConfigError(f"{field} must be a 16-bit integer or hex string") from exc
    if not 0 <= parsed <= 0xFFFF:
        raise VirtualDeviceConfigError(f"{field} must be between 0x0000 and 0xffff")
    return parsed


def _event_code(name: object, *, axis: bool) -> str:
    normalized = str(name or "").strip().lower()
    prefix = "abs_" if axis else "btn_"
    if not normalized.startswith(prefix):
        raise VirtualDeviceConfigError(f"event code must start with {prefix}")
    if axis and normalized in {"abs_max", "abs_cnt"}:
        raise VirtualDeviceConfigError(f"{normalized} is an axis sentinel, not an input axis")
    code = getattr(evdev.ecodes, normalized.upper(), None)
    code_table = evdev.ecodes.ABS if axis else evdev.ecodes.keys
    if not isinstance(code, int) or code not in code_table:
        raise VirtualDeviceConfigError(f"unknown evdev code {normalized!r}")
    return normalized


def _dict(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise VirtualDeviceConfigError(f"{field} must be a table")
    return cast(dict[str, object], value)


def _list(value: object, field: str) -> list[object]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise VirtualDeviceConfigError(f"{field} must be an array")
    return cast(list[object], value)


def _button_from_data(value: object) -> VirtualButton:
    data = _dict(value, "button")
    return VirtualButton(
        id=_require_id(data.get("id"), "button.id"),
        label=_require_label(data.get("label"), "button.label"),
        evdev=_event_code(data.get("evdev"), axis=False),
    )


def _axis_from_data(value: object) -> VirtualAxis:
    data = _dict(value, "axis")
    minimum = _integer(data.get("minimum"), "axis.minimum")
    maximum = _integer(data.get("maximum"), "axis.maximum")
    rest = _integer(data.get("rest", 0), "axis.rest")
    if minimum >= maximum:
        raise VirtualDeviceConfigError("axis.minimum must be less than axis.maximum")
    if not minimum <= rest <= maximum:
        raise VirtualDeviceConfigError("axis.rest must be inside the axis range")
    return VirtualAxis(
        id=_require_id(data.get("id"), "axis.id"),
        label=_require_label(data.get("label"), "axis.label"),
        evdev=_event_code(data.get("evdev"), axis=True),
        minimum=minimum,
        maximum=maximum,
        rest=rest,
        fuzz=max(0, _integer(data.get("fuzz", 0), "axis.fuzz")),
        flat=max(0, _integer(data.get("flat", 0), "axis.flat")),
        resolution=max(0, _integer(data.get("resolution", 0), "axis.resolution")),
    )


def _integer(value: object, field: str) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise VirtualDeviceConfigError(f"{field} must be an integer") from exc
    if not -2147483648 <= parsed <= 2147483647:
        raise VirtualDeviceConfigError(f"{field} must be a signed 32-bit integer")
    return parsed


def template_from_data(value: object) -> VirtualDeviceTemplate:
    data = _dict(value, "template")
    template_id = _require_id(data.get("id"), "template.id")
    buttons = tuple(_button_from_data(item) for item in _list(data.get("buttons"), "buttons"))
    axes = tuple(_axis_from_data(item) for item in _list(data.get("axes"), "axes"))
    template = VirtualDeviceTemplate(
        id=template_id,
        label=_require_label(data.get("label"), "template.label"),
        name=_require_label(data.get("name"), "template.name"),
        vendor_id=_hardware_int(data.get("vendor_id"), "template.vendor_id"),
        product_id=_hardware_int(data.get("product_id"), "template.product_id"),
        version=_hardware_int(data.get("version"), "template.version", default=0x0100),
        bustype=_parse_bustype(data.get("bustype", "usb")),
        buttons=buttons,
        axes=axes,
        layout=str(data.get("layout", "gamepad")),
    )
    validate_template(template)
    return template


def _parse_bustype(value: object) -> int:
    normalized = str(value or "usb").strip().lower()
    if normalized in _BUS_TYPES:
        return _BUS_TYPES[normalized]
    return _hardware_int(value, "template.bustype")


def validate_template(template: VirtualDeviceTemplate) -> None:
    if template.layout not in {"gamepad", "flight-stick"}:
        raise VirtualDeviceConfigError("template.layout must be gamepad or flight-stick")
    if (
        template.id in {item.id for item in BUILTIN_VIRTUAL_DEVICE_TEMPLATES}
        and not template.builtin
    ):
        raise VirtualDeviceConfigError(f"template ID {template.id!r} is reserved")
    if not 1 <= len(template.buttons) <= MAX_TEMPLATE_BUTTONS:
        raise VirtualDeviceConfigError(f"template must define 1..{MAX_TEMPLATE_BUTTONS} buttons")
    if not 2 <= len(template.axes) <= MAX_TEMPLATE_AXES:
        raise VirtualDeviceConfigError(f"template must define 2..{MAX_TEMPLATE_AXES} axes")
    control_ids = [item.id for item in (*template.buttons, *template.axes)]
    if len(control_ids) != len(set(control_ids)):
        raise VirtualDeviceConfigError("template control IDs must be unique")
    event_codes = [
        (isinstance(item, VirtualAxis), int(getattr(evdev.ecodes, item.evdev.upper())))
        for item in (*template.buttons, *template.axes)
    ]
    if len(event_codes) != len(set(event_codes)):
        raise VirtualDeviceConfigError("template evdev codes must be unique")
    axis_codes = {axis.evdev for axis in template.axes}
    if not {"abs_x", "abs_y"}.issubset(axis_codes):
        raise VirtualDeviceConfigError("SDL-compatible templates must define abs_x and abs_y")
    button_codes = {
        cast(int, getattr(evdev.ecodes, button.evdev.upper())) for button in template.buttons
    }
    classifier_buttons = {
        evdev.ecodes.BTN_TRIGGER,
        evdev.ecodes.BTN_A,
        evdev.ecodes.BTN_1,
    }
    if not button_codes.intersection(classifier_buttons) and not axis_codes.intersection(
        _SDL_CLASSIFIER_AXES
    ):
        raise VirtualDeviceConfigError(
            "SDL-compatible templates need a joystick button or an RX/RY/RZ-style axis"
        )


def instance_from_data(value: object) -> VirtualDeviceInstance:
    data = _dict(value, "device")
    return VirtualDeviceInstance(
        output_id=_require_id(data.get("output_id"), "device.output_id"),
        template_id=_require_id(data.get("template"), "device.template"),
        name=_require_label(data.get("name"), "device.name") if data.get("name") else None,
        vendor_id=(
            _hardware_int(data.get("vendor_id"), "device.vendor_id")
            if data.get("vendor_id") is not None
            else None
        ),
        product_id=(
            _hardware_int(data.get("product_id"), "device.product_id")
            if data.get("product_id") is not None
            else None
        ),
        version=(
            _hardware_int(data.get("version"), "device.version")
            if data.get("version") is not None
            else None
        ),
        bustype=_parse_bustype(data.get("bustype")) if data.get("bustype") else None,
    )


def virtual_device_config_from_toml(data: dict[str, object]) -> VirtualDeviceConfig:
    templates = tuple(
        template_from_data(item) for item in _list(data.get("templates"), "templates")
    )
    if len({template.id for template in templates}) != len(templates):
        raise VirtualDeviceConfigError("template IDs must be unique")
    devices = tuple(instance_from_data(item) for item in _list(data.get("devices"), "devices"))
    if len(devices) > MAX_USER_VIRTUAL_DEVICES:
        raise VirtualDeviceConfigError(
            f"at most {MAX_USER_VIRTUAL_DEVICES} user virtual devices may be configured"
        )
    if len({device.output_id for device in devices}) != len(devices):
        raise VirtualDeviceConfigError("device output IDs must be unique")
    reserved_outputs = {
        virtual_gamepad_output_id(index) for index in range(1, MAX_VIRTUAL_GAMEPADS + 1)
    }
    if reserved_outputs.intersection(device.output_id for device in devices):
        raise VirtualDeviceConfigError("virtual-gamepad-N output IDs are reserved")
    template_ids = {template.id for template in (*BUILTIN_VIRTUAL_DEVICE_TEMPLATES, *templates)}
    missing = {device.template_id for device in devices} - template_ids
    if missing:
        raise VirtualDeviceConfigError(f"unknown device template(s): {', '.join(sorted(missing))}")
    return VirtualDeviceConfig(templates=templates, devices=devices)


def _button_to_data(button: VirtualButton) -> dict[str, object]:
    return {"id": button.id, "label": button.label, "evdev": button.evdev}


def _axis_to_data(axis: VirtualAxis) -> dict[str, object]:
    return {
        "id": axis.id,
        "label": axis.label,
        "evdev": axis.evdev,
        "minimum": axis.minimum,
        "maximum": axis.maximum,
        "rest": axis.rest,
        "fuzz": axis.fuzz,
        "flat": axis.flat,
        "resolution": axis.resolution,
    }


def _hex(value: int) -> str:
    return f"{value:04x}"


def template_to_data(template: VirtualDeviceTemplate) -> dict[str, object]:
    return {
        "id": template.id,
        "layout": template.layout,
        "label": template.label,
        "name": template.name,
        "vendor_id": _hex(template.vendor_id),
        "product_id": _hex(template.product_id),
        "version": _hex(template.version),
        "bustype": _hex(template.bustype),
        "buttons": [_button_to_data(button) for button in template.buttons],
        "axes": [_axis_to_data(axis) for axis in template.axes],
    }


def instance_to_data(instance: VirtualDeviceInstance) -> dict[str, object]:
    data: dict[str, object] = {
        "output_id": instance.output_id,
        "template": instance.template_id,
    }
    for key, value in (
        ("name", instance.name),
        ("vendor_id", instance.vendor_id),
        ("product_id", instance.product_id),
        ("version", instance.version),
        ("bustype", instance.bustype),
    ):
        if value is not None:
            data[key] = _hex(value) if isinstance(value, int) else value
    return data


def virtual_device_config_to_toml(config: VirtualDeviceConfig) -> dict[str, object]:
    return {
        "templates": [template_to_data(template) for template in config.templates],
        "devices": [instance_to_data(device) for device in config.devices],
    }


def template_catalog(config: VirtualDeviceConfig) -> dict[str, VirtualDeviceTemplate]:
    return {
        template.id: template for template in (*BUILTIN_VIRTUAL_DEVICE_TEMPLATES, *config.templates)
    }


def template_output_axes(template: VirtualDeviceTemplate) -> tuple[OutputAxis, ...]:
    """Describe the exact axes advertised by a virtual output for routing and editing."""
    return tuple(
        OutputAxis(axis.evdev, axis.label, axis.minimum, axis.maximum, axis.rest)
        for axis in template.axes
    )


def template_analog_inputs(template: VirtualDeviceTemplate) -> dict[str, object]:
    inputs: dict[str, object] = {}
    axes_by_evdev = {axis.evdev: axis for axis in template.axes}
    paired_codes = (("abs_x", "abs_y"), ("abs_rx", "abs_ry"))
    paired_axis_ids: set[str] = set()
    for x_code, y_code in paired_codes:
        x_axis = axes_by_evdev.get(x_code)
        y_axis = axes_by_evdev.get(y_code)
        if x_axis is None or y_axis is None:
            continue
        analog_id = f"{x_axis.id}__{y_axis.id}"
        inputs[analog_id] = {
            "label": f"{x_axis.label} / {y_axis.label}",
            "type": "stick",
            "axes": [
                _axis_analog_data(x_axis, role="x"),
                _axis_analog_data(y_axis, role="y"),
            ],
        }
        paired_axis_ids.update((x_axis.id, y_axis.id))
    for axis in template.axes:
        if axis.id in paired_axis_ids:
            continue
        inputs[axis.id] = {
            "label": axis.label,
            "type": "axis",
            "axes": [_axis_analog_data(axis, role="x")],
        }
    return inputs


def _axis_analog_data(axis: VirtualAxis, *, role: str) -> dict[str, object]:
    return {
        "role": role,
        "evdev": axis.evdev,
        "minimum": axis.minimum,
        "maximum": axis.maximum,
        "center": axis.rest,
        "rest": axis.rest,
    }


def resolve_virtual_devices(
    xbox_count: int,
    config: VirtualDeviceConfig,
) -> tuple[ResolvedVirtualDevice, ...]:
    catalog = template_catalog(config)
    resolved: list[ResolvedVirtualDevice] = []
    for index in range(1, xbox_count + 1):
        name = XBOX_360_TEMPLATE.name if index == 1 else f"{XBOX_360_TEMPLATE.name}-{index}"
        resolved.append(
            ResolvedVirtualDevice(
                output_id=virtual_gamepad_output_id(index),
                template=XBOX_360_TEMPLATE,
                name=name,
                vendor_id=XBOX_360_TEMPLATE.vendor_id,
                product_id=XBOX_360_TEMPLATE.product_id,
                version=XBOX_360_TEMPLATE.version,
                bustype=XBOX_360_TEMPLATE.bustype,
            )
        )
    for instance in config.devices:
        template = catalog[instance.template_id]
        resolved.append(
            ResolvedVirtualDevice(
                output_id=instance.output_id,
                template=template,
                name=instance.name or template.name,
                vendor_id=(
                    instance.vendor_id if instance.vendor_id is not None else template.vendor_id
                ),
                product_id=(
                    instance.product_id if instance.product_id is not None else template.product_id
                ),
                version=instance.version if instance.version is not None else template.version,
                bustype=instance.bustype if instance.bustype is not None else template.bustype,
            )
        )
    return tuple(resolved)


def config_to_json(config: VirtualDeviceConfig) -> dict[str, object]:
    return virtual_device_config_to_toml(config)


def config_from_json(value: object) -> VirtualDeviceConfig:
    return virtual_device_config_from_toml(_dict(value, "virtual device config"))


def numbered_button_batch(
    buttons: Sequence[VirtualButton], count: int, *, reserved_ids: Collection[str] = ()
) -> tuple[VirtualButton, ...]:
    """Allocate independent TriggerHappy buttons without reusing codes or IDs."""
    if count < 1 or len(buttons) + count > MAX_TEMPLATE_BUTTONS:
        raise VirtualDeviceConfigError(
            f"A template can contain at most {MAX_TEMPLATE_BUTTONS} buttons"
        )
    codes = {int(getattr(evdev.ecodes, button.evdev.upper())) for button in buttons}
    used_ids = {button.id for button in buttons} | set(reserved_ids)
    added: list[VirtualButton] = []
    for index in range(1, 41):
        code = f"btn_trigger_happy{index}"
        if int(getattr(evdev.ecodes, code.upper())) in codes:
            continue
        base = f"extra-button-{index}"
        control_id = base
        suffix = 2
        while control_id in used_ids:
            control_id = f"{base}-{suffix}"
            suffix += 1
        used_ids.add(control_id)
        added.append(VirtualButton(control_id, f"Button {len(buttons) + len(added) + 1}", code))
        if len(added) == count:
            return tuple(added)
    raise VirtualDeviceConfigError("Not enough unused TriggerHappy codes")
