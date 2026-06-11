from keymasq.common.devices import gamepad_button_label


def label_from_evdev(evdev_name: str) -> str:
    gamepad_label = gamepad_button_label(evdev_name)
    if gamepad_label:
        return gamepad_label
    evdev_key = evdev_name.lower()
    if evdev_key == "btn_left":
        return "Left Click"
    if evdev_key == "btn_right":
        return "Right Click"
    if evdev_key == "btn_middle":
        return "Middle Click"
    if evdev_key == "btn_side":
        return "Back"
    if evdev_key == "btn_extra":
        return "Forward"
    if evdev_key == "rel_wheel":
        return "Scroll Wheel"
    if evdev_key == "rel_hwheel":
        return "Scroll Horizontal"

    token = evdev_name.upper()
    if token.startswith("KEY_"):
        token = token[4:]
    if token.startswith("BTN_"):
        token = token[4:]
    token = token.replace("LEFT", "Left ").replace("RIGHT", "Right ")
    token = token.replace("CTRL", "Ctrl").replace("ALT", "Alt")
    token = token.replace("META", "Meta").replace("SHIFT", "Shift")
    token = token.replace("PAGEUP", "Page Up").replace("PAGEDOWN", "Page Down")
    return token.replace("_", " ").strip().title()
