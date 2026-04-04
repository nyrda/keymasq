import os

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gtk

KEYBOARD_ICON_NAMES = (
    "keyforge-keyboard-symbolic",
    "preferences-desktop-keyboard-shortcuts-symbolic",
    "input-keyboard-symbolic",
    "preferences-desktop-keyboard-symbolic",
    "input-keyboard",
    "preferences-desktop-keyboard-shortcuts",
)

COMBO_ICON_NAMES = (
    "keyforge-combos-symbolic",
    "preferences-desktop-keyboard-shortcuts-symbolic",
    "input-keyboard-symbolic",
    "preferences-desktop-keyboard-shortcuts",
)

MOUSE_ICON_NAMES = (
    "keyforge-mouse-symbolic",
    "input-mouse-symbolic",
    "input-mouse",
    "input-tablet",
)
GAMEPAD_ICON_NAMES = (
    "input-gaming-symbolic",
    "input-gaming",
    "applications-games-symbolic",
    "applications-games",
)

CORE_ICON_GROUPS = (
    KEYBOARD_ICON_NAMES,
    MOUSE_ICON_NAMES,
    GAMEPAD_ICON_NAMES,
    ("list-add-symbolic", "list-add"),
    ("user-trash-symbolic", "edit-delete-symbolic", "edit-delete"),
)


def _icon_theme() -> Gtk.IconTheme | None:
    display = Gdk.Display.get_default()
    if not display:
        return None
    return Gtk.IconTheme.get_for_display(display)


def register_icon_search_path() -> None:
    theme = _icon_theme()
    if not theme:
        return
    theme.add_search_path(os.path.join(os.path.dirname(__file__), "assets"))


def resolve_icon_name(*names: str) -> str:
    theme = _icon_theme()
    if theme:
        for name in names:
            if theme.has_icon(name):
                return name
    return names[0]


def image_from_icon_names(*names: str, pixel_size: int | None = None) -> Gtk.Image:
    image = Gtk.Image.new_from_icon_name(resolve_icon_name(*names))
    if pixel_size is not None:
        image.set_pixel_size(pixel_size)
    return image


def device_icon_names(
    is_keyboard: bool | None = None,
    *,
    device_kind: str | None = None,
) -> tuple[str, ...]:
    if device_kind == "gamepad":
        return GAMEPAD_ICON_NAMES
    if device_kind == "mouse":
        return MOUSE_ICON_NAMES
    if device_kind == "keyboard":
        return KEYBOARD_ICON_NAMES
    return KEYBOARD_ICON_NAMES if is_keyboard else MOUSE_ICON_NAMES


def combo_icon_names() -> tuple[str, ...]:
    return COMBO_ICON_NAMES


def theme_supports_core_icons() -> bool:
    theme = _icon_theme()
    if not theme:
        return True
    return all(any(theme.has_icon(name) for name in group) for group in CORE_ICON_GROUPS)
