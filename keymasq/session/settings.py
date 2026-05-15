import contextlib
import logging
import os
import tempfile
import tomllib
from pathlib import Path
from typing import cast

import tomli_w

from keymasq.common import paths
from keymasq.common.settings import (
    GlobalSettings,
    global_settings_from_toml,
    global_settings_to_toml,
)
from keymasq.common.virtual_devices import (
    DEFAULT_VIRTUAL_GAMEPADS,
    clamp_virtual_gamepad_count,
)

log = logging.getLogger("keymasq-session.settings")


def _settings_path() -> Path:
    return paths.CONFIG_DIR / "settings.toml"


def load_global_settings() -> GlobalSettings:
    settings_path = _settings_path()
    if not settings_path.exists():
        return GlobalSettings()
    try:
        with settings_path.open("rb") as config_file:
            data = cast(dict[str, object], tomllib.load(config_file))
        return global_settings_from_toml(data)
    except Exception as exc:
        log.warning(
            "Failed to load settings from %s: %s; using defaults",
            settings_path,
            exc,
        )
        return GlobalSettings()


def save_global_settings(settings: GlobalSettings) -> GlobalSettings:
    normalized = GlobalSettings(
        virtual_gamepad_count=clamp_virtual_gamepad_count(settings.virtual_gamepad_count),
    )
    paths.ensure_config_dirs()
    settings_path = _settings_path()
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=settings_path.parent,
            prefix=f".{settings_path.name}.",
            delete=False,
        ) as config_file:
            temp_path = config_file.name
            tomli_w.dump(global_settings_to_toml(normalized), config_file)
            config_file.flush()
            os.fsync(config_file.fileno())
        os.replace(temp_path, settings_path)
        temp_path = ""
    finally:
        if temp_path:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp_path)
    return normalized


def load_virtual_gamepad_count() -> int:
    return load_global_settings().virtual_gamepad_count


def save_virtual_gamepad_count(count: int) -> int:
    saved = save_global_settings(
        GlobalSettings(
            virtual_gamepad_count=count,
        )
    )
    return saved.virtual_gamepad_count


__all__ = [
    "DEFAULT_VIRTUAL_GAMEPADS",
    "GlobalSettings",
    "load_global_settings",
    "load_virtual_gamepad_count",
    "save_global_settings",
    "save_virtual_gamepad_count",
]
