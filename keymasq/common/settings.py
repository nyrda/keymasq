from dataclasses import dataclass
from typing import cast

from keymasq.common.keyboard_layouts import (
    DEFAULT_KEYBOARD_LAYOUT,
    normalize_keyboard_layout_id,
)
from keymasq.common.virtual_devices import (
    DEFAULT_VIRTUAL_GAMEPADS,
    clamp_virtual_gamepad_count,
)


@dataclass(frozen=True)
class GlobalSettings:
    virtual_gamepad_count: int = DEFAULT_VIRTUAL_GAMEPADS
    keyboard_layout: str = DEFAULT_KEYBOARD_LAYOUT


def _table(data: dict[str, object], name: str) -> dict[str, object]:
    table = data.get(name)
    return cast(dict[str, object], table) if isinstance(table, dict) else {}


def normalize_global_settings(settings: GlobalSettings) -> GlobalSettings:
    return GlobalSettings(
        virtual_gamepad_count=clamp_virtual_gamepad_count(settings.virtual_gamepad_count),
        keyboard_layout=normalize_keyboard_layout_id(settings.keyboard_layout),
    )


def global_settings_from_toml(data: dict[str, object]) -> GlobalSettings:
    gamepad_data = _table(data, "gamepads")
    keyboard_data = _table(data, "keyboard")
    return GlobalSettings(
        virtual_gamepad_count=clamp_virtual_gamepad_count(gamepad_data.get("virtual_count")),
        keyboard_layout=normalize_keyboard_layout_id(keyboard_data.get("layout")),
    )


def global_settings_to_toml(settings: GlobalSettings) -> dict[str, object]:
    normalized = normalize_global_settings(settings)
    return {
        "gamepads": {
            "virtual_count": normalized.virtual_gamepad_count,
        },
        "keyboard": {
            "layout": normalized.keyboard_layout,
        },
    }
