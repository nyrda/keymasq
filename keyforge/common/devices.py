import os
import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import evdev

from keyforge.common.models import DeviceType

INPUT_CLASS_ORDER = ("mouse", "keyboard", "gamepad", "pointstick", "other")
INPUT_CLASS_LABELS = {
    "mouse": "Mouse",
    "keyboard": "Keyboard",
    "gamepad": "Gamepad",
    "pointstick": "Pointstick",
    "other": "Other",
}
_GAMEPAD_ABS_CODES = frozenset(
    {
        evdev.ecodes.ABS_X,
        evdev.ecodes.ABS_Y,
        evdev.ecodes.ABS_Z,
        evdev.ecodes.ABS_RX,
        evdev.ecodes.ABS_RY,
        evdev.ecodes.ABS_RZ,
        evdev.ecodes.ABS_THROTTLE,
        evdev.ecodes.ABS_RUDDER,
        evdev.ecodes.ABS_HAT0X,
        evdev.ecodes.ABS_HAT0Y,
        evdev.ecodes.ABS_GAS,
        evdev.ecodes.ABS_BRAKE,
    }
)
_GAMEPAD_BUTTON_CODES = frozenset(
    {
        evdev.ecodes.BTN_SOUTH,
        evdev.ecodes.BTN_EAST,
        evdev.ecodes.BTN_NORTH,
        evdev.ecodes.BTN_WEST,
    }
)
GAMEPAD_BUTTON_ORDER = (
    "btn_tl2",
    "btn_tl",
    "btn_tr2",
    "btn_tr",
    "btn_select",
    "btn_mode",
    "btn_start",
    "btn_north",
    "btn_west",
    "btn_east",
    "btn_south",
    "btn_thumbl",
    "btn_thumbr",
    "btn_dpad_up",
    "btn_dpad_left",
    "btn_dpad_right",
    "btn_dpad_down",
)
_GAMEPAD_BUTTON_LABELS = {
    "btn_south": "A",
    "btn_east": "B",
    "btn_north": "X",
    "btn_west": "Y",
    "btn_tl": "LB",
    "btn_tr": "RB",
    "btn_tl2": "LT",
    "btn_tr2": "RT",
    "btn_select": "Select",
    "btn_start": "Start",
    "btn_mode": "Guide",
    "btn_thumbl": "LS",
    "btn_thumbr": "RS",
    "btn_dpad_up": "D-Up",
    "btn_dpad_down": "D-Down",
    "btn_dpad_left": "D-Left",
    "btn_dpad_right": "D-Right",
}
_GAMEPAD_BUTTON_ALIASES = {
    "btn_a": "btn_south",
    "btn_b": "btn_east",
    "btn_x": "btn_north",
    "btn_y": "btn_west",
    "btn_lt": "btn_tl2",
    "btn_rt": "btn_tr2",
}


def normalize_input_classes(
    classes: Iterable[str | DeviceType] | None,
    primary: str | DeviceType | None = None,
) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []

    def _append(value: str | DeviceType | None) -> None:
        if value is None:
            return
        raw = value.value if isinstance(value, DeviceType) else str(value or "")
        label = raw.strip().lower()
        if label not in INPUT_CLASS_ORDER or label in seen:
            return
        seen.add(label)
        normalized.append(label)

    if classes is not None:
        for value in classes:
            _append(value)
    _append(primary)

    if not normalized:
        return ["other"]

    if len(normalized) > 1 and "other" in seen:
        normalized = [value for value in normalized if value != "other"]

    return sorted(normalized, key=lambda value: INPUT_CLASS_ORDER.index(value))


def input_class_label(value: str | DeviceType) -> str:
    raw = value.value if isinstance(value, DeviceType) else str(value or "")
    label = raw.strip().lower()
    return INPUT_CLASS_LABELS.get(label, label.title())


def canonical_gamepad_button_name(evdev_name: str | None) -> str:
    label = str(evdev_name or "").strip().lower()
    return _GAMEPAD_BUTTON_ALIASES.get(label, label)


def is_gamepad_button_name(evdev_name: str | None) -> bool:
    return canonical_gamepad_button_name(evdev_name) in _GAMEPAD_BUTTON_LABELS


def gamepad_button_label(evdev_name: str | None) -> str | None:
    canonical = canonical_gamepad_button_name(evdev_name)
    return _GAMEPAD_BUTTON_LABELS.get(canonical)


