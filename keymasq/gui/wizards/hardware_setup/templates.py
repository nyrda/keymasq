import math
from collections.abc import Mapping, Sequence
from typing import Any

import evdev

from keymasq.common.devices import (
    canonical_gamepad_button_name,
    capability_name,
    evdev_code_value,
    gamepad_button_label,
    is_by_id_path,
    is_low_res_wheel_evdev,
    normalize_input_classes,
    ordered_gamepad_button_names,
    primary_input_class,
    wheel_button_id,
    wheel_label,
)
from keymasq.common.model.hardware import (
    AnalogAxisDefinition,
    AnalogInputDefinition,
    ButtonDefinition,
    EvdevDevice,
)
from keymasq.common.model.motion import (
    MOTION_NORMALIZATION_VERSION,
    MotionAxisDefinition,
    MotionSensorDefinition,
    canonical_motion_axis,
)

InterfaceInfo = Mapping[str, Any]


def interface_device_types(iface: InterfaceInfo) -> list[str]:
    return normalize_input_classes(iface.get("device_types"), iface.get("device_type"))


def interface_has_role(iface: InterfaceInfo, role: str) -> bool:
    return role in interface_device_types(iface)


def interfaces_for_roles(
    discovered_interfaces: Sequence[InterfaceInfo],
    roles: set[str],
) -> list[InterfaceInfo]:
    interfaces = [
        iface
        for iface in discovered_interfaces
        if any(interface_has_role(iface, role) for role in roles)
    ]
    return interfaces or list(discovered_interfaces)


def merge_interface_lists(*interface_lists: Sequence[InterfaceInfo]) -> list[InterfaceInfo]:
    merged: list[InterfaceInfo] = []
    seen_keys: set[tuple[str, str]] = set()
    for interface_list in interface_lists:
        for iface in interface_list:
            iface_id = str(iface.get("id", "") or "")
            config_path = str(iface.get("config_path", "") or iface.get("stable_path", "") or "")
            key = (iface_id, config_path)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            merged.append(iface)
    return merged


def interfaces_have_capability(
    interfaces: Sequence[InterfaceInfo],
    capability: str,
) -> bool:
    capability_l = capability.strip().lower()
    for iface in interfaces:
        capabilities = {str(name).strip().lower() for name in iface.get("capabilities", [])}
        if capability_l in capabilities:
            return True
        raw_capabilities = iface.get("raw_capabilities") or {}
        if not isinstance(raw_capabilities, dict):
            continue
        for code in raw_capabilities.get(evdev.ecodes.EV_REL, []):
            name = capability_name(evdev.ecodes.EV_REL, code)
            if name == capability_l:
                return True
    return False


def build_evdev_devices(interfaces: Sequence[InterfaceInfo]) -> list[EvdevDevice]:
    evdev_devices = []
    for iface in interfaces:
        stable_path = str(iface.get("stable_path", "") or "")
        config_path = str(iface.get("config_path", "") or "")
        event_path = str(iface.get("path", "") or "")
        device_path = (
            stable_path if is_by_id_path(stable_path) else config_path or stable_path or event_path
        )
        iface_id = str(iface.get("id", "") or "")
        if not device_path or not iface_id:
            continue
        evdev_devices.append(
            EvdevDevice(
                path=device_path,
                device_type=primary_input_class(iface.get("device_types")),
                id=iface_id,
                phys=str(iface.get("phys", "") or "") or None,
                capabilities=list(iface.get("capabilities", [])),
            )
        )
    return evdev_devices


def build_standard_mouse_buttons(
    source_id: str,
    *,
    include_horizontal: bool = False,
) -> list[ButtonDefinition]:
    buttons = [
        ButtonDefinition(
            id="btn_left",
            label="Left Click",
            evdev="btn_left",
            source=source_id or None,
            type="button",
            zone="left",
        ),
        ButtonDefinition(
            id="btn_right",
            label="Right Click",
            evdev="btn_right",
            source=source_id or None,
            type="button",
            zone="right",
        ),
        ButtonDefinition(
            id="btn_middle",
            label="Middle Click",
            evdev="btn_middle",
            source=source_id or None,
            type="button",
            zone="wheel",
        ),
        ButtonDefinition(
            id="btn_back",
            label="Back",
            evdev="btn_side",
            source=source_id or None,
            type="button",
            zone="thumb",
        ),
        ButtonDefinition(
            id="btn_forward",
            label="Forward",
            evdev="btn_extra",
            source=source_id or None,
            type="button",
            zone="thumb",
        ),
    ]
    buttons.extend(standard_wheel_buttons(source_id, include_horizontal))
    return buttons


