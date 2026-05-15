from dataclasses import dataclass
from typing import cast

from keymasq.common.virtual_devices import (
    DEFAULT_VIRTUAL_GAMEPADS,
    clamp_virtual_gamepad_count,
)


@dataclass(frozen=True)
class GlobalSettings:
    virtual_gamepad_count: int = DEFAULT_VIRTUAL_GAMEPADS


def global_settings_from_toml(data: dict[str, object]) -> GlobalSettings:
    gamepads = data.get("gamepads")
    gamepad_data = cast(dict[str, object], gamepads) if isinstance(gamepads, dict) else {}
    return GlobalSettings(
        virtual_gamepad_count=clamp_virtual_gamepad_count(gamepad_data.get("virtual_count")),
    )


def global_settings_to_toml(settings: GlobalSettings) -> dict[str, object]:
    return {
        "gamepads": {
            "virtual_count": clamp_virtual_gamepad_count(settings.virtual_gamepad_count),
        },
    }
