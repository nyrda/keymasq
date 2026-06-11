from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import (  # pyright: ignore[reportAttributeAccessIssue]
    Adw,  # pyright: ignore[reportAttributeAccessIssue]
    Gdk,  # pyright: ignore[reportAttributeAccessIssue]
    GLib,  # pyright: ignore[reportAttributeAccessIssue]
    GObject,  # pyright: ignore[reportAttributeAccessIssue]
    Gtk,  # pyright: ignore[reportAttributeAccessIssue]
)

from keymasq import __version__
from keymasq.common.slurp import get_slurp_capture
from keymasq.gui.session_client import session_request_async
from keymasq.gui.session_reload import notify_session_reload_async
from keymasq.gui.widgets.gamepad_output_choices import virtual_gamepad_count
from keymasq.gui.widgets.input_picker_shared import GAMEPAD_BUTTONS
from keymasq.gui.widgets.key_selector import KeySelectorDialog, SuperkeyActionDialog
from keymasq.gui.widgets.key_selector.gamepad_axis import (
    GamepadAxisControlsMixin as _GamepadAxisControlsMixin,
)
from keymasq.gui.widgets.key_selector.tabs import (
    _create_actions_docs_button,
    _ensure_compact_tabs_css,
)
from keymasq.gui.widgets.key_selector.targets import (
    _GAMEPAD_AXIS_CUSTOM_SLOT,
    _PROFILE_LIFETIME_PRESETS_ENABLE,
    _PROFILE_LIFETIME_PRESETS_TOGGLE,
    ACTION_DOC_LINKS,
    DEFAULT_RAPIDFIRE_TOOLTIP,
    EVDEV_TO_GAMEPAD,
    EVDEV_TO_KEY,
    F_EXTRA,
    KEY_TO_EVDEV,
    KEY_WIDTHS,
    KEYBOARD_LAYOUT,
    MEDIA_KEY_GROUPS,
    MEDIA_KEY_TARGETS,
    MPRIS_MEDIA_GROUPS,
    REPEAT_CATEGORY_OPTIONS,
    REPEAT_RAPIDFIRE_TOOLTIP,
    SYSTEM_KEY_GROUPS,
    SYSTEM_KEY_TARGETS,
    _actions_docs_url,
    _keyboard_target_allows_rapidfire,
    _keyboard_target_allows_tap,
    _resolve_gamepad_axis_target,
    _resolve_gamepad_button_target,
)
from keymasq.session.compositor import detect_compositor_sync
from keymasq.session.hardware import HardwareManager

__all__ = [
    "Adw",
    "Gdk",
    "GLib",
    "GObject",
    "Gtk",
    "KeySelectorDialog",
    "SuperkeyActionDialog",
    "GAMEPAD_BUTTONS",
    "KEYBOARD_LAYOUT",
    "KEY_TO_EVDEV",
    "KEY_WIDTHS",
    "F_EXTRA",
    "MPRIS_MEDIA_GROUPS",
    "MEDIA_KEY_GROUPS",
    "SYSTEM_KEY_GROUPS",
    "MEDIA_KEY_TARGETS",
    "SYSTEM_KEY_TARGETS",
    "ACTION_DOC_LINKS",
    "DEFAULT_RAPIDFIRE_TOOLTIP",
    "REPEAT_CATEGORY_OPTIONS",
    "REPEAT_RAPIDFIRE_TOOLTIP",
    "EVDEV_TO_KEY",
    "EVDEV_TO_GAMEPAD",
    "_actions_docs_url",
    "_create_actions_docs_button",
    "_ensure_compact_tabs_css",
    "_GamepadAxisControlsMixin",
    "_GAMEPAD_AXIS_CUSTOM_SLOT",
    "_PROFILE_LIFETIME_PRESETS_ENABLE",
    "_PROFILE_LIFETIME_PRESETS_TOGGLE",
    "_keyboard_target_allows_rapidfire",
    "_keyboard_target_allows_tap",
    "_resolve_gamepad_axis_target",
    "_resolve_gamepad_button_target",
    "session_request_async",
    "notify_session_reload_async",
    "get_slurp_capture",
    "detect_compositor_sync",
    "HardwareManager",
    "virtual_gamepad_count",
    "__version__",
]
