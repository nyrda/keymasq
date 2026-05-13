import logging
import tomllib
from typing import cast

import tomli_w

from keymasq.common.paths import VIRTUAL_DEVICES_PATH, ensure_config_dirs
from keymasq.common.virtual_devices import (
    DEFAULT_VIRTUAL_GAMEPADS,
    MAX_VIRTUAL_GAMEPADS,
    MIN_VIRTUAL_GAMEPADS,
    clamp_virtual_gamepad_count,
)

__all__ = [
    "DEFAULT_VIRTUAL_GAMEPADS",
    "MAX_VIRTUAL_GAMEPADS",
    "MIN_VIRTUAL_GAMEPADS",
    "clamp_virtual_gamepad_count",
    "load_virtual_gamepad_count",
    "save_virtual_gamepad_count",
]

log = logging.getLogger("keymasq-session.virtual-devices")


def load_virtual_gamepad_count() -> int:
    if not VIRTUAL_DEVICES_PATH.exists():
        return DEFAULT_VIRTUAL_GAMEPADS
    try:
        with open(VIRTUAL_DEVICES_PATH, "rb") as config_file:
            data = cast(dict[str, object], tomllib.load(config_file))
        gamepads = data.get("gamepads")
        if not isinstance(gamepads, dict):
            return DEFAULT_VIRTUAL_GAMEPADS
        gamepad_data = cast(dict[str, object], gamepads)
        return clamp_virtual_gamepad_count(gamepad_data.get("virtual_count"))
    except Exception as exc:
        log.warning(
            "Failed to load virtual device config from %s: %s; using default count %s",
            VIRTUAL_DEVICES_PATH,
            exc,
            DEFAULT_VIRTUAL_GAMEPADS,
        )
        return DEFAULT_VIRTUAL_GAMEPADS


def save_virtual_gamepad_count(count: int) -> int:
    ensure_config_dirs()
    clamped_count = clamp_virtual_gamepad_count(count)
    with open(VIRTUAL_DEVICES_PATH, "wb") as config_file:
        tomli_w.dump({"gamepads": {"virtual_count": clamped_count}}, config_file)
    return clamped_count
