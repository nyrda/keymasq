import re
from collections.abc import Callable, Iterable

from keymasq.common.devices import is_gamepad_button_name
from keymasq.common.model.core import DeviceType
from keymasq.common.model.hardware import HardwareConfig

KEYBOARD_LAYOUT_KEY_THRESHOLD = 40
_TRAILING_NUMBER_RE = re.compile(r"^(?P<prefix>.*?)(?P<number>\d+)\s*$")

POINTER_MAIN_BUTTON_IDS = {"btn_left", "btn_right", "btn_middle"}
POINTER_SCROLL_KEYWORDS = {"scroll", "wheel"}
POINTER_SIDE_BUTTON_IDS = {
    "btn_side",
    "btn_extra",
    "btn_4",
    "btn_forward",
    "btn_back",
}


def resolve_device_layout_kind(device: HardwareConfig) -> str:
    key_count = sum(1 for button in device.buttons if button.id.startswith("key_"))
    if key_count >= KEYBOARD_LAYOUT_KEY_THRESHOLD:
        return "keyboard"
    if any(evdev_device.device_type == DeviceType.GAMEPAD for evdev_device in device.evdev_devices):
        return "gamepad"
    if any(is_gamepad_button_name(button.evdev) for button in device.buttons):
        return "gamepad"
    return "mouse"


def group_pointer_controls[T](
    controls: Iterable[T],
    *,
    id_for_control: Callable[[T], str],
) -> tuple[list[T], list[T], list[T], list[T]]:
    main_buttons: list[T] = []
    scroll_buttons: list[T] = []
    side_buttons: list[T] = []
    extra_buttons: list[T] = []

    for control in controls:
        control_id = id_for_control(control).lower()
        if control_id in POINTER_MAIN_BUTTON_IDS:
            main_buttons.append(control)
        elif any(keyword in control_id for keyword in POINTER_SCROLL_KEYWORDS):
            scroll_buttons.append(control)
        elif control_id in POINTER_SIDE_BUTTON_IDS:
            side_buttons.append(control)
        else:
            extra_buttons.append(control)

    return main_buttons, scroll_buttons, side_buttons, extra_buttons


def label_sort_key(label: object) -> tuple[str, int, int, str]:
    text = str(label or "").strip()
    lowered = text.lower()
    match = _TRAILING_NUMBER_RE.match(lowered)
    if match:
        return (match.group("prefix").strip(), 0, int(match.group("number")), lowered)
    return (lowered, 1, 0, lowered)
