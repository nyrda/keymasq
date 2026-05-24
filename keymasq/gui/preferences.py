import logging
import tomllib
from pathlib import Path
from typing import Literal, cast

import tomli_w

from keymasq.common import paths

log = logging.getLogger(__name__)

AppearanceMode = Literal["system", "light", "dark"]

DEFAULT_APPEARANCE_MODE: AppearanceMode = "system"
VALID_APPEARANCE_MODES: set[str] = {"system", "light", "dark"}


def _settings_path() -> Path:
    return paths.CONFIG_DIR / "gui_settings.toml"


def _load_settings() -> dict[str, object]:
    settings_path = _settings_path()
    if not settings_path.exists():
        return {}

    try:
        with settings_path.open("rb") as f:
            data = tomllib.load(f)
    except Exception:
        log.warning("Failed to load GUI settings: %s", settings_path, exc_info=True)
        return {}

    return dict(data)


def _save_settings(data: dict[str, object]) -> None:
    paths.ensure_config_dirs()
    with _settings_path().open("wb") as f:
        tomli_w.dump(data, f)


def load_appearance_mode() -> AppearanceMode:
    data = _load_settings()
    appearance = data.get("appearance")
    if isinstance(appearance, str) and appearance in VALID_APPEARANCE_MODES:
        return cast(AppearanceMode, appearance)
    return DEFAULT_APPEARANCE_MODE


def save_appearance_mode(mode: AppearanceMode) -> None:
    data = _load_settings()
    data["appearance"] = mode
    _save_settings(data)


def load_device_tab_order() -> list[str]:
    data = _load_settings()
    order = data.get("device_tab_order")
    if not isinstance(order, list):
        return []
    return [str(hardware_id) for hardware_id in order if str(hardware_id).strip()]


def save_device_tab_order(hardware_ids: list[str]) -> None:
    data = _load_settings()
    seen: set[str] = set()
    data["device_tab_order"] = [
        hardware_id
        for hardware_id in hardware_ids
        if hardware_id.strip() and not (hardware_id in seen or seen.add(hardware_id))
    ]
    _save_settings(data)
