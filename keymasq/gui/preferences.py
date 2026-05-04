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


def load_appearance_mode() -> AppearanceMode:
    settings_path = _settings_path()
    if not settings_path.exists():
        return DEFAULT_APPEARANCE_MODE

    try:
        with settings_path.open("rb") as f:
            data = tomllib.load(f)
    except Exception:
        log.warning("Failed to load GUI settings: %s", settings_path, exc_info=True)
        return DEFAULT_APPEARANCE_MODE

    appearance = data.get("appearance")
    if isinstance(appearance, str) and appearance in VALID_APPEARANCE_MODES:
        return cast(AppearanceMode, appearance)
    return DEFAULT_APPEARANCE_MODE


def save_appearance_mode(mode: AppearanceMode) -> None:
    paths.ensure_config_dirs()
    with _settings_path().open("wb") as f:
        tomli_w.dump({"appearance": mode}, f)
