from __future__ import annotations

from collections.abc import Iterable

from keymasq.common.devices import canonical_gamepad_button_name

COMBO_PULSE_EVDEVS = frozenset(
    {
        "wheel_up",
        "wheel_down",
        "wheel_left",
        "wheel_right",
    }
)

EMERGENCY_CANCEL_COMBO_LABEL = "Ctrl+Alt+Esc"
EMERGENCY_CANCEL_COMBO_EVDEVS = ("ctrl", "alt", "key_esc")
EMERGENCY_CANCEL_COMBO_EVDEV_SET = frozenset(EMERGENCY_CANCEL_COMBO_EVDEVS)

GENERIC_MODIFIER_MAP = {
    "key_leftctrl": "ctrl",
    "key_rightctrl": "ctrl",
    "key_leftshift": "shift",
    "key_rightshift": "shift",
    "key_leftalt": "alt",
    "key_rightalt": "alt",
    "key_leftmeta": "meta",
    "key_rightmeta": "meta",
}


def normalize_combo_evdev(evdev_name: str) -> str:
    token = str(evdev_name or "").strip().lower()
    if not token:
        return ""
    return canonical_gamepad_button_name(GENERIC_MODIFIER_MAP.get(token, token))


def is_combo_pulse_evdev(evdev_name: str) -> bool:
    return normalize_combo_evdev(evdev_name) in COMBO_PULSE_EVDEVS


def is_emergency_cancel_combo_evdevs(evdev_names: Iterable[object]) -> bool:
    normalized = {normalize_combo_evdev(str(evdev_name or "")) for evdev_name in evdev_names}
    normalized.discard("")
    return frozenset(normalized) == EMERGENCY_CANCEL_COMBO_EVDEV_SET


def normalize_combo_restore_keys(keys: Iterable[object]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for key in keys:
        token = normalize_combo_evdev(str(key or ""))
        if not token or token in seen:
            continue
        seen.add(token)
        normalized.append(token)
    return normalized