def standard_wheel_buttons(
    source_id: str,
    include_horizontal: bool,
) -> list[ButtonDefinition]:
    specs = [("rel_wheel", 1), ("rel_wheel", -1)]
    if include_horizontal:
        specs.extend([("rel_hwheel", -1), ("rel_hwheel", 1)])

    buttons: list[ButtonDefinition] = []
    for evdev_name, value in specs:
        button_id = wheel_button_id(evdev_name, value)
        label = wheel_label(evdev_name, value)
        if button_id is None or label is None or not is_low_res_wheel_evdev(evdev_name):
            continue
        code = getattr(evdev.ecodes, evdev_name.upper(), None)
        buttons.append(
            ButtonDefinition(
                id=button_id,
                label=label,
                evdev=evdev_name,
                evdev_code=evdev_code_value(code),
                evdev_value=value,
                source=source_id or None,
                type="wheel",
                zone="wheel",
            )
        )
    return buttons


def build_gamepad_buttons(interfaces: Sequence[InterfaceInfo]) -> list[ButtonDefinition]:
    button_specs: dict[str, tuple[int, str | None]] = {}

    for iface in interfaces:
        raw_capabilities = iface.get("raw_capabilities") or {}
        if not isinstance(raw_capabilities, dict):
            continue

        source_id = str(iface.get("id", "") or "")
        for code in raw_capabilities.get(evdev.ecodes.EV_KEY, []):
            code_int = evdev_code_value(code)
            if code_int is None:
                continue
            evdev_name = capability_name(evdev.ecodes.EV_KEY, code_int)
            if not evdev_name:
                continue
            label = gamepad_button_label(evdev_name)
            canonical = canonical_gamepad_button_name(evdev_name)
            if canonical in button_specs or label is None:
                continue
            button_specs[canonical] = (code_int, source_id or None)

    buttons: list[ButtonDefinition] = []
    for canonical in ordered_gamepad_button_names(button_specs):
        code_int, source_id = button_specs[canonical]
        label = gamepad_button_label(canonical)
        if label is None:
            continue
        buttons.append(
            ButtonDefinition(
                id=canonical,
                label=label,
                evdev=canonical,
                evdev_code=code_int,
                source=source_id,
                type="gamepad",
            )
        )

    return buttons


def build_gamepad_analog_inputs(
    interfaces: Sequence[InterfaceInfo],
) -> list[AnalogInputDefinition]:
    axis_specs = {
        "left_stick": (
            "Left Stick",
            "stick",
            ((evdev.ecodes.ABS_X, "x"), (evdev.ecodes.ABS_Y, "y")),
        ),
        "right_stick": (
            "Right Stick",
            "stick",
            ((evdev.ecodes.ABS_RX, "x"), (evdev.ecodes.ABS_RY, "y")),
        ),
        "left_trigger": (
            "Left Trigger",
            "axis",
            ((evdev.ecodes.ABS_Z, "x"),),
        ),
        "right_trigger": (
            "Right Trigger",
            "axis",
            ((evdev.ecodes.ABS_RZ, "x"),),
        ),
    }
    discovered: dict[str, dict[int, tuple[str, str]]] = {}

    for iface in interfaces:
        if interface_has_role(iface, "motion"):
            continue
        raw_capabilities = iface.get("raw_capabilities") or {}
        if not isinstance(raw_capabilities, dict):
            continue
        source_id = str(iface.get("id", "") or "")
        abs_codes = {
            code_int
            for code in raw_capabilities.get(evdev.ecodes.EV_ABS, [])
            if (code_int := evdev_code_value(code)) is not None
        }
        for analog_id, (_label, _input_type, axes) in axis_specs.items():
            codes = tuple(code for code, _role in axes)
            if all(code in abs_codes for code in codes):
                discovered[analog_id] = {code: (role, source_id) for code, role in axes}

    analog_inputs: list[AnalogInputDefinition] = []
    for analog_id, (label, input_type, axes) in axis_specs.items():
        axis_data = discovered.get(analog_id)
        if axis_data is None:
            continue
        codes = tuple(code for code, _role in axes)
        source_id = next(
            (axis_data[code][1] for code in codes if axis_data[code][1]),
            None,
        )
        analog_inputs.append(
            AnalogInputDefinition(
                id=analog_id,
                label=label,
                type=input_type,
                source=source_id,
                axes=[
                    AnalogAxisDefinition(
                        role=axis_data[code][0],
                        evdev=capability_name(evdev.ecodes.EV_ABS, code) or str(code),
                        evdev_code=code,
                    )
                    for code in codes
                ],
            )
        )
    return analog_inputs


