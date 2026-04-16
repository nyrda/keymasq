from __future__ import annotations

from collections.abc import Iterable

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
