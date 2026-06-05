import logging
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Protocol, cast

import evdev

from keymasq.common.models import DeviceType

INPUT_CLASS_ORDER = ("mouse", "touchpad", "keyboard", "gamepad", "pointstick", "other")
KEYMASQ_DEVICE_PATH_PREFIX = "keymasq:"
log = logging.getLogger("keymasq.common.devices")
INPUT_CLASS_LABELS = {
    "mouse": "Mouse",
    "touchpad": "Touchpad",
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
_GAMEPAD_CONTROLLER_ONLY_ABS_CODES = _GAMEPAD_ABS_CODES - {
    evdev.ecodes.ABS_X,
    evdev.ecodes.ABS_Y,
}
_GAMEPAD_BUTTON_CODES = frozenset(
    {
        evdev.ecodes.BTN_SOUTH,
        evdev.ecodes.BTN_EAST,
        evdev.ecodes.BTN_NORTH,
        evdev.ecodes.BTN_WEST,
    }
)
_TOUCHPAD_MT_ABS_CODES = frozenset(
    {
        evdev.ecodes.ABS_MT_POSITION_X,
        evdev.ecodes.ABS_MT_POSITION_Y,
    }
)
LOW_RES_WHEEL_EVDEVS = frozenset({"rel_wheel", "rel_hwheel"})
WHEEL_BINDINGS = {
    ("rel_wheel", 1): ("wheel_up", "Scroll Up"),
    ("rel_wheel", -1): ("wheel_down", "Scroll Down"),
    ("rel_hwheel", -1): ("wheel_left", "Scroll Left"),
    ("rel_hwheel", 1): ("wheel_right", "Scroll Right"),
}
GAMEPAD_BUTTON_ORDER = (
    "btn_tl",
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


class _CapabilityDevice(Protocol):
    def input_props(self) -> Iterable[int]:
        ...

    def capabilities(self) -> Mapping[int, Sequence[object]]:
        ...
_GAMEPAD_BUTTON_ALIASES = {
    "btn_a": "btn_south",
    "btn_b": "btn_east",
    "btn_x": "btn_north",
    "btn_y": "btn_west",
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


def input_classes_include_gamepad(
    classes: Iterable[str | DeviceType] | None = None,
    primary: str | DeviceType | None = None,
) -> bool:
    return "gamepad" in normalize_input_classes(classes, primary)


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
    code_int = _capability_code_int(code)
    if code_int is None:
        return None
    code_name = evdev.ecodes.bytype.get(int(event_type), {}).get(code_int)
    if isinstance(code_name, tuple):
        code_name = code_name[0] if code_name else None
    if not isinstance(code_name, str):
        return None
    return code_name.lower()


def _capability_code_int(code: object) -> int | None:
    if isinstance(code, tuple):
        tuple_code = cast(tuple[object, ...], code)
        if not tuple_code:
            return None
        candidate = tuple_code[0]
    else:
        candidate = code
    if isinstance(candidate, int):
        return candidate
    if isinstance(candidate, str):
        try:
            return int(candidate)
        except ValueError:
            return None
    return None


def resolve_evdev_code(evdev_name: str | None) -> int | None:
    if not evdev_name:
        return None

    label = str(evdev_name).strip()
    if not label:
        return None

    for candidate in (label.upper(), label.lower()):
        if not hasattr(evdev.ecodes, candidate):
            continue
        code = getattr(evdev.ecodes, candidate)
        if isinstance(code, tuple):
            tuple_code = cast(tuple[object, ...], code)
            first = tuple_code[0] if tuple_code else None
            return first if isinstance(first, int) else None
        return int(code)

    return None


def resolve_evdev_event_type(evdev_name: str | None) -> int | None:
    if not evdev_name:
        return None

    label = str(evdev_name).strip().lower()
    if not label:
        return None

    if label.startswith(("key_", "btn_")):
        return evdev.ecodes.EV_KEY
    if label.startswith("rel_"):
        return evdev.ecodes.EV_REL
    if label.startswith("abs_"):
        return evdev.ecodes.EV_ABS
    return None


def normalize_evdev_binding_value(event_type: int, value: int | None) -> int | None:
    if value is None:
        return None
    normalized = int(value)
    if event_type == evdev.ecodes.EV_REL:
        if normalized == 0:
            return None
        return 1 if normalized > 0 else -1
    return normalized


def normalize_wheel_value(value: int | None) -> int | None:
    if value is None:
        return None
    normalized = int(value)
    if normalized == 0:
        return None
    return 1 if normalized > 0 else -1


def is_low_res_wheel_evdev(evdev_name: str | None) -> bool:
    return str(evdev_name or "").strip().lower() in LOW_RES_WHEEL_EVDEVS


def wheel_button_id(evdev_name: str | None, value: int | None) -> str | None:
    normalized_value = normalize_wheel_value(value)
    if normalized_value is None:
        return None
    spec = WHEEL_BINDINGS.get((str(evdev_name or "").strip().lower(), normalized_value))
    return spec[0] if spec else None


def wheel_label(evdev_name: str | None, value: int | None) -> str | None:
    normalized_value = normalize_wheel_value(value)
    if normalized_value is None:
        return None
    spec = WHEEL_BINDINGS.get((str(evdev_name or "").strip().lower(), normalized_value))
    return spec[1] if spec else None


def wheel_duplicate_key(
    evdev_name: str | None,
    code: int | None,
    value: int | None,
) -> tuple[str, int | None, int] | None:
    label = str(evdev_name or "").strip().lower()
    normalized_value = normalize_wheel_value(value)
    if label not in LOW_RES_WHEEL_EVDEVS or normalized_value is None:
        return None
    return (label, code, normalized_value)


def high_res_wheel_low_res_code(code: int) -> int | None:
    rel_wheel_hi_res = getattr(evdev.ecodes, "REL_WHEEL_HI_RES", None)
    rel_hwheel_hi_res = getattr(evdev.ecodes, "REL_HWHEEL_HI_RES", None)
    if rel_wheel_hi_res is not None and int(code) == int(rel_wheel_hi_res):
        return int(evdev.ecodes.REL_WHEEL)
    if rel_hwheel_hi_res is not None and int(code) == int(rel_hwheel_hi_res):
        return int(evdev.ecodes.REL_HWHEEL)
    return None


def capability_names_from_capabilities(caps: Mapping[int, Sequence[object]]) -> list[str]:
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


def gamepad_button_names_from_capabilities(caps: Mapping[int, Sequence[object]]) -> list[str]:
    names: list[str] = []
    for code in caps.get(evdev.ecodes.EV_KEY, []):
        name = capability_name(evdev.ecodes.EV_KEY, code)
        if is_gamepad_button_name(name):
            names.append(canonical_gamepad_button_name(name))
    return ordered_gamepad_button_names(names)


def detect_input_classes_from_capabilities(
    caps: Mapping[int, Sequence[object]],
    input_props: Iterable[int] | None = None,
) -> list[str]:
    key_codes = {
        code_int
        for code in caps.get(evdev.ecodes.EV_KEY, [])
        if (code_int := _capability_code_int(code)) is not None
    }
    rel_codes = {
        code_int
        for code in caps.get(evdev.ecodes.EV_REL, [])
        if (code_int := _capability_code_int(code)) is not None
    }
    abs_codes = {
        code_int
        for code in caps.get(evdev.ecodes.EV_ABS, [])
        if (code_int := _capability_code_int(code)) is not None
    }
    props = {int(prop) for prop in (input_props or [])}

    classes: list[str] = []
    has_touchpad_axes = bool(abs_codes & _TOUCHPAD_MT_ABS_CODES) or (
        evdev.ecodes.ABS_X in abs_codes and evdev.ecodes.ABS_Y in abs_codes
    )
    has_touchpad_contact = evdev.ecodes.BTN_TOOL_FINGER in key_codes or (
        evdev.ecodes.INPUT_PROP_POINTER in props and evdev.ecodes.BTN_TOUCH in key_codes
    )
    is_touchpad = evdev.ecodes.INPUT_PROP_BUTTONPAD in props or (
        has_touchpad_axes and has_touchpad_contact
    )

    has_gamepad_axes = bool(abs_codes & _GAMEPAD_ABS_CODES)
    has_controller_buttons = bool(key_codes & _GAMEPAD_BUTTON_CODES) or any(
        evdev.ecodes.BTN_JOYSTICK <= code < evdev.ecodes.BTN_DIGI for code in key_codes
    )
    is_plain_absolute_touch = evdev.ecodes.BTN_TOUCH in key_codes and not has_controller_buttons
    if has_gamepad_axes and not is_touchpad and not is_plain_absolute_touch:
        classes.append("gamepad")

    if is_touchpad:
        classes.append("touchpad")

    has_mouse_motion = evdev.ecodes.REL_X in rel_codes and evdev.ecodes.REL_Y in rel_codes
    has_mouse_buttons = any(
        evdev.ecodes.BTN_MOUSE <= code < evdev.ecodes.BTN_JOYSTICK for code in key_codes
    )
    if not is_touchpad and (has_mouse_motion or has_mouse_buttons):
        classes.append("mouse")

    if any(code < evdev.ecodes.BTN_MISC for code in key_codes):
        classes.append("keyboard")

    if evdev.ecodes.INPUT_PROP_POINTING_STICK in props:
        classes.append("pointstick")

    return normalize_input_classes(classes)


def detect_input_classes(device: _CapabilityDevice) -> list[str]:
    try:
        input_props = list(device.input_props())
    except (OSError, RuntimeError, AttributeError):
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
        if "touchpad" in normalized:
            return "touchpad"
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
        if "touchpad" in normalized:
            return "touchpad"
        if {"mouse", "pointstick"} & normalized:
            return "mouse"
        if "gamepad" in normalized:
            return "gamepad"

    for label in ("keyboard", "touchpad", "mouse", "gamepad"):
        if label in normalized:
            return label
    return "other"


def resolve_stable_path(event_path: str) -> str:
    if not event_path:
        return event_path
    return _resolve_stable_path_cached(str(event_path))


def is_keymasq_device_path(path: str) -> bool:
    return str(path or "").strip().lower().startswith(KEYMASQ_DEVICE_PATH_PREFIX)


def make_keymasq_device_path(vendor_id: str, product_id: str) -> str:
    return (
        f"{KEYMASQ_DEVICE_PATH_PREFIX}"
        f"{str(vendor_id or '').strip().lower()}:{str(product_id or '').strip().lower()}"
    )


def parse_keymasq_device_path(path: str) -> tuple[str, str] | None:
    normalized = str(path or "").strip().lower()
    if not normalized.startswith(KEYMASQ_DEVICE_PATH_PREFIX):
        return None
    parts = normalized.removeprefix(KEYMASQ_DEVICE_PATH_PREFIX).split(":")
    if len(parts) != 2:
        return None
    vendor_id, product_id = (part.strip() for part in parts)
    if not re.fullmatch(r"[0-9a-f]{1,4}", vendor_id) or not re.fullmatch(
        r"[0-9a-f]{1,4}", product_id
    ):
        return None
    return vendor_id.zfill(4), product_id.zfill(4)


def parse_hardware_model_id(value: object) -> tuple[str, str] | None:
    normalized = str(value or "").strip().lower()
    if normalized.startswith(KEYMASQ_DEVICE_PATH_PREFIX):
        normalized = normalized.removeprefix(KEYMASQ_DEVICE_PATH_PREFIX)
    model_text = normalized.split("@", 1)[0]
    parts = model_text.split(":", 1)
    if len(parts) != 2:
        return None
    vendor_id, product_id = (part.strip() for part in parts)
    if not _is_hardware_hex_id(vendor_id) or not _is_hardware_hex_id(product_id):
        return None
    return vendor_id.zfill(4), product_id.zfill(4)


def hardware_model_id_key(value: object) -> str | None:
    parsed = parse_hardware_model_id(value)
    if parsed is None:
        return None
    vendor_id, product_id = parsed
    return f"{vendor_id}:{product_id}"


def _is_hardware_hex_id(value: str) -> bool:
    return 1 <= len(value) <= 4 and all(char in "0123456789abcdef" for char in value)


def is_by_id_path(path: str) -> bool:
    return "/dev/input/by-id/" in str(path or "")


def config_path_for_detected_event(event_path: str, vendor_id: str, product_id: str) -> str:
    stable_path = resolve_stable_path(event_path)
    if is_by_id_path(stable_path):
        return stable_path
    return make_keymasq_device_path(vendor_id, product_id)


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

    list_devices = cast(Callable[[], list[str]], evdev.list_devices)
    for path in list_devices():
        device = None
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
                        "phys": str(getattr(device, "phys", "") or ""),
                    }
                )
        except OSError as exc:
            log.debug("Skipping evdev interface %s during VID:PID probe: %s", path, exc)
        except Exception:
            log.exception("Unexpected failure probing evdev interface %s for VID:PID match", path)
        finally:
            if device is not None:
                try:
                    device.close()
                except (OSError, RuntimeError) as exc:
                    log.debug("Failed to close evdev interface %s: %s", path, exc)

    return interfaces