MOTION_DRIVER_LABELS = {
    "playstation": "PlayStation Motion Sensor",
    "hid-playstation": "PlayStation Motion Sensor",
    "nintendo": "Nintendo Motion Sensor",
    "hid-nintendo": "Nintendo Motion Sensor",
    "steam": "Steam Motion Sensor",
    "hid-steam": "Steam Motion Sensor",
}
STANDARD_GRAVITY = 9.80665


def build_motion_sensors(interfaces: Sequence[InterfaceInfo]) -> list[MotionSensorDefinition]:
    """Build calibrated motion sensor definitions from kernel evdev metadata."""
    sensors: list[MotionSensorDefinition] = []
    for iface in interfaces:
        if not interface_has_role(iface, "motion"):
            continue
        source_id = str(iface.get("id", "") or "")
        if not source_id:
            continue
        raw_capabilities = iface.get("raw_capabilities") or {}
        if not isinstance(raw_capabilities, dict):
            continue
        abs_values = raw_capabilities.get(evdev.ecodes.EV_ABS, [])
        abs_entries = {
            code: value for value in abs_values if (code := evdev_code_value(value)) is not None
        }
        driver = str(iface.get("driver", "") or "") or None
        gyro_axes = _motion_axes(
            abs_entries,
            (evdev.ecodes.ABS_RX, evdev.ecodes.ABS_RY, evdev.ecodes.ABS_RZ),
            driver=driver,
            kind="gyro",
        )
        accelerometer_axes = _motion_axes(
            abs_entries,
            (evdev.ecodes.ABS_X, evdev.ecodes.ABS_Y, evdev.ecodes.ABS_Z),
            driver=driver,
            kind="accelerometer",
        )
        if not gyro_axes and not accelerometer_axes:
            continue
        label = MOTION_DRIVER_LABELS.get(driver or "", "Motion Sensor")
        sensors.append(
            MotionSensorDefinition(
                id=f"motion_{len(sensors) + 1}",
                label=label,
                source=source_id,
                driver=driver,
                gyro_axes=gyro_axes,
                accelerometer_axes=accelerometer_axes,
                calibration_version=MOTION_NORMALIZATION_VERSION,
            )
        )
    return sensors


def _motion_axes(
    entries: Mapping[int, object],
    codes: Sequence[int],
    *,
    driver: str | None,
    kind: str,
) -> list[MotionAxisDefinition]:
    axes: list[MotionAxisDefinition] = []
    for code in codes:
        entry = entries.get(code)
        if entry is None:
            continue
        evdev_name = capability_name(evdev.ecodes.EV_ABS, code) or str(code)
        canonical = canonical_motion_axis(driver, kind, evdev_name)
        if canonical is None:
            continue
        role, invert = canonical
        resolution = _abs_resolution(entry)
        if kind == "gyro":
            # The kernel motion ABI reports angular resolution in units/degree/second.
            scale = math.radians(1.0 / resolution) if resolution > 0 else math.radians(1.0)
        else:
            # Accelerometer resolution is units/g; store canonical metres/second squared.
            scale = STANDARD_GRAVITY / resolution if resolution > 0 else STANDARD_GRAVITY
        axes.append(
            MotionAxisDefinition(
                role=role,
                evdev=evdev_name,
                evdev_code=code,
                scale=scale,
                invert=invert,
            )
        )
    role_order = {"pitch": 0, "yaw": 1, "roll": 2} if kind == "gyro" else {"x": 0, "y": 1, "z": 2}
    return sorted(axes, key=lambda axis: role_order[axis.role])


