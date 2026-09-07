import math
from collections.abc import Mapping, Sequence
from typing import Any

import evdev

from keymasq.common.controller_capabilities import (
    controller_button_name,
    controller_control_label,
    is_flight_stick,
)
from keymasq.common.devices import (
    canonical_gamepad_button_name,
    capability_name,
    evdev_code_value,
    is_by_id_path,
    is_low_res_wheel_evdev,
    normalize_input_classes,
    ordered_gamepad_button_names,
    primary_input_class,
    resolve_evdev_code,
    resolve_evdev_event_type,
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


def _controller_entries(iface: InterfaceInfo, event_type: int) -> dict[int, object]:
    raw = iface.get("raw_capabilities") or {}
    entries = {
        code: entry
        for entry in raw.get(event_type, [])
        if (code := evdev_code_value(entry)) is not None
    }
    for name in iface.get("capabilities", []):
        code = resolve_evdev_code(name)
        if code is not None and resolve_evdev_event_type(name) == event_type:
            entries.setdefault(code, code)
    return entries


def _controller_names(iface: InterfaceInfo) -> list[str]:
    return [
        controller_button_name(name)
        for code in _controller_entries(iface, evdev.ecodes.EV_KEY)
        if (name := capability_name(evdev.ecodes.EV_KEY, code)) is not None
    ]


def _unique_control_id(base: str, source: str | None, used: set[str]) -> str:
    candidate = base
    suffix = 2
    if candidate in used and source:
        candidate = f"{base}_{source}"
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def build_gamepad_buttons(interfaces: Sequence[InterfaceInfo]) -> list[ButtonDefinition]:
    buttons: list[ButtonDefinition] = []
    used: set[str] = set()
    for iface in interfaces:
        if interface_has_role(iface, "motion"):
            continue
        source = str(iface.get("id", "") or "") or None
        names = ordered_gamepad_button_names(_controller_names(iface))
        for name in names:
            canonical = canonical_gamepad_button_name(name)
            buttons.append(
                ButtonDefinition(
                    id=_unique_control_id(canonical, source, used),
                    label=controller_control_label(canonical),
                    evdev=canonical,
                    evdev_code=resolve_evdev_code(canonical),
                    source=source,
                    type="gamepad",
                )
            )
    return buttons


def build_gamepad_analog_inputs(
    interfaces: Sequence[InterfaceInfo],
) -> list[AnalogInputDefinition]:
    analogs: list[AnalogInputDefinition] = []
    used: set[str] = set()
    flight = any(
        is_flight_stick(_controller_names(iface))
        for iface in interfaces
        if not interface_has_role(iface, "motion")
    )
    for iface in interfaces:
        if interface_has_role(iface, "motion"):
            continue
        source = str(iface.get("id", "") or "") or None
        entries = _controller_entries(iface, evdev.ecodes.EV_ABS)
        # Touch and sensor axes do not describe controller controls.
        entries = {
            code: entry
            for code, entry in entries.items()
            if code < evdev.ecodes.ABS_MT_SLOT and code != evdev.ecodes.ABS_RESERVED
        }
        groups = [
            (
                "stick" if flight else "left_stick",
                "Stick" if flight else "Left Stick",
                (evdev.ecodes.ABS_X, evdev.ecodes.ABS_Y),
            )
        ]
        if not flight:
            groups.append(
                ("right_stick", "Right Stick", (evdev.ecodes.ABS_RX, evdev.ecodes.ABS_RY))
            )
        for index in range(4):
            groups.append(
                (
                    f"hat_{index}",
                    f"Hat {index + 1}",
                    (evdev.ecodes.ABS_HAT0X + index * 2, evdev.ecodes.ABS_HAT0Y + index * 2),
                )
            )

        def add(
            analog_id: str,
            label: str,
            codes: tuple[int, ...],
            *,
            entries: dict[int, object] = entries,
            flight: bool = flight,
            source: str | None = source,
        ) -> None:
            axes = []
            paired = len(codes) == 2
            for role, code in zip(("x", "y"), codes, strict=False):
                entry = entries.pop(code)
                info = entry[1] if isinstance(entry, (tuple, list)) and len(entry) > 1 else None
                minimum, maximum = getattr(info, "min", None), getattr(info, "max", None)
                valid = isinstance(minimum, int) and isinstance(maximum, int) and minimum < maximum
                name = capability_name(evdev.ecodes.EV_ABS, code) or str(code)
                # Current position is not calibration. Only conventional centered
                # controls get a midpoint; other single axes retain runtime learning.
                centered = paired or (
                    flight and name in {"abs_rx", "abs_ry", "abs_rz", "abs_rudder"}
                )
                midpoint = (
                    (minimum + maximum) // 2
                    if isinstance(minimum, int) and isinstance(maximum, int) and valid and centered
                    else None
                )
                axes.append(
                    AnalogAxisDefinition(
                        role=role,
                        evdev=name,
                        evdev_code=code,
                        minimum=minimum if valid else None,
                        maximum=maximum if valid else None,
                        center=midpoint if paired else None,
                        rest=midpoint if not paired else None,
                    )
                )
            analogs.append(
                AnalogInputDefinition(
                    id=_unique_control_id(analog_id, source, used),
                    label=label,
                    type="stick" if paired else "axis",
                    source=source,
                    axes=axes,
                )
            )

        for analog_id, label, codes in groups:
            if all(code in entries for code in codes):
                add(analog_id, label, codes)
        for code in sorted(entries):
            name = capability_name(evdev.ecodes.EV_ABS, code) or str(code)
            analog_id, label = name, controller_control_label(name)
            if not flight and code in {evdev.ecodes.ABS_Z, evdev.ecodes.ABS_RZ}:
                analog_id, label = (
                    ("left_trigger", "Left Trigger")
                    if code == evdev.ecodes.ABS_Z
                    else ("right_trigger", "Right Trigger")
                )
            elif flight and code == evdev.ecodes.ABS_RZ:
                label = "Twist"
            add(analog_id, label, (code,))
    return analogs


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
