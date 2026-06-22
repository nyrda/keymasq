# pyright: reportUnusedFunction=false
from __future__ import annotations

import evdev

from keymasq.common.devices import resolve_evdev_code
from keymasq.common.models import (
    REPEAT_CATEGORY_GAMEPAD,
    REPEAT_CATEGORY_KEYBOARD,
    REPEAT_CATEGORY_MACRO,
    REPEAT_CATEGORY_MOUSE,
    REPEAT_CATEGORY_SPECIAL,
)
from keymasq.gui.widgets.docs_links import actions_docs_url
from keymasq.gui.widgets.input_picker_shared import GAMEPAD_BUTTONS

from . import compat

_PROFILE_LIFETIME_PRESETS_ENABLE: tuple[tuple[str, str], ...] = (
    ("until_changed", "Persistent"),
    ("while_trigger_active", "While trigger is held"),
    ("after_one_action", "One-shot"),
    ("custom", "Custom"),
)
_PROFILE_LIFETIME_PRESETS_TOGGLE: tuple[tuple[str, str], ...] = (
    ("until_changed", "Persistent"),
    ("after_one_action", "One-shot"),
    ("custom", "Custom"),
)


def _resolve_gamepad_button_target(raw: str) -> str | None:
    """Resolve free-form button input to a canonical evdev button name.

    Accepts an evdev button name (e.g. ``btn_c``, ``btn_trigger_happy1``) or a
    numeric key code (decimal or ``0x``-prefixed). Returns a lowercase ``btn_*``
    name, or ``None`` when the input is not a recognized button code.
    """
    text = (raw or "").strip().lower()
    if not text:
        return None

    if text.startswith("btn_"):
        return text if resolve_evdev_code(text) is not None else None

    try:
        code = int(text, 0)
    except ValueError:
        return None

    names = evdev.ecodes.BTN.get(code)
    if isinstance(names, (list, tuple)):
        # evdev orders aliases oldest-first; the last name is the canonical
        # modern label (e.g. BTN_SOUTH over BTN_A).
        names = names[-1] if names else None
    if isinstance(names, str) and names.startswith("BTN_"):
        return names.lower()
    return None


def _resolve_gamepad_axis_target(raw: str) -> str | None:
    """Resolve free-form axis input to a canonical evdev ABS name.

    Accepts an evdev axis name (e.g. ``abs_hat0x``, ``abs_throttle``) or a
    numeric ABS code (decimal or ``0x``-prefixed). Returns a lowercase ``abs_*``
    name, or ``None`` when the input is not a recognized axis code.
    """
    text = (raw or "").strip().lower()
    if not text:
        return None

    if text.startswith("abs_"):
        return text if resolve_evdev_code(text) is not None else None

    try:
        code = int(text, 0)
    except ValueError:
        return None

    names = evdev.ecodes.ABS.get(code)
    if isinstance(names, (list, tuple)):
        names = names[-1] if names else None
    if isinstance(names, str) and names.startswith("ABS_"):
        return names.lower()
    return None


