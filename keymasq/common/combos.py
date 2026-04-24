from __future__ import annotations

from collections.abc import Iterable

COMBO_PULSE_EVDEVS = frozenset(
    {
        "wheel_up",
        "wheel_down",
        "wheel_left",
        "wheel_right",
    }
)

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
    return GENERIC_MODIFIER_MAP.get(token, token)


def is_combo_pulse_evdev(evdev_name: str) -> bool:
    return normalize_combo_evdev(evdev_name) in COMBO_PULSE_EVDEVS


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
