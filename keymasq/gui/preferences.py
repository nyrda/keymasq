import logging
import tomllib
from pathlib import Path
from typing import Literal, cast

from keymasq.common import paths
from keymasq.common.config_files import write_toml_atomically

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
    write_toml_atomically(_settings_path(), data)


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


def _clean_string_list(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        cleaned.append(item)
    return cleaned


def load_tab_order() -> list[str]:
    data = _load_settings()
    return _clean_string_list(data.get("tab_order"))


def load_hidden_tabs() -> set[str]:
    data = _load_settings()
    return set(_clean_string_list(data.get("hidden_tabs")))


def load_selected_tab() -> str:
    data = _load_settings()
    selected_tab = data.get("selected_tab")
    if isinstance(selected_tab, str):
        return selected_tab.strip()
    return ""


def save_selected_tab(selected_tab: str) -> None:
    data = _load_settings()
    cleaned = selected_tab.strip()
    if cleaned:
        data["selected_tab"] = cleaned
    else:
        data.pop("selected_tab", None)
    _save_settings(data)


def save_tab_layout(tab_order: list[str], hidden_tabs: set[str]) -> None:
    data = _load_settings()
    cleaned_order = _clean_string_list(tab_order)
    hidden = set(_clean_string_list(list(hidden_tabs)))
    hidden_tabs_ordered = [tab_id for tab_id in cleaned_order if tab_id in hidden]
    hidden_tabs_ordered.extend(sorted(hidden - set(hidden_tabs_ordered)))
    data["tab_order"] = cleaned_order
    data["hidden_tabs"] = hidden_tabs_ordered
    _save_settings(data)