KEYBOARD_LAYOUT = [
    ["Esc", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12"],
    ["`", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "-", "=", "Bspc"],
    ["Tab", "Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P", "[", "]", "\\"],
    ["Caps", "A", "S", "D", "F", "G", "H", "J", "K", "L", ";", "'", "Enter"],
    ["LShift", "Z", "X", "C", "V", "B", "N", "M", ",", ".", "/", "RShift"],
    ["LCtrl", "LMeta", "LAlt", "Space", "RAlt", "RMeta", "Fn", "Menu", "RCtrl"],
]

KEY_TO_EVDEV = {
    "Esc": "key_esc",
    "F1": "key_f1",
    "F2": "key_f2",
    "F3": "key_f3",
    "F4": "key_f4",
    "F5": "key_f5",
    "F6": "key_f6",
    "F7": "key_f7",
    "F8": "key_f8",
    "F9": "key_f9",
    "F10": "key_f10",
    "F11": "key_f11",
    "F12": "key_f12",
    "`": "key_grave",
    "1": "key_1",
    "2": "key_2",
    "3": "key_3",
    "4": "key_4",
    "5": "key_5",
    "6": "key_6",
    "7": "key_7",
    "8": "key_8",
    "9": "key_9",
    "0": "key_0",
    "-": "key_minus",
    "=": "key_equal",
    "Bspc": "key_backspace",
    "Tab": "key_tab",
    "Q": "key_q",
    "W": "key_w",
    "E": "key_e",
    "R": "key_r",
    "T": "key_t",
    "Y": "key_y",
    "U": "key_u",
    "I": "key_i",
    "O": "key_o",
    "P": "key_p",
    "[": "key_leftbrace",
    "]": "key_rightbrace",
    "\\": "key_backslash",
    "Caps": "key_capslock",
    "A": "key_a",
    "S": "key_s",
    "D": "key_d",
    "F": "key_f",
    "G": "key_g",
    "H": "key_h",
    "J": "key_j",
    "K": "key_k",
    "L": "key_l",
    ";": "key_semicolon",
    "'": "key_apostrophe",
    "Enter": "key_enter",
    "LShift": "key_leftshift",
    "Z": "key_z",
    "X": "key_x",
    "C": "key_c",
    "V": "key_v",
    "B": "key_b",
    "N": "key_n",
    "M": "key_m",
    ",": "key_comma",
    ".": "key_dot",
    "/": "key_slash",
    "RShift": "key_rightshift",
    "LCtrl": "key_leftctrl",
    "LMeta": "key_leftmeta",
    "LAlt": "key_leftalt",
    "Space": "key_space",
    "RAlt": "key_rightalt",
    "RMeta": "key_rightmeta",
    "Fn": None,
    "Menu": "key_menu",
    "RCtrl": "key_rightctrl",
    "F13": "key_f13",
    "F14": "key_f14",
    "F15": "key_f15",
    "F16": "key_f16",
    "F17": "key_f17",
    "F18": "key_f18",
    "F19": "key_f19",
    "F20": "key_f20",
    "F21": "key_f21",
    "F22": "key_f22",
    "F23": "key_f23",
    "F24": "key_f24",
    "Ins": "key_insert",
    "Del": "key_delete",
    "Home": "key_home",
    "End": "key_end",
    "PgUp": "key_pageup",
    "PgDn": "key_pagedown",
    "Up": "key_up",
    "Down": "key_down",
    "Left": "key_left",
    "Right": "key_right",
    "NumLk": "key_numlock",
    "KP/": "key_kpslash",
    "KP*": "key_kpasterisk",
    "KP-": "key_kpminus",
    "KP7": "key_kp7",
    "KP8": "key_kp8",
    "KP9": "key_kp9",
    "KP+": "key_kpplus",
    "KP4": "key_kp4",
    "KP5": "key_kp5",
    "KP6": "key_kp6",
    "KP1": "key_kp1",
    "KP2": "key_kp2",
    "KP3": "key_kp3",
    "KPEnter": "key_kpenter",
    "KP0": "key_kp0",
    "KP.": "key_kpdot",
    "Mute": "key_mute",
    "Volume Down": "key_volumedown",
    "Volume Up": "key_volumeup",
    "Mic Mute": "key_micmute",
    "Play/Pause": "key_playpause",
    "Play": "key_play",
    "Pause": "key_pause",
    "Stop": "key_stop",
    "Previous Track": "key_previoussong",
    "Next Track": "key_nextsong",
}

KEY_WIDTHS = {
    "Esc": 1,
    "Bspc": 2,
    "Tab": 1.5,
    "\\": 1.5,
    "Caps": 1.75,
    "Enter": 2.25,
    "LShift": 2.25,
    "RShift": 2.75,
    "LCtrl": 1.25,
    "LMeta": 1.25,
    "LAlt": 1.25,
    "Space": 6.25,
    "RAlt": 1.25,
    "RMeta": 1.25,
    "Fn": 1.25,
    "Menu": 1.25,
    "RCtrl": 1.25,
}

F_EXTRA = ["F13", "F14", "F15", "F16", "F17", "F18", "F19", "F20", "F21", "F22", "F23", "F24"]

MPRIS_MEDIA_GROUPS = [
    (
        "Player Controls",
        [
            ("Previous", "previous", "media-skip-backward-symbolic"),
            ("Play/Pause", "play_pause", "media-playback-start-symbolic"),
            ("Next", "next", "media-skip-forward-symbolic"),
            ("Stop", "stop", "media-playback-stop-symbolic"),
            ("Play", "play", "media-playback-start-symbolic"),
            ("Pause", "pause", "media-playback-pause-symbolic"),
        ],
    )
]

MEDIA_KEY_GROUPS = [
    (
        "Playback",
        [
            ("Previous", "key_previoussong", "media-skip-backward-symbolic"),
            ("Play/Pause", "key_playpause", "media-playback-start-symbolic"),
            ("Next", "key_nextsong", "media-skip-forward-symbolic"),
            ("Stop", "key_stop", "media-playback-stop-symbolic"),
            ("Play", "key_play", "media-playback-start-symbolic"),
            ("Pause", "key_pause", "media-playback-pause-symbolic"),
        ],
    ),
]
SYSTEM_KEY_GROUPS = [
    (
        "System Keys",
        [
            ("Vol Down", "key_volumedown", "audio-volume-low-symbolic"),
            ("Vol Up", "key_volumeup", "audio-volume-high-symbolic"),
            ("Mute", "key_mute", "audio-volume-muted-symbolic"),
            ("Mic Mute", "key_micmute", "microphone-sensitivity-muted-symbolic"),
            ("Bright Down", "key_brightnessdown", "display-brightness-symbolic"),
            ("Bright Up", "key_brightnessup", "display-brightness-symbolic"),
        ],
    )
]
MEDIA_KEY_TARGETS = {
    evdev_id for _title, buttons in MEDIA_KEY_GROUPS for _label, evdev_id, _icon_name in buttons
}
SYSTEM_KEY_TARGETS = {
    evdev_id for _title, buttons in SYSTEM_KEY_GROUPS for _label, evdev_id, _icon_name in buttons
}


def _keyboard_target_allows_rapidfire(evdev_name: str) -> bool:
    return evdev_name not in MEDIA_KEY_TARGETS


def _keyboard_target_allows_tap(evdev_name: str) -> bool:
    return evdev_name not in MEDIA_KEY_TARGETS

ACTION_DOC_LINKS = {
    "analog_presets": ("analog-controls", "Analog Controls"),
    "analog_control": ("analog-controls", "Analog Controls"),
    "special": ("special", "Special"),
    "keyboard": ("keyboard", "Keyboard"),
    "type": ("type-macro-inline-controls", "Type"),
    "navigation": ("navigation", "Navigation"),
    "media": ("media", "Media"),
    "mouse": ("mouse", "Mouse"),
    "gamepad": ("gamepad", "Gamepad"),
    "hyprland": ("hyprland", "Hyprland"),
    "niri": ("niri", "Niri"),
    "kde": ("kde-plasma", "KDE Plasma"),
    "gnome": ("gnome", "GNOME"),
    "superkey": ("super-keys", "Super Keys"),
    "macro": ("macro", "Macro"),
    "profile": ("profile", "Profile"),
    "exec": ("execute-shell-command", "Command"),
}

REPEAT_RAPIDFIRE_TOOLTIP = (
    "Rapidfire repeats only remembered keyboard keys, mouse buttons, mouse wheel actions, "
    "and gamepad buttons. Other remembered actions run once."
)
DEFAULT_RAPIDFIRE_TOOLTIP = "Repeatedly send the mapped action while the button is held"
REPEAT_CATEGORY_OPTIONS = (
    (REPEAT_CATEGORY_KEYBOARD, "Keys", "Repeat remembered keyboard actions"),
    (REPEAT_CATEGORY_MOUSE, "Mouse", "Repeat remembered mouse button and wheel actions"),
    (REPEAT_CATEGORY_GAMEPAD, "Gamepad", "Repeat remembered gamepad actions"),
    (REPEAT_CATEGORY_MACRO, "Macros", "Repeat remembered macro actions"),
    (
        REPEAT_CATEGORY_SPECIAL,
        "Other",
        "Repeat remembered Keymasq special actions that do not fit the other groups, "
        "including mouse movement, shell commands, and recording or playback controls.",
    ),
)

EVDEV_TO_KEY = {v: k for k, v in KEY_TO_EVDEV.items()}
EVDEV_TO_GAMEPAD = {v: k for k, v in GAMEPAD_BUTTONS.items()}


def _actions_docs_url(anchor: str) -> str:
    return actions_docs_url(anchor, version=compat.package_version())


_GAMEPAD_AXIS_CUSTOM_SLOT = "custom"