def capability_name(event_type: int, code: object) -> str | None:
    code_int = int(code[0] if isinstance(code, tuple) else code)
    code_name = evdev.ecodes.bytype.get(int(event_type), {}).get(code_int)
    if isinstance(code_name, tuple):
        code_name = code_name[0] if code_name else None
    if not isinstance(code_name, str):
        return None
    return code_name.lower()


def capability_names_from_capabilities(caps: dict[int, list[object]]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()

    for event_type in (evdev.ecodes.EV_KEY, evdev.ecodes.EV_REL, evdev.ecodes.EV_ABS):
        for code in caps.get(event_type, []):
            name = capability_name(event_type, code)
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)

    return names


def ordered_gamepad_button_names(names: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()

    for name in names:
        canonical = canonical_gamepad_button_name(name)
        if canonical in seen:
            continue
        seen.add(canonical)
        normalized.append(canonical)

    order_map = {name: idx for idx, name in enumerate(GAMEPAD_BUTTON_ORDER)}
    return sorted(normalized, key=lambda name: (order_map.get(name, 999), name))


def gamepad_button_names_from_capabilities(caps: dict[int, list[object]]) -> list[str]:
    names: list[str] = []
    for code in caps.get(evdev.ecodes.EV_KEY, []):
        name = capability_name(evdev.ecodes.EV_KEY, code)
        if is_gamepad_button_name(name):
            names.append(canonical_gamepad_button_name(name))
    return ordered_gamepad_button_names(names)


def detect_input_classes_from_capabilities(
    caps: dict[int, list[object]],
    input_props: Iterable[int] | None = None,
) -> list[str]:
    key_codes = {
        int(code[0] if isinstance(code, tuple) else code)
        for code in caps.get(evdev.ecodes.EV_KEY, [])
    }
    rel_codes = {
        int(code[0] if isinstance(code, tuple) else code)
        for code in caps.get(evdev.ecodes.EV_REL, [])
    }
    abs_codes = {
        int(code[0] if isinstance(code, tuple) else code)
        for code in caps.get(evdev.ecodes.EV_ABS, [])
    }
    props = {int(prop) for prop in (input_props or [])}

    classes: list[str] = []

    has_gamepad_axes = bool(abs_codes & _GAMEPAD_ABS_CODES)
    has_gamepad_buttons = bool(key_codes & _GAMEPAD_BUTTON_CODES)
    if has_gamepad_axes and has_gamepad_buttons:
        classes.append("gamepad")

    has_mouse_motion = evdev.ecodes.REL_X in rel_codes and evdev.ecodes.REL_Y in rel_codes
    has_mouse_buttons = any(
        evdev.ecodes.BTN_MOUSE <= code < evdev.ecodes.BTN_JOYSTICK for code in key_codes
    )
    if has_mouse_motion or has_mouse_buttons:
        classes.append("mouse")

    if any(code < evdev.ecodes.BTN_MISC for code in key_codes):
        classes.append("keyboard")

    if evdev.ecodes.INPUT_PROP_POINTING_STICK in props:
        classes.append("pointstick")

    return normalize_input_classes(classes)


def detect_input_classes(device: evdev.InputDevice) -> list[str]:
    try:
        input_props = device.input_props()
    except Exception:
        input_props = []
    return detect_input_classes_from_capabilities(device.capabilities(), input_props)


def primary_input_class(classes: Iterable[str | DeviceType] | None) -> DeviceType:
    normalized = normalize_input_classes(classes)
    if "gamepad" in normalized:
        return DeviceType.GAMEPAD
    if "mouse" in normalized or "pointstick" in normalized:
        return DeviceType.MOUSE
    if "keyboard" in normalized:
        return DeviceType.KEYBOARD
    return DeviceType.OTHER


def classify_event_device_type(
    event: evdev.InputEvent,
    classes: Iterable[str | DeviceType] | None,
) -> str:
    normalized = set(normalize_input_classes(classes))
    event_type = int(event.type)
    event_code = int(event.code)

    if event_type == evdev.ecodes.EV_KEY:
        if event_code < evdev.ecodes.BTN_MISC and "keyboard" in normalized:
            return "keyboard"
        if (
            evdev.ecodes.BTN_MOUSE <= event_code < evdev.ecodes.BTN_JOYSTICK
            and {"mouse", "pointstick"} & normalized
        ):
            return "mouse"
        if "gamepad" in normalized:
            return "gamepad"
        if {"mouse", "pointstick"} & normalized:
            return "mouse"
        if "keyboard" in normalized:
            return "keyboard"

    if event_type == evdev.ecodes.EV_REL:
        if {"mouse", "pointstick"} & normalized:
            return "mouse"

    if event_type == evdev.ecodes.EV_ABS:
        if "gamepad" in normalized and event_code in _GAMEPAD_ABS_CODES:
            return "gamepad"
        if {"mouse", "pointstick"} & normalized:
            return "mouse"
        if "gamepad" in normalized:
            return "gamepad"

    for label in ("keyboard", "mouse", "gamepad"):
        if label in normalized:
            return label
    return "other"


def resolve_stable_path(event_path: str) -> str:
    if not event_path:
        return event_path
    return _resolve_stable_path_cached(str(event_path))


def clear_device_path_cache() -> None:
    _resolve_stable_path_cached.cache_clear()


@lru_cache(maxsize=512)
def _resolve_stable_path_cached(event_path: str) -> str:
    """
    Resolve an event device path to its stable by-id path.

    Args:
        event_path: Path like /dev/input/event4 or event4

    Returns:
        Stable by-id path like /dev/input/by-id/usb-Vendor_Device-event-mouse
        or the original path if no by-id symlink exists
    """
    path = Path(event_path)
    if path.name.startswith("event"):
        full_path = Path("/dev/input") / path.name
    else:
        full_path = path

    by_id_dir = Path("/dev/input/by-id")
    if not by_id_dir.exists():
        return event_path

    target_name = full_path.name

    for symlink in by_id_dir.iterdir():
        try:
            if symlink.is_symlink():
                link_target = os.readlink(symlink)
                if link_target.endswith(target_name) or link_target == f"../{target_name}":
                    return str(symlink)
        except OSError:
            continue

    return event_path


def get_interface_id(stable_path: str) -> str:
    """
    Extract an interface ID from a stable by-id path.

    Examples:
        usb-Razer_...-event-mouse -> "mouse"
        usb-Razer_...-if01-event-mouse -> "if01_mouse"
        usb-Razer_...-if02-event-kbd -> "if02_kbd"

    Args:
        stable_path: A by-id stable path

    Returns:
        Interface identifier string
    """
    path = Path(stable_path)
    name = path.name

    event_suffix = ""
    event_marker = "-event-"
    if event_marker in name:
        event_suffix = name.split(event_marker, 1)[1]
        event_suffix = re.sub(r"[^a-zA-Z0-9]+", "_", event_suffix).strip("_").lower()

    if "-if" in name:
        start = name.find("-if") + 1
        end = name.find("-event", start)
        if end == -1:
            end = name.find("-", start + 3)
        if end > start:
            interface_id = name[start:end]
            if event_suffix:
                return f"{interface_id}_{event_suffix}"
            return interface_id

    if name.startswith("event"):
        return name

    if "-event-mouse" in name:
        return "mouse"

    if "-event-kbd" in name:
        return "kbd"

    if "-event-joystick" in name:
        return "joystick"

    if "-event" in name:
        start = name.find("-event") + 1
        end = name.find(".", start)
        if end == -1:
            return name[start:]
        return name[start:end]

    return "default"


def find_all_interfaces(vendor_id: str, product_id: str) -> list[dict[str, str]]:
    """
    Find all evdev interfaces for a device by VID:PID.

    Args:
        vendor_id: Vendor ID as hex string (e.g., "1532")
        product_id: Product ID as hex string (e.g., "00b4")

    Returns:
        List of dicts with 'path', 'stable_path', 'id', 'name' for each interface
    """
    import evdev

    clear_device_path_cache()
    interfaces: list[dict[str, str]] = []
    vid = vendor_id.lower()
    pid = product_id.lower()

    for path in evdev.list_devices():
        try:
            device = evdev.InputDevice(path)
            info = device.info

            if f"{info.vendor:04x}" == vid and f"{info.product:04x}" == pid:
                stable_path = resolve_stable_path(path)
                interface_id = get_interface_id(stable_path)

                interfaces.append(
                    {
                        "path": path,
                        "stable_path": stable_path,
                        "id": interface_id,
                        "name": device.name,
                    }
                )
        except Exception:
            continue

    return interfaces