def _abs_resolution(entry: object) -> float:
    if not isinstance(entry, (tuple, list)) or len(entry) < 2:
        return 0.0
    value = entry[1]
    resolution = getattr(value, "resolution", 0)
    return float(resolution) if isinstance(resolution, (int, float)) else 0.0


def build_standard_keyboard_buttons(source_id: str) -> list[ButtonDefinition]:
    buttons: list[ButtonDefinition] = []
    standard_keys = [
        "KEY_ESC",
        "KEY_GRAVE",
        "KEY_1",
        "KEY_2",
        "KEY_3",
        "KEY_4",
        "KEY_5",
        "KEY_6",
        "KEY_7",
        "KEY_8",
        "KEY_9",
        "KEY_0",
        "KEY_MINUS",
        "KEY_EQUAL",
        "KEY_BACKSPACE",
        "KEY_TAB",
        "KEY_Q",
        "KEY_W",
        "KEY_E",
        "KEY_R",
        "KEY_T",
        "KEY_Y",
        "KEY_U",
        "KEY_I",
        "KEY_O",
        "KEY_P",
        "KEY_LEFTBRACE",
        "KEY_RIGHTBRACE",
        "KEY_BACKSLASH",
        "KEY_CAPSLOCK",
        "KEY_A",
        "KEY_S",
        "KEY_D",
        "KEY_F",
        "KEY_G",
        "KEY_H",
        "KEY_J",
        "KEY_K",
        "KEY_L",
        "KEY_SEMICOLON",
        "KEY_APOSTROPHE",
        "KEY_ENTER",
        "KEY_LEFTSHIFT",
        "KEY_Z",
        "KEY_X",
        "KEY_C",
        "KEY_V",
        "KEY_B",
        "KEY_N",
        "KEY_M",
        "KEY_COMMA",
        "KEY_DOT",
        "KEY_SLASH",
        "KEY_RIGHTSHIFT",
        "KEY_LEFTCTRL",
        "KEY_LEFTALT",
        "KEY_LEFTMETA",
        "KEY_SPACE",
        "KEY_RIGHTALT",
        "KEY_RIGHTCTRL",
        "KEY_RIGHTMETA",
        "KEY_SYSRQ",
        "KEY_SCROLLLOCK",
        "KEY_PAUSE",
        "KEY_INSERT",
        "KEY_HOME",
        "KEY_PAGEUP",
        "KEY_DELETE",
        "KEY_END",
        "KEY_PAGEDOWN",
        "KEY_UP",
        "KEY_LEFT",
        "KEY_DOWN",
        "KEY_RIGHT",
        "KEY_F1",
        "KEY_F2",
        "KEY_F3",
        "KEY_F4",
        "KEY_F5",
        "KEY_F6",
        "KEY_F7",
        "KEY_F8",
        "KEY_F9",
        "KEY_F10",
        "KEY_F11",
        "KEY_F12",
        "KEY_NUMLOCK",
        "KEY_KPSLASH",
        "KEY_KPASTERISK",
        "KEY_KPMINUS",
        "KEY_KP7",
        "KEY_KP8",
        "KEY_KP9",
        "KEY_KPPLUS",
        "KEY_KP4",
        "KEY_KP5",
        "KEY_KP6",
        "KEY_KP1",
        "KEY_KP2",
        "KEY_KP3",
        "KEY_KPENTER",
        "KEY_KP0",
        "KEY_KPDOT",
    ]

    for name in standard_keys:
        if name not in evdev.ecodes.ecodes:
            continue
        evdev_name = name.lower()
        buttons.append(
            ButtonDefinition(
                id=evdev_name,
                label=keyboard_label_from_evdev(name),
                evdev=evdev_name,
                source=source_id or None,
                type="key",
            )
        )

    return buttons


def keyboard_label_from_evdev(key_name: str) -> str:
    token = key_name[4:] if key_name.startswith("KEY_") else key_name
    token = token.replace("LEFT", "Left ").replace("RIGHT", "Right ")
    token = token.replace("CTRL", "Ctrl").replace("ALT", "Alt")
    token = token.replace("META", "Meta").replace("SHIFT", "Shift")
    token = token.replace("PAGEUP", "Page Up").replace("PAGEDOWN", "Page Down")
    token = token.replace("NUMLOCK", "Num Lock")
    return token.replace("_", " ").strip().title()
