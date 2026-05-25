from collections.abc import Callable, Iterable

from keymasq.common.devices import is_gamepad_button_name
from keymasq.common.models import DeviceType, HardwareConfig

KEYBOARD_LAYOUT_KEY_THRESHOLD = 40

POINTER_MAIN_BUTTON_IDS = {"btn_left", "btn_right", "btn_middle"}
POINTER_SCROLL_KEYWORDS = {"scroll", "wheel"}
POINTER_SIDE_BUTTON_IDS = {
    "btn_side",
    "btn_extra",
    "btn_4",
    "btn_forward",
    "btn_back",
}


def device_layout_kind(device: HardwareConfig) -> str:
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
