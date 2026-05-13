import logging

import evdev
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
from keymasq.common.devices import is_gamepad_button_name
from keymasq.common.models import (
    MIN_RAPIDFIRE_HOLD_MS,
    MIN_RAPIDFIRE_WAIT_MS,
    ActionType,
    MappingAction,
    SuperkeyAction,
    SuperkeyConfig,
    action_type_supports_rapidfire,
)
from keymasq.common.slurp import get_slurp_capture
from keymasq.common.virtual_devices import virtual_gamepad_output_id
from keymasq.gui.session_client import session_request_async
from keymasq.gui.widgets.action_labels import describe_mapping_action_verbose
from keymasq.gui.widgets.compositor_actions import (
    build_compositor_action_pages,
    compositor_action_tab_name,
)
from keymasq.gui.widgets.input_picker_shared import (
    build_gamepad_tab as build_shared_gamepad_tab,
)
from keymasq.gui.widgets.input_picker_shared import (
    build_keyboard_tab as build_shared_keyboard_tab,
)
from keymasq.gui.widgets.input_picker_shared import (
    build_media_tab as build_shared_media_tab,
)
from keymasq.gui.widgets.input_picker_shared import (
    build_mouse_tab as build_shared_mouse_tab,
)
from keymasq.gui.widgets.input_picker_shared import (
    build_navigation_tab as build_shared_navigation_tab,
)
from keymasq.session.compositor import detect_compositor_sync
from keymasq.session.hardware import HardwareManager
from keymasq.session.superkeys import SuperkeyManager
from keymasq.session.virtual_devices import load_virtual_gamepad_count

log = logging.getLogger("keymasq.gui.widgets.key_selector_dialog")

KEYBOARD_LAYOUT = [
    ["Esc", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12"],
    ["`", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "-", "=", "Bspc"],
    ["Tab", "Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P", "[", "]", "\\"],
    ["Caps", "A", "S", "D", "F", "G", "H", "J", "K", "L", ";", "'", "Enter"],
    ["LShift", "Z", "X", "C", "V", "B", "N", "M", ",", ".", "/", "RShift"],
    ["LCtrl", "LMeta", "LAlt", "Space", "RAlt", "RMeta", "Fn", "Menu", "RCtrl"],
]

KEY_TO_EVDEV = {
    "Esc": "key_esc",
    "F1": "key_f1",
    "F2": "key_f2",
    "F3": "key_f3",
    "F4": "key_f4",
    "F5": "key_f5",
    "F6": "key_f6",
    "F7": "key_f7",
    "F8": "key_f8",
    "F9": "key_f9",
    "F10": "key_f10",
    "F11": "key_f11",
    "F12": "key_f12",
    "`": "key_grave",
    "1": "key_1",
    "2": "key_2",
    "3": "key_3",
    "4": "key_4",
    "5": "key_5",
    "6": "key_6",
    "7": "key_7",
    "8": "key_8",
    "9": "key_9",
    "0": "key_0",
    "-": "key_minus",
    "=": "key_equal",
    "Bspc": "key_backspace",
    "Tab": "key_tab",
    "Q": "key_q",
    "W": "key_w",
    "E": "key_e",
    "R": "key_r",
    "T": "key_t",
    "Y": "key_y",
    "U": "key_u",
    "I": "key_i",
    "O": "key_o",
    "P": "key_p",
    "[": "key_leftbrace",
    "]": "key_rightbrace",
    "\\": "key_backslash",
    "Caps": "key_capslock",
    "A": "key_a",
    "S": "key_s",
    "D": "key_d",
    "F": "key_f",
    "G": "key_g",
    "H": "key_h",
    "J": "key_j",
    "K": "key_k",
    "L": "key_l",
    ";": "key_semicolon",
    "'": "key_apostrophe",
    "Enter": "key_enter",
    "LShift": "key_leftshift",
    "Z": "key_z",
    "X": "key_x",
    "C": "key_c",
    "V": "key_v",
    "B": "key_b",
    "N": "key_n",
    "M": "key_m",
    ",": "key_comma",
    ".": "key_dot",
    "/": "key_slash",
    "RShift": "key_rightshift",
    "LCtrl": "key_leftctrl",
    "LMeta": "key_leftmeta",
    "LAlt": "key_leftalt",
    "Space": "key_space",
    "RAlt": "key_rightalt",
    "RMeta": "key_rightmeta",
    "Fn": None,
    "Menu": "key_menu",
    "RCtrl": "key_rightctrl",
    "F13": "key_f13",
    "F14": "key_f14",
    "F15": "key_f15",
    "F16": "key_f16",
    "F17": "key_f17",
    "F18": "key_f18",
    "F19": "key_f19",
    "F20": "key_f20",
    "F21": "key_f21",
    "F22": "key_f22",
    "F23": "key_f23",
    "F24": "key_f24",
    "Ins": "key_insert",
    "Del": "key_delete",
    "Home": "key_home",
    "End": "key_end",
    "PgUp": "key_pageup",
    "PgDn": "key_pagedown",
    "Up": "key_up",
    "Down": "key_down",
    "Left": "key_left",
    "Right": "key_right",
    "NumLk": "key_numlock",
    "KP/": "key_kpslash",
    "KP*": "key_kpasterisk",
    "KP-": "key_kpminus",
    "KP7": "key_kp7",
    "KP8": "key_kp8",
    "KP9": "key_kp9",
    "KP+": "key_kpplus",
    "KP4": "key_kp4",
    "KP5": "key_kp5",
    "KP6": "key_kp6",
    "KP1": "key_kp1",
    "KP2": "key_kp2",
    "KP3": "key_kp3",
    "KPEnter": "key_kpenter",
    "KP0": "key_kp0",
    "KP.": "key_kpdot",
    "Mute": "key_mute",
    "Volume Down": "key_volumedown",
    "Volume Up": "key_volumeup",
    "Mic Mute": "key_micmute",
    "Play/Pause": "key_playpause",
    "Play": "key_play",
    "Pause": "key_pause",
    "Stop": "key_stop",
    "Previous Track": "key_previoussong",
    "Next Track": "key_nextsong",
}

KEY_WIDTHS = {
    "Esc": 1,
    "Bspc": 2,
    "Tab": 1.5,
    "\\": 1.5,
    "Caps": 1.75,
    "Enter": 2.25,
    "LShift": 2.25,
    "RShift": 2.75,
    "LCtrl": 1.25,
    "LMeta": 1.25,
    "LAlt": 1.25,
    "Space": 6.25,
    "RAlt": 1.25,
    "RMeta": 1.25,
    "Fn": 1.25,
    "Menu": 1.25,
    "RCtrl": 1.25,
}

GAMEPAD_BUTTONS = {
    "A": ("btn_south", False),
    "B": ("btn_east", False),
    "X": ("btn_north", False),
    "Y": ("btn_west", False),
    "LB": ("btn_tl", False),
    "RB": ("btn_tr", False),
    "LT": ("btn_tl2", True),
    "RT": ("btn_tr2", True),
    "Select": ("btn_select", False),
    "Start": ("btn_start", False),
    "Guide": ("btn_mode", False),
    "LS": ("btn_thumbl", False),
    "RS": ("btn_thumbr", False),
    "D-Up": ("btn_dpad_up", False),
    "D-Down": ("btn_dpad_down", False),
    "D-Left": ("btn_dpad_left", False),
    "D-Right": ("btn_dpad_right", False),
}

F_EXTRA = ["F13", "F14", "F15", "F16", "F17", "F18", "F19", "F20", "F21", "F22", "F23", "F24"]

MEDIA_KEY_GROUPS = [
    (
        "Audio",
        [
            ("Mute", "key_mute", "audio-volume-muted-symbolic"),
            ("Vol Down", "key_volumedown", "audio-volume-low-symbolic"),
            ("Vol Up", "key_volumeup", "audio-volume-high-symbolic"),
            ("Mic Mute", "key_micmute", "microphone-sensitivity-muted-symbolic"),
        ],
    ),
    (
        "Playback",
        [
            ("Previous", "key_previoussong", "media-skip-backward-symbolic"),
            ("Play/Pause", "key_playpause", "media-playback-start-symbolic"),
            ("Next", "key_nextsong", "media-skip-forward-symbolic"),
            ("Stop", "key_stop", "media-playback-stop-symbolic"),
            ("Play", "key_play", "media-playback-start-symbolic"),
            ("Pause", "key_pause", "media-playback-pause-symbolic"),
        ],
    ),
]
MEDIA_KEY_TARGETS = {
    evdev_id for _title, buttons in MEDIA_KEY_GROUPS for _label, evdev_id, _icon_name in buttons
}

ACTION_DOC_LINKS = {
    "special": ("special", "Special"),
    "keyboard": ("keyboard", "Keyboard"),
    "navigation": ("navigation", "Navigation"),
    "media": ("media", "Media"),
    "mouse": ("mouse", "Mouse"),
    "gamepad": ("gamepad", "Gamepad"),
    "hyprland": ("hyprland", "Hyprland"),
    "niri": ("niri", "Niri"),
    "kde": ("kde-plasma", "KDE Plasma"),
    "gnome": ("gnome", "GNOME"),
    "superkey": ("super-keys", "Super Keys"),
    "macro": ("macro", "Macro"),
    "profile": ("profile", "Profile"),
    "exec": ("execute-shell-command", "Command"),
}

EVDEV_TO_KEY = {v: k for k, v in KEY_TO_EVDEV.items()}
EVDEV_TO_GAMEPAD = {v[0]: k for k, v in GAMEPAD_BUTTONS.items()}

_compact_tabs_css_installed = False


def _docs_version() -> str:
    version = __version__.strip()
    if not version:
        return "master"
    if "dev" in version:
        return "master"
    return f"v{version.removeprefix('v')}"


def _actions_docs_url(anchor: str) -> str:
    return f"https://keymasq.tools/docs/{_docs_version()}/ACTIONS/#{anchor}"


def _create_actions_docs_button() -> Gtk.Button:
    btn = Gtk.Button(label="?")
    btn.add_css_class("flat")
    btn.add_css_class("actions-docs-button")
    btn.set_tooltip_text("Open documentation for this tab")
    return btn


def _ensure_compact_tabs_css() -> None:
    global _compact_tabs_css_installed
    if _compact_tabs_css_installed:
        return

    display = Gdk.Display.get_default()
    if display is None:
        return

    provider = Gtk.CssProvider()
    provider.load_from_string(
        """
        .compact-map-tabs button {
            padding-left: 7px;
            padding-right: 7px;
            min-height: 28px;
            font-size: 0.85em;
        }
        """
    )
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _compact_tabs_css_installed = True


class KeySelectorDialog(Adw.Dialog):
    __gsignals__ = {
        "key-selected": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(
        self,
        parent: Gtk.Widget,
        button_label: str,
        current_action: MappingAction | None = None,
        compositor_action_status: dict[str, object] | None = None,
        *,
        allow_passthrough: bool = True,
        allow_clear_mapping: bool = True,
        allow_suppress: bool = True,
        allow_superkey: bool = True,
        allow_rapidfire: bool = True,
        allow_tap: bool = True,
        allow_macro_options: bool = True,
    ):
        super().__init__(title=f"Map: {button_label}", content_width=570, content_height=580)
        self._parent = parent
        self._button_label = button_label
        self._current_action = current_action
        self._allow_passthrough = allow_passthrough
        self._allow_clear_mapping = allow_clear_mapping
        self._allow_suppress = allow_suppress
        self._allow_superkey = allow_superkey
        self._allow_rapidfire = allow_rapidfire
        self._allow_tap = allow_tap
        self._allow_macro_options = allow_macro_options
        self._compositor_action_status = self._resolve_compositor_action_status(
            compositor_action_status
        )
        self._rapidfire_enabled = False
        self._rapidfire_hold = 20
        self._rapidfire_wait = 20
        self._tap_enabled = False
        self._tap_hold = 50
        self._macro_list: list[dict] = []
        self._selected_macro: str | None = None
        self._superkey_list: list[SuperkeyConfig] = []
        self._superkey_names: list[str] = []
        self._selected_superkey: str | None = None
        self._macro_replay_movement: bool = True
        self._macro_replay_clicks: bool = True
        self._macro_speed: float = 1.0
        self._profile_entries: list[dict] = []
        self._selected_profile_action: str = "toggle"
        self._selected_profile_name: str = ""
        self._selected_gamepad_output_id: str | None = (
            current_action.output_id
            if current_action and current_action.action_type == ActionType.GAMEPAD
            else None
        )
        self._gamepad_output_ids: list[str | None] = []
        self._gamepad_output_dropdown: Gtk.DropDown | None = None
        self._profile_name_items: list[str] = []
        self._exec_cmd: str = ""
        self._mouse_move_x: int = 0
        self._mouse_move_y: int = 0
        self._mouse_move_mode: str = "rel"
        self._capture_delay_seconds: float = 2.0
        self._capture_timeout_id: int = 0
        self._capture_pending: bool = False
        self._capture_request_id: int = 0
        self._slurp_capture = get_slurp_capture()
        self._slurp_capture.set_compositor(detect_compositor_sync())
        self._slurp_available = self._slurp_capture.available

        if current_action:
            self._rapidfire_enabled = current_action.rapidfire_enabled
            self._rapidfire_hold = current_action.rapidfire_hold_ms
            self._rapidfire_wait = current_action.rapidfire_wait_ms
            self._tap_enabled = current_action.tap_enabled
            self._tap_hold = current_action.tap_hold_ms
            if current_action.action_type == ActionType.MACRO:
                self._selected_macro = current_action.macro_name
                self._macro_replay_movement = current_action.macro_replay_mouse_movement
                self._macro_replay_clicks = current_action.macro_replay_mouse_clicks
                self._macro_speed = current_action.macro_speed
            elif current_action.action_type == ActionType.SUPERKEY:
                self._selected_superkey = current_action.superkey_name
            elif current_action.action_type == ActionType.EXEC:
                self._exec_cmd = current_action.cmd or ""
            elif current_action.action_type in (
                ActionType.PROFILE_ENABLE,
                ActionType.PROFILE_DISABLE,
                ActionType.PROFILE_TOGGLE,
            ):
                self._selected_profile_name = str(current_action.profile_name or "")
                if current_action.action_type == ActionType.PROFILE_ENABLE:
                    self._selected_profile_action = "enable"
                elif current_action.action_type == ActionType.PROFILE_DISABLE:
                    self._selected_profile_action = "disable"
                else:
                    self._selected_profile_action = "toggle"
            elif current_action.action_type in (
                ActionType.MOUSE_MOVE_REL,
                ActionType.MOUSE_MOVE_ABS,
            ):
                self._mouse_move_x = int(current_action.move_x)
                self._mouse_move_y = int(current_action.move_y)
                if current_action.action_type == ActionType.MOUSE_MOVE_ABS:
                    self._mouse_move_mode = "abs"
        if not self._allow_rapidfire:
            self._rapidfire_enabled = False
        if not self._allow_tap:
            self._tap_enabled = False

        self._build_ui()

    def _build_ui(self):
        _ensure_compact_tabs_css()

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main_box.set_margin_top(12)
        main_box.set_margin_bottom(12)
        main_box.set_margin_start(12)
        main_box.set_margin_end(12)

        self.options_box = self._build_options_box()
        self._compositor_action_pages = build_compositor_action_pages(
            self._current_action,
            self._on_compositor_action_selected,
            self._compositor_action_status,
        )
        self._compositor_action_page_ids = {page.page_id for page in self._compositor_action_pages}

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.add_titled(self._build_special_tab(), "special", "Special")
        self.stack.add_titled(self._build_keyboard_tab(), "keyboard", "Keyboard")
        self.stack.add_titled(self._build_navigation_tab(), "navigation", "Navigation")
        self.stack.add_titled(self._build_media_tab(), "media", "Media")
        self.stack.add_titled(self._build_mouse_tab(), "mouse", "Mouse")
        for page in self._compositor_action_pages:
            self.stack.add_titled(page.widget, page.page_id, page.title)
        self.stack.add_titled(self._build_gamepad_tab(), "gamepad", "Gamepad")
        if self._allow_superkey:
            self.stack.add_titled(self._build_superkey_tab(), "superkey", "Super Keys")
        self.stack.add_titled(self._build_macro_tab(), "macro", "Macro")
        self.stack.add_titled(self._build_profile_tab(), "profile", "Profile")

        self._set_initial_tab()

        frame = Gtk.Frame()
        frame.set_vexpand(True)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        title_label = Gtk.Label(label=f"Map: {self._button_label}")
        title_label.add_css_class("title-3")
        title_label.set_halign(Gtk.Align.CENTER)
        title_label.set_margin_top(12)
        title_label.set_margin_bottom(6)
        inner.append(title_label)

        if self._current_action:
            current_label = Gtk.Label(label=self._describe_current_action())
            current_label.add_css_class("dim-label")
            current_label.set_halign(Gtk.Align.CENTER)
            current_label.set_margin_bottom(10)
            inner.append(current_label)
        else:
            title_label.set_margin_bottom(12)

        inner.append(Gtk.Separator())

        sidebar = Gtk.StackSidebar()
        sidebar.set_stack(self.stack)
        sidebar.set_size_request(120, -1)

        paned = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        paned.set_vexpand(True)
        paned.append(sidebar)
        paned.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        right_box.set_hexpand(True)
        right_box.append(self.stack)
        right_box.append(Gtk.Separator())

        options_pad = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        options_pad.set_margin_top(8)
        options_pad.set_margin_bottom(8)
        options_pad.set_margin_start(12)
        options_pad.set_margin_end(12)
        options_pad.append(self.options_box)
        right_box.append(options_pad)

        paned.append(right_box)
        inner.append(paned)

        inner.append(Gtk.Separator())

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_hexpand(True)
        footer.set_margin_top(8)
        footer.set_margin_bottom(8)
        footer.set_margin_start(12)
        footer.set_margin_end(12)

        self.actions_docs_btn = _create_actions_docs_button()
        self.actions_docs_btn.connect("clicked", self._on_actions_docs_clicked)
        footer.append(self.actions_docs_btn)

        footer_spacer = Gtk.Box()
        footer_spacer.set_hexpand(True)
        footer.append(footer_spacer)

        self.map_btn = Gtk.Button(label="Map")
        self.map_btn.add_css_class("suggested-action")
        self.map_btn.set_sensitive(False)
        self.map_btn.set_visible(False)
        self.map_btn.connect("clicked", self._on_map_clicked)
        footer.append(self.map_btn)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_cancel_clicked)
        footer.append(cancel_btn)
        inner.append(footer)

        frame.set_child(inner)
        main_box.append(frame)
        self.set_child(main_box)

        self.stack.connect("notify::visible-child", self._on_tab_changed)
        self._on_tab_changed(self.stack, None)

    def _on_cancel_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _build_special_tab(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_valign(Gtk.Align.CENTER)

        special_buttons_added = False

        if self._allow_clear_mapping:
            passthrough_btn = self._create_key_button("Passthrough", "clear_mapping", large=True)
            passthrough_btn.connect("clicked", self._on_special_clicked, "clear_mapping")
            passthrough_btn.set_tooltip_text(
                "Do not store a mapping here, so lower-priority profiles can still apply"
            )
            box.append(passthrough_btn)
            special_buttons_added = True

        if self._allow_suppress:
            suppress_btn = self._create_key_button("Suppress", "suppress", large=True)
            suppress_btn.connect("clicked", self._on_special_clicked, "suppress")
            suppress_btn.set_tooltip_text("Block the button press entirely — nothing is sent")
            box.append(suppress_btn)
            special_buttons_added = True

        exec_label = Gtk.Label(label="Execute Shell Command")
        exec_label.add_css_class("dim-label")
        if special_buttons_added:
            box.append(Gtk.Separator())
        box.append(exec_label)

        exec_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        exec_box.set_halign(Gtk.Align.CENTER)

        self.exec_entry = Gtk.Entry()
        self.exec_entry.set_placeholder_text("e.g., notify-send 'hello'")
        self.exec_entry.set_size_request(300, -1)
        self.exec_entry.set_text(self._exec_cmd)
        self.exec_entry.connect("changed", self._on_exec_text_changed)
        exec_box.append(self.exec_entry)

        self.exec_map_btn = Gtk.Button(label="Map Command")
        self.exec_map_btn.add_css_class("suggested-action")
        self.exec_map_btn.set_sensitive(bool(self._exec_cmd.strip()))
        self.exec_map_btn.connect("clicked", self._on_exec_map_clicked)
        exec_box.append(self.exec_map_btn)

        box.append(exec_box)

        return box

    def _build_superkey_tab(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        toolbar_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar_row.set_margin_top(8)
        toolbar_row.set_margin_bottom(4)
        toolbar_row.set_margin_start(12)
        toolbar_row.set_margin_end(12)
        toolbar_row.set_halign(Gtk.Align.START)

        refresh_btn = Gtk.Button(label="Refresh")
        refresh_btn.add_css_class("flat")
        refresh_btn.connect("clicked", self._on_superkey_refresh)
        toolbar_row.append(refresh_btn)
        outer.append(toolbar_row)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        self._superkey_listbox = Gtk.ListBox()
        self._superkey_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._superkey_listbox.set_valign(Gtk.Align.START)
        self._superkey_listbox.add_css_class("boxed-list")
        self._superkey_listbox.set_margin_start(12)
        self._superkey_listbox.set_margin_end(12)
        self._superkey_listbox.connect("row-selected", self._on_superkey_row_selected)
        scrolled.set_child(self._superkey_listbox)
        outer.append(scrolled)

        self._load_superkey_list()
        return outer

    def _build_keyboard_tab(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        scrolled = build_shared_keyboard_tab(
            self,
            keyboard_layout=KEYBOARD_LAYOUT,
            key_to_evdev=KEY_TO_EVDEV,
            key_widths=KEY_WIDTHS,
        )
        scrolled.set_vexpand(True)
        outer.append(scrolled)

        toolbar_sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        toolbar_sep.set_margin_start(12)
        toolbar_sep.set_margin_end(12)
        outer.append(toolbar_sep)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.set_halign(Gtk.Align.CENTER)
        toolbar.set_margin_top(12)
        toolbar.set_margin_bottom(12)
        toolbar.set_margin_start(12)
        toolbar.set_margin_end(12)

        self.kb_capture_btn = Gtk.Button(label="Capture Key")
        self.kb_capture_btn.connect("clicked", self._on_keyboard_capture_clicked)
        toolbar.append(self.kb_capture_btn)

        self.kb_capture_status = Gtk.Label(label="")
        self.kb_capture_status.add_css_class("dim-label")
        toolbar.append(self.kb_capture_status)

        toolbar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        toolbar.append(Gtk.Label(label="Key code:"))

        self.kb_code_entry = Gtk.Entry()
        self.kb_code_entry.set_placeholder_text("e.g. 125 or key_leftmeta")
        self.kb_code_entry.set_width_chars(18)
        toolbar.append(self.kb_code_entry)

        code_btn = Gtk.Button(label="Map Code")
        code_btn.connect("clicked", self._on_map_code_clicked)
        toolbar.append(code_btn)

        outer.append(toolbar)

        if not hasattr(self, "_kb_capture_controller"):
            self._kb_capture_pending = False
            self._kb_capture_controller = Gtk.EventControllerKey()
            self._kb_capture_controller.connect(
                "key-pressed", self._on_keyboard_capture_key_pressed
            )
            self.add_controller(self._kb_capture_controller)

        return outer

    def _on_keyboard_capture_clicked(self, btn) -> None:
        self._kb_capture_pending = True
        self.kb_capture_status.set_text("Press a key...")

    def _on_keyboard_capture_key_pressed(self, controller, keyval, keycode, state) -> bool:
        if not getattr(self, "_kb_capture_pending", False):
            return False
        evdev_name = self._keyval_to_evdev(keyval)
        if not evdev_name:
            self.kb_capture_status.set_text("Unrecognized key")
            self._kb_capture_pending = False
            return True
        self.kb_capture_status.set_text(f"Captured: {evdev_name}")
        self._kb_capture_pending = False
        self._emit_keyboard_mapping(evdev_name)
        return True

    def _on_map_code_clicked(self, btn) -> None:
        raw = self.kb_code_entry.get_text().strip().lower()
        if not raw:
            return
        evdev_name = None
        if raw.startswith("key_"):
            evdev_name = raw
        else:
            try:
                code = int(raw)
                key_name = evdev.ecodes.KEY.get(code)
                if isinstance(key_name, str) and key_name.startswith("KEY_"):
                    evdev_name = key_name.lower()
            except Exception:
                evdev_name = None
        if not evdev_name:
            self.kb_code_entry.set_text("")
            self.kb_code_entry.set_placeholder_text("Unknown key code")
            return
        self._emit_keyboard_mapping(evdev_name)

    def _emit_keyboard_mapping(self, evdev_name: str) -> None:
        action = MappingAction(
            action_type=ActionType.KEYBOARD,
            target=evdev_name,
            rapidfire_enabled=self._rapidfire_enabled,
            rapidfire_hold_ms=int(self.hold_spin.get_value()),
            rapidfire_wait_ms=int(self.wait_spin.get_value()),
            tap_enabled=self._tap_enabled,
            tap_hold_ms=int(self.tap_spin.get_value()),
        )
        self.emit("key-selected", action)
        self.close()

    def _keyval_to_evdev(self, keyval: int) -> str | None:
        name = (Gdk.keyval_name(keyval) or "").lower()
        if not name:
            return None
        special = {
            "escape": "key_esc",
            "tab": "key_tab",
            "return": "key_enter",
            "backspace": "key_backspace",
            "space": "key_space",
            "shift_l": "key_leftshift",
            "shift_r": "key_rightshift",
            "control_l": "key_leftctrl",
            "control_r": "key_rightctrl",
            "alt_l": "key_leftalt",
            "alt_r": "key_rightalt",
            "super_l": "key_leftmeta",
            "super_r": "key_rightmeta",
            "menu": "key_menu",
            "left": "key_left",
            "right": "key_right",
            "up": "key_up",
            "down": "key_down",
            "minus": "key_minus",
            "equal": "key_equal",
            "bracketleft": "key_leftbrace",
            "bracketright": "key_rightbrace",
            "backslash": "key_backslash",
            "semicolon": "key_semicolon",
            "apostrophe": "key_apostrophe",
            "comma": "key_comma",
            "period": "key_dot",
            "slash": "key_slash",
        }
        if name in special:
            return special[name]
        if len(name) == 1 and name.isalpha():
            return f"key_{name}"
        if name.isdigit():
            return f"key_{name}"
        if name.startswith("f") and name[1:].isdigit():
            return f"key_{name}"
        return None

    def _build_navigation_tab(self) -> Gtk.Widget:
        return build_shared_navigation_tab(self, f_extra=F_EXTRA)

    def _build_media_tab(self) -> Gtk.Widget:
        return build_shared_media_tab(self, media_groups=MEDIA_KEY_GROUPS)

    def _build_mouse_tab(self) -> Gtk.Widget:
        box = build_shared_mouse_tab(self)
        box.append(Gtk.Separator())

        move_label = Gtk.Label(label="Move Cursor")
        move_label.add_css_class("dim-label")
        box.append(move_label)

        mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mode_row.set_halign(Gtk.Align.CENTER)

        self.mouse_move_rel_check = Gtk.CheckButton(label="Relative")
        self.mouse_move_rel_check.set_active(self._mouse_move_mode == "rel")
        self.mouse_move_rel_check.connect("toggled", self._on_mouse_move_mode_changed)
        mode_row.append(self.mouse_move_rel_check)

        self.mouse_move_abs_check = Gtk.CheckButton(label="Absolute")
        self.mouse_move_abs_check.set_group(self.mouse_move_rel_check)
        self.mouse_move_abs_check.set_active(self._mouse_move_mode == "abs")
        self.mouse_move_abs_check.connect("toggled", self._on_mouse_move_mode_changed)
        mode_row.append(self.mouse_move_abs_check)

        box.append(mode_row)

        coords_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        coords_row.set_halign(Gtk.Align.CENTER)

        x_label = Gtk.Label(label="X:")
        coords_row.append(x_label)
        self.mouse_move_x_spin = Gtk.SpinButton()
        self.mouse_move_x_spin.set_adjustment(
            Gtk.Adjustment(value=self._mouse_move_x, lower=-10000, upper=10000, step_increment=1)
        )
        self.mouse_move_x_spin.set_width_chars(6)
        coords_row.append(self.mouse_move_x_spin)

        y_label = Gtk.Label(label="Y:")
        coords_row.append(y_label)
        self.mouse_move_y_spin = Gtk.SpinButton()
        self.mouse_move_y_spin.set_adjustment(
            Gtk.Adjustment(value=self._mouse_move_y, lower=-10000, upper=10000, step_increment=1)
        )
        self.mouse_move_y_spin.set_width_chars(6)
        coords_row.append(self.mouse_move_y_spin)

        capture_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        capture_row.set_halign(Gtk.Align.CENTER)

        if not self._slurp_available:
            delay_label = Gtk.Label(label="Capture in:")
            capture_row.append(delay_label)

        self.mouse_move_capture_delay_spin = Gtk.SpinButton()
        self.mouse_move_capture_delay_spin.set_adjustment(
            Gtk.Adjustment(
                value=self._capture_delay_seconds,
                lower=0.2,
                upper=15.0,
                step_increment=0.2,
            )
        )
        self.mouse_move_capture_delay_spin.set_digits(1)
        self.mouse_move_capture_delay_spin.set_width_chars(4)
        self.mouse_move_capture_delay_spin.set_visible(not self._slurp_available)
        capture_row.append(self.mouse_move_capture_delay_spin)

        if not self._slurp_available:
            delay_suffix = Gtk.Label(label="s")
            capture_row.append(delay_suffix)

        btn_label = "Capture" if self._slurp_available else "Capture Position"
        self.mouse_move_capture_btn = Gtk.Button(label=btn_label)
        self.mouse_move_capture_btn.connect("clicked", self._on_capture_position_clicked)
        capture_row.append(self.mouse_move_capture_btn)

        self.mouse_move_capture_status = Gtk.Label(label="")
        self.mouse_move_capture_status.add_css_class("dim-label")
        self.mouse_move_capture_status.set_halign(Gtk.Align.START)
        capture_row.append(self.mouse_move_capture_status)

        self.mouse_move_capture_row = capture_row
        box.append(self.mouse_move_capture_row)

        move_map_btn = Gtk.Button(label="Map Move")
        move_map_btn.add_css_class("suggested-action")
        move_map_btn.connect("clicked", self._on_mouse_move_map_clicked)
        coords_row.append(move_map_btn)

        box.append(coords_row)

        self._update_mouse_move_mode_visibility()

        return box

    def _build_gamepad_tab(self) -> Gtk.Widget:
        choices = self._gamepad_output_choices()
        concrete_count = sum(1 for output_id, _label in choices if output_id is not None)
        if concrete_count <= 1 and not self._selected_gamepad_output_id:
            return build_shared_gamepad_tab(self)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_margin_top(8)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_start(12)
        row.set_margin_end(12)
        label = Gtk.Label(label="Output")
        label.set_xalign(0)
        label.set_hexpand(True)
        self._gamepad_output_ids = [output_id for output_id, _label in choices]
        dropdown = Gtk.DropDown.new_from_strings([label for _output_id, label in choices])
        selected = 0
        for index, output_id in enumerate(self._gamepad_output_ids):
            if output_id == self._selected_gamepad_output_id:
                selected = index
                break
        dropdown.set_selected(selected)
        dropdown.connect("notify::selected", self._on_gamepad_output_selected)
        self._gamepad_output_dropdown = dropdown
        row.append(label)
        row.append(dropdown)
        outer.append(row)
        outer.append(build_shared_gamepad_tab(self))
        return outer

    def _gamepad_output_choices(self) -> list[tuple[str | None, str]]:
        choices: list[tuple[str | None, str]] = [(None, "Default")]
        try:
            count = load_virtual_gamepad_count()
        except Exception:
            count = 1
        for index in range(1, count + 1):
            output_id = virtual_gamepad_output_id(index)
            label = "Virtual Gamepad 1" if index == 1 else f"Virtual Gamepad {index}"
            choices.append((output_id, label))

        try:
            hardware = HardwareManager()
            for config in hardware.list_hardware():
                if self._is_hardware_gamepad(config):
                    choices.append((config.hardware_id, config.hardware_id))
        except Exception:
            pass

        if self._selected_gamepad_output_id and all(
            output_id != self._selected_gamepad_output_id for output_id, _label in choices
        ):
            choices.append(
                (
                    self._selected_gamepad_output_id,
                    f"{self._selected_gamepad_output_id} (unknown)",
                )
            )
        return choices

    def _is_hardware_gamepad(self, config: object) -> bool:
        evdev_devices = getattr(config, "evdev_devices", []) or []
        for device in evdev_devices:
            device_type = getattr(device, "device_type", None)
            if getattr(device_type, "value", device_type) == "gamepad":
                return True
        return any(
            is_gamepad_button_name(getattr(button, "evdev", None))
            for button in getattr(config, "buttons", []) or []
        )

    def _on_gamepad_output_selected(self, dropdown: Gtk.DropDown, _param) -> None:
        selected = int(dropdown.get_selected())
        if 0 <= selected < len(self._gamepad_output_ids):
            self._selected_gamepad_output_id = self._gamepad_output_ids[selected]

    def _build_options_box(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row1.set_halign(Gtk.Align.START)

        self.rapidfire_check = Gtk.CheckButton(label="Rapidfire")
        self.rapidfire_check.set_active(self._rapidfire_enabled)
        self.rapidfire_check.set_tooltip_text(
            "Repeatedly send the mapped action while the button is held"
        )
        self.rapidfire_check.connect("toggled", self._on_rapidfire_toggled)
        row1.append(self.rapidfire_check)

        self.hold_label = Gtk.Label(label="Hold:")
        row1.append(self.hold_label)

        self.hold_spin = Gtk.SpinButton()
        hold_adj = Gtk.Adjustment(
            value=self._rapidfire_hold,
            lower=MIN_RAPIDFIRE_HOLD_MS,
            upper=1000,
            step_increment=1,
        )
        self.hold_spin.set_adjustment(hold_adj)
        row1.append(self.hold_spin)

        self.hold_ms_label = Gtk.Label(label="ms")
        row1.append(self.hold_ms_label)

        self.wait_label = Gtk.Label(label="Wait:")
        row1.append(self.wait_label)

        self.wait_spin = Gtk.SpinButton()
        wait_adj = Gtk.Adjustment(
            value=self._rapidfire_wait,
            lower=MIN_RAPIDFIRE_WAIT_MS,
            upper=1000,
            step_increment=1,
        )
        self.wait_spin.set_adjustment(wait_adj)
        row1.append(self.wait_spin)

        self.wait_ms_label = Gtk.Label(label="ms")
        row1.append(self.wait_ms_label)

        box.append(row1)

        row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row2.set_halign(Gtk.Align.START)

        self.tap_check = Gtk.CheckButton(label="Tap")
        self.tap_check.set_active(self._tap_enabled)
        self.tap_check.set_tooltip_text(
            "Send the action as a quick tap when the button is released within the hold window"
        )
        self.tap_check.connect("toggled", self._on_tap_toggled)
        row2.append(self.tap_check)

        self.tap_hold_label = Gtk.Label(label="Hold:")
        row2.append(self.tap_hold_label)

        self.tap_spin = Gtk.SpinButton()
        tap_adj = Gtk.Adjustment(value=self._tap_hold, lower=10, upper=500, step_increment=10)
        self.tap_spin.set_adjustment(tap_adj)
        row2.append(self.tap_spin)

        self.tap_ms_label = Gtk.Label(label="ms")
        row2.append(self.tap_ms_label)

        box.append(row2)

        self._update_options_visibility()

        return box

    def _update_options_visibility(self):
        rf_active = self._allow_rapidfire and self.rapidfire_check.get_active()
        tap_active = self._allow_tap and self.tap_check.get_active()

        self.rapidfire_check.set_visible(self._allow_rapidfire)
        self.tap_check.set_visible(self._allow_tap)

        self.hold_label.set_visible(rf_active)
        self.hold_spin.set_visible(rf_active)
        self.hold_ms_label.set_visible(rf_active)
        self.wait_label.set_visible(rf_active)
        self.wait_spin.set_visible(rf_active)
        self.wait_ms_label.set_visible(rf_active)

        self.tap_hold_label.set_visible(tap_active)
        self.tap_spin.set_visible(tap_active)
        self.tap_ms_label.set_visible(tap_active)

    def _create_key_button(
        self, label: str, evdev: str, width: float = 1, large: bool = False, protected: bool = False
    ) -> Gtk.Button:
        btn = Gtk.Button(label=label)
        btn.add_css_class("key-button")

        if large:
            btn.set_size_request(200, 50)
        else:
            base_width = 36
            btn.set_size_request(int(base_width * width), 34)

        if protected:
            btn.add_css_class("protected-key")
            btn.set_tooltip_text("Protected - cannot remap")

        btn._evdev_name = evdev
        btn._protected = protected
        return btn

    def _on_tab_changed(self, stack, param):
        child_name = self.stack.get_visible_child_name()
        is_special = child_name == "special"
        is_superkey = child_name == "superkey"
        is_macro = child_name == "macro"
        is_profile = child_name == "profile"
        is_exec = child_name == "exec"
        is_compositor_action = child_name in self._compositor_action_page_ids
        has_options = self._allow_rapidfire or self._allow_tap
        options_enabled = (
            not is_special
            and not is_superkey
            and not is_macro
            and not is_profile
            and not is_exec
            and not is_compositor_action
        )
        self.options_box.set_sensitive(options_enabled and has_options)
        self.options_box.set_visible(
            has_options
            and not is_superkey
            and not is_macro
            and not is_profile
            and not is_exec
            and not is_compositor_action
        )
        self.map_btn.set_visible(is_superkey or is_macro or is_profile)
        if is_superkey:
            self.map_btn.set_sensitive(self._selected_superkey is not None)
        elif is_macro:
            self.map_btn.set_sensitive(self._selected_macro is not None)
        elif is_profile:
            self.map_btn.set_sensitive(bool(self._selected_profile_name))
        else:
            self.map_btn.set_sensitive(False)
        self._update_actions_docs_button()

    def _active_actions_docs_link(self) -> tuple[str, str] | None:
        child_name = self.stack.get_visible_child_name()
        if not child_name:
            return None
        return ACTION_DOC_LINKS.get(child_name)

    def _update_actions_docs_button(self) -> None:
        if not hasattr(self, "actions_docs_btn"):
            return
        link = self._active_actions_docs_link()
        self.actions_docs_btn.set_visible(link is not None)
        if link is None:
            return
        _anchor, title = link
        self.actions_docs_btn.set_tooltip_text(f"Open {title} documentation")

    def _on_actions_docs_clicked(self, _button: Gtk.Button) -> None:
        link = self._active_actions_docs_link()
        if link is None:
            return
        anchor, _title = link
        url = _actions_docs_url(anchor)
        try:
            launcher = Gtk.UriLauncher.new(url)
            launcher.launch(None, None, None)
        except Exception as exc:
            log.warning("Could not open action documentation %s: %s", url, exc)

    def _warn_and_clear_unsupported_rapidfire(self, action_type: ActionType) -> None:
        if not self._rapidfire_enabled or action_type_supports_rapidfire(action_type):
            return
        log.warning(
            "Ignoring rapidfire for unsupported %s action in key selector",
            action_type.value,
        )
        if self.rapidfire_check.get_active():
            self.rapidfire_check.set_active(False)
        else:
            self._rapidfire_enabled = False
            self._update_options_visibility()

    def _on_rapidfire_toggled(self, check):
        if not self._allow_rapidfire:
            return
        self._rapidfire_enabled = check.get_active()
        if self._rapidfire_enabled:
            self.tap_check.set_active(False)
            self._tap_enabled = False
        self._update_options_visibility()

    def _on_tap_toggled(self, check):
        if not self._allow_tap:
            return
        self._tap_enabled = check.get_active()
        if self._tap_enabled:
            self.rapidfire_check.set_active(False)
            self._rapidfire_enabled = False
        self._update_options_visibility()

    def _on_special_clicked(self, btn, action_type: str):
        if action_type == "clear_mapping":
            self.emit("key-selected", None)
        elif action_type == "explicit_passthrough":
            self._warn_and_clear_unsupported_rapidfire(ActionType.PASSTHROUGH)
            action = MappingAction(action_type=ActionType.PASSTHROUGH)
            self.emit("key-selected", action)
        elif action_type == "suppress":
            self._warn_and_clear_unsupported_rapidfire(ActionType.SUPPRESS)
            action = MappingAction(action_type=ActionType.SUPPRESS)
            self.emit("key-selected", action)
        elif action_type == "start_macro_recording":
            self._warn_and_clear_unsupported_rapidfire(ActionType.START_MACRO_RECORDING)
            action = MappingAction(action_type=ActionType.START_MACRO_RECORDING)
            self.emit("key-selected", action)
        elif action_type == "stop_macro_recording":
            self._warn_and_clear_unsupported_rapidfire(ActionType.STOP_MACRO_RECORDING)
            action = MappingAction(action_type=ActionType.STOP_MACRO_RECORDING)
            self.emit("key-selected", action)
        elif action_type == "cancel_macro_playback":
            self._warn_and_clear_unsupported_rapidfire(ActionType.CANCEL_MACRO_PLAYBACK)
            action = MappingAction(action_type=ActionType.CANCEL_MACRO_PLAYBACK)
            self.emit("key-selected", action)
        self.close()

    def _on_exec_text_changed(self, entry: Gtk.Entry) -> None:
        self.exec_map_btn.set_sensitive(bool(entry.get_text().strip()))

    def _on_exec_map_clicked(self, btn: Gtk.Button) -> None:
        cmd = self.exec_entry.get_text().strip()
        if not cmd:
            return
        self._warn_and_clear_unsupported_rapidfire(ActionType.EXEC)
        action = MappingAction(action_type=ActionType.EXEC, cmd=cmd)
        self.emit("key-selected", action)
        self.close()

    def _on_compositor_action_selected(self, action: MappingAction) -> None:
        self._warn_and_clear_unsupported_rapidfire(ActionType.COMPOSITOR_DISPATCH)
        self.emit("key-selected", action)
        self.close()

    def _on_keyboard_clicked(self, btn, evdev_name: str):
        action = MappingAction(
            action_type=ActionType.KEYBOARD,
            target=evdev_name,
            rapidfire_enabled=self._rapidfire_enabled,
            rapidfire_hold_ms=int(self.hold_spin.get_value()),
            rapidfire_wait_ms=int(self.wait_spin.get_value()),
            tap_enabled=self._tap_enabled,
            tap_hold_ms=int(self.tap_spin.get_value()),
        )
        self.emit("key-selected", action)
        self.close()

    def _on_f_key_selected(self, btn):
        idx = self.f_dropdown.get_selected()
        f_key = F_EXTRA[idx]
        evdev_name = KEY_TO_EVDEV.get(f_key)
        if evdev_name:
            self._on_keyboard_clicked(btn, evdev_name)

    def _on_mouse_clicked(self, btn, evdev_name: str):
        action = MappingAction(
            action_type=ActionType.MOUSE,
            target=evdev_name,
            rapidfire_enabled=self._rapidfire_enabled,
            rapidfire_hold_ms=int(self.hold_spin.get_value()),
            rapidfire_wait_ms=int(self.wait_spin.get_value()),
            tap_enabled=self._tap_enabled,
            tap_hold_ms=int(self.tap_spin.get_value()),
        )
        self.emit("key-selected", action)
        self.close()

    def _on_mouse_move_map_clicked(self, btn) -> None:
        x = int(self.mouse_move_x_spin.get_value())
        y = int(self.mouse_move_y_spin.get_value())
        if self.mouse_move_abs_check.get_active():
            action_type = ActionType.MOUSE_MOVE_ABS
        else:
            action_type = ActionType.MOUSE_MOVE_REL
        action = MappingAction(
            action_type=action_type,
            move_x=x,
            move_y=y,
            rapidfire_enabled=self._rapidfire_enabled,
            rapidfire_hold_ms=int(self.hold_spin.get_value()),
            rapidfire_wait_ms=int(self.wait_spin.get_value()),
            tap_enabled=self._tap_enabled,
            tap_hold_ms=int(self.tap_spin.get_value()),
        )
        self.emit("key-selected", action)
        self.close()

    def _on_mouse_move_mode_changed(self, check: Gtk.CheckButton) -> None:
        self._update_mouse_move_mode_visibility()

    def _update_mouse_move_mode_visibility(self) -> None:
        is_abs = self.mouse_move_abs_check.get_active()
        self.mouse_move_capture_row.set_visible(is_abs)
        if not is_abs:
            self._cancel_capture_position("")

    def _on_capture_position_clicked(self, btn: Gtk.Button) -> None:
        self._cancel_capture_position("")
        self._capture_request_id += 1
        request_id = self._capture_request_id

        if self._slurp_available:
            self._capture_pending = True
            self.mouse_move_capture_btn.set_sensitive(False)
            self.mouse_move_capture_status.set_text("Click to capture position...")
            self._slurp_capture.capture_point(
                lambda result, expected_id=request_id: self._on_slurp_capture_result(
                    expected_id, result
                )
            )
        else:
            self._capture_delay_seconds = float(self.mouse_move_capture_delay_spin.get_value())
            self._capture_pending = True
            self.mouse_move_capture_btn.set_sensitive(False)
            self.mouse_move_capture_status.set_text(
                f"Move cursor now... capturing in {self._capture_delay_seconds:.1f}s"
            )
            self._capture_timeout_id = GLib.timeout_add(
                int(self._capture_delay_seconds * 1000),
                lambda expected_id=request_id: self._capture_position_after_delay(expected_id),
            )

    def _on_slurp_capture_result(self, request_id: int, result) -> None:
        if request_id != self._capture_request_id:
            return
        self._capture_pending = False
        self.mouse_move_capture_btn.set_sensitive(True)

        if result is None:
            self.mouse_move_capture_status.set_text("Capture cancelled or failed")
            return

        self.mouse_move_x_spin.set_value(result.x)
        self.mouse_move_y_spin.set_value(result.y)
        self.mouse_move_capture_status.set_text(f"Captured: {result.x}, {result.y}")

    def _capture_position_after_delay(self, request_id: int) -> bool:
        self._capture_timeout_id = 0
        if request_id != self._capture_request_id or not self._capture_pending:
            return False
        self.mouse_move_capture_status.set_text("Reading cursor position...")
        session_request_async(
            {"command": "get_cursor_position"},
            lambda response, expected_id=request_id: self._on_capture_position_response(
                expected_id, response
            ),
            timeout=5.0,
        )
        return False

    def _on_capture_position_response(self, request_id: int, response: dict | None) -> bool:
        if request_id != self._capture_request_id:
            return False
        self._capture_pending = False
        self.mouse_move_capture_btn.set_sensitive(True)

        if not response or response.get("status") != "ok":
            message = (
                (response or {}).get("message") or (response or {}).get("error") or "Capture failed"
            )
            if "Unknown command: get_cursor_position" in message:
                message = "Please restart Keymasq Session, then try again"
            self.mouse_move_capture_status.set_text(message)
            return False

        self.mouse_move_x_spin.set_value(int(response.get("x", 0)))
        self.mouse_move_y_spin.set_value(int(response.get("y", 0)))
        self.mouse_move_capture_status.set_text("Captured")
        return False

    def _cancel_capture_position(self, status_text: str) -> None:
        self._capture_request_id += 1
        if self._capture_timeout_id:
            GLib.source_remove(self._capture_timeout_id)
            self._capture_timeout_id = 0
        self._capture_pending = False
        if hasattr(self, "mouse_move_capture_btn"):
            self.mouse_move_capture_btn.set_sensitive(True)
        if hasattr(self, "mouse_move_capture_status"):
            self.mouse_move_capture_status.set_text(status_text)

    def close(self) -> None:
        self._cancel_capture_position("")
        super().close()

    def _on_gamepad_clicked(self, btn, evdev_name: str):
        action = MappingAction(
            action_type=ActionType.GAMEPAD,
            target=evdev_name,
            output_id=self._selected_gamepad_output_id,
            rapidfire_enabled=self._rapidfire_enabled,
            rapidfire_hold_ms=int(self.hold_spin.get_value()),
            rapidfire_wait_ms=int(self.wait_spin.get_value()),
            tap_enabled=self._tap_enabled,
            tap_hold_ms=int(self.tap_spin.get_value()),
        )
        self.emit("key-selected", action)
        self.close()

    def _build_macro_tab(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        toolbar_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar_row.set_margin_top(8)
        toolbar_row.set_margin_bottom(4)
        toolbar_row.set_margin_start(12)
        toolbar_row.set_margin_end(12)
        toolbar_row.set_halign(Gtk.Align.START)

        refresh_btn = Gtk.Button(label="Refresh")
        refresh_btn.add_css_class("flat")
        refresh_btn.connect("clicked", self._on_macro_refresh)
        toolbar_row.append(refresh_btn)
        outer.append(toolbar_row)

        controls_label = Gtk.Label(label="Macro Controls")
        controls_label.add_css_class("dim-label")
        controls_label.set_halign(Gtk.Align.CENTER)
        controls_label.set_margin_top(4)
        outer.append(controls_label)

        controls_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls_row.set_halign(Gtk.Align.CENTER)
        controls_row.set_margin_bottom(8)

        toggle_rec_btn = self._create_key_button(
            "Toggle Recording", "start_macro_recording", width=2.0
        )
        toggle_rec_btn.connect("clicked", self._on_macro_special_action_clicked, "toggle_recording")
        toggle_rec_btn.set_tooltip_text("Start recording when idle, stop when recording is active")
        controls_row.append(toggle_rec_btn)

        cancel_macro_btn = self._create_key_button(
            "Cancel Playback", "cancel_macro_playback", width=2.0
        )
        cancel_macro_btn.connect(
            "clicked", self._on_macro_special_action_clicked, "cancel_macro_playback"
        )
        cancel_macro_btn.set_tooltip_text("Stop currently running macro playback")
        controls_row.append(cancel_macro_btn)

        outer.append(controls_row)

        controls_spacer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        controls_spacer.set_size_request(-1, 6)
        outer.append(controls_spacer)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        self._macro_listbox = Gtk.ListBox()
        self._macro_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._macro_listbox.set_valign(Gtk.Align.START)
        self._macro_listbox.add_css_class("boxed-list")
        self._macro_listbox.set_margin_start(12)
        self._macro_listbox.set_margin_end(12)
        self._macro_listbox.connect("row-selected", self._on_macro_row_selected)
        scrolled.set_child(self._macro_listbox)
        outer.append(scrolled)

        self._macro_options_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._macro_options_box.set_margin_top(8)
        self._macro_options_box.set_margin_start(12)
        self._macro_options_box.set_margin_end(12)
        self._macro_options_box.set_visible(False)

        opt_row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        opt_row1.set_halign(Gtk.Align.START)

        self._macro_movement_check = Gtk.CheckButton(label="Replay mouse movement")
        self._macro_movement_check.set_active(self._macro_replay_movement)
        self._macro_movement_check.connect("toggled", self._on_macro_movement_toggled)
        opt_row1.append(self._macro_movement_check)

        self._macro_clicks_check = Gtk.CheckButton(label="Replay mouse clicks")
        self._macro_clicks_check.set_active(self._macro_replay_clicks)
        self._macro_clicks_check.connect("toggled", self._on_macro_clicks_toggled)
        opt_row1.append(self._macro_clicks_check)

        self._macro_options_box.append(opt_row1)

        opt_row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        opt_row2.set_halign(Gtk.Align.START)

        speed_label = Gtk.Label(label="Speed:")
        opt_row2.append(speed_label)

        self._macro_speed_spin = Gtk.SpinButton()
        speed_adj = Gtk.Adjustment(
            value=self._macro_speed, lower=0.1, upper=10.0, step_increment=0.1
        )
        self._macro_speed_spin.set_adjustment(speed_adj)
        self._macro_speed_spin.set_digits(1)
        self._macro_speed_spin.connect("value-changed", self._on_macro_speed_changed)
        opt_row2.append(self._macro_speed_spin)

        speed_suffix = Gtk.Label(label="×")
        opt_row2.append(speed_suffix)

        self._macro_options_box.append(opt_row2)
        outer.append(self._macro_options_box)

        GLib.idle_add(self._load_macro_list)
        return outer

    def _build_profile_tab(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        outer.set_margin_start(12)
        outer.set_margin_end(12)

        title = Gtk.Label(label="Profile Controls")
        title.add_css_class("title-4")
        title.set_halign(Gtk.Align.START)
        outer.append(title)

        subtitle = Gtk.Label(label="Trigger global profile enable/disable/toggle.")
        subtitle.add_css_class("dim-label")
        subtitle.set_wrap(True)
        subtitle.set_halign(Gtk.Align.START)
        outer.append(subtitle)

        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_row.set_halign(Gtk.Align.START)

        action_label = Gtk.Label(label="Action")
        action_label.set_size_request(90, -1)
        action_label.set_halign(Gtk.Align.START)
        action_row.append(action_label)

        self._profile_action_dropdown = Gtk.DropDown()
        action_model = Gtk.StringList()
        action_model.append("Toggle")
        action_model.append("Enable")
        action_model.append("Disable")
        self._profile_action_dropdown.set_model(action_model)
        self._profile_action_dropdown.set_selected(
            {"toggle": 0, "enable": 1, "disable": 2}.get(self._selected_profile_action, 0)
        )
        self._profile_action_dropdown.connect("notify::selected", self._on_profile_action_changed)
        action_row.append(self._profile_action_dropdown)
        outer.append(action_row)

        profile_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        profile_row.set_halign(Gtk.Align.START)

        profile_label = Gtk.Label(label="Profile")
        profile_label.set_size_request(90, -1)
        profile_label.set_halign(Gtk.Align.START)
        profile_row.append(profile_label)

        self._profile_name_dropdown = Gtk.DropDown()
        self._profile_name_model = Gtk.StringList()
        self._profile_name_dropdown.set_model(self._profile_name_model)
        self._profile_name_dropdown.set_size_request(360, -1)
        self._profile_name_dropdown.connect("notify::selected", self._on_profile_name_changed)
        profile_row.append(self._profile_name_dropdown)
        outer.append(profile_row)

        self._profile_hint_label = Gtk.Label(label="")
        self._profile_hint_label.add_css_class("dim-label")
        self._profile_hint_label.set_halign(Gtk.Align.START)
        self._profile_hint_label.set_wrap(True)
        outer.append(self._profile_hint_label)

        GLib.idle_add(self._load_profile_overview)
        return outer

    def _load_profile_overview(self) -> bool:
        session_request_async({"command": "list_profiles"}, self._on_profile_overview_loaded)
        return False

    def _on_profile_overview_loaded(self, result: dict | None) -> bool:
        result = result or {}
        self._profile_entries = result.get("profiles", []) if result.get("status") == "ok" else []
        self._populate_profile_names()
        return False

    def _on_profile_action_changed(self, dropdown, _pspec) -> None:
        idx = dropdown.get_selected()
        if idx == 1:
            self._selected_profile_action = "enable"
        elif idx == 2:
            self._selected_profile_action = "disable"
        else:
            self._selected_profile_action = "toggle"
        self._update_profile_hint()

    def _populate_profile_names(self) -> None:
        self._profile_name_items = []
        while self._profile_name_model.get_n_items() > 0:
            self._profile_name_model.remove(0)

        for profile in self._profile_entries:
            name = str(profile.get("name", "") or "")
            if not name:
                continue
            self._profile_name_items.append(name)
            enabled = bool(profile.get("enabled", True))
            marker = "" if enabled else " [disabled]"
            self._profile_name_model.append(f"{name}{marker}")

        if not self._profile_name_items:
            self._selected_profile_name = ""
            self._update_profile_hint()
            self._on_tab_changed(self.stack, None)
            return

        selected_index = 0
        if self._selected_profile_name in self._profile_name_items:
            selected_index = self._profile_name_items.index(self._selected_profile_name)
        else:
            self._selected_profile_name = self._profile_name_items[0]

        self._profile_name_dropdown.set_selected(selected_index)
        self._on_profile_name_changed(self._profile_name_dropdown, None)

    def _on_profile_name_changed(self, dropdown, _pspec) -> None:
        idx = int(dropdown.get_selected())
        if idx < 0 or idx >= len(self._profile_name_items):
            self._selected_profile_name = ""
        else:
            self._selected_profile_name = self._profile_name_items[idx]
        self._update_profile_hint()
        self._on_tab_changed(self.stack, None)

    def _update_profile_hint(self) -> None:
        if not self._selected_profile_name:
            self._profile_hint_label.set_label("Select a profile to map this action.")
            return

        verb = {
            "toggle": "Toggle",
            "enable": "Enable",
            "disable": "Disable",
        }.get(self._selected_profile_action, "Toggle")
        self._profile_hint_label.set_label(f"{verb} profile '{self._selected_profile_name}'.")

    def _load_superkey_list(self) -> None:
        manager = SuperkeyManager()
        configs = manager.get_all_superkeys()
        self._superkey_names = manager.list_superkeys()
        self._superkey_list = [
            config for name in self._superkey_names if (config := configs.get(name)) is not None
        ]
        self._populate_superkey_listbox()

    def _populate_superkey_listbox(self) -> None:
        while self._superkey_listbox.get_first_child():
            self._superkey_listbox.remove(self._superkey_listbox.get_first_child())

        if not self._superkey_list:
            self._selected_superkey = None
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            lbl = Gtk.Label(label="No super keys saved yet")
            lbl.add_css_class("dim-label")
            lbl.set_margin_top(12)
            lbl.set_margin_bottom(12)
            row.set_child(lbl)
            self._superkey_listbox.append(row)
            return

        selected_row: Gtk.ListBoxRow | None = None
        for config in self._superkey_list:
            row = Gtk.ListBoxRow()
            row._superkey_name = config.name

            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row_box.set_margin_top(8)
            row_box.set_margin_bottom(8)
            row_box.set_margin_start(12)
            row_box.set_margin_end(12)

            name_label = Gtk.Label(label=config.name)
            name_label.set_halign(Gtk.Align.START)
            name_label.set_hexpand(True)
            row_box.append(name_label)

            info_label = Gtk.Label(label=self._describe_superkey_row(config))
            info_label.add_css_class("dim-label")
            info_label.add_css_class("caption")
            row_box.append(info_label)

            row.set_child(row_box)
            self._superkey_listbox.append(row)

            if self._selected_superkey and config.name == self._selected_superkey:
                selected_row = row

        if selected_row is not None:
            self._superkey_listbox.select_row(selected_row)
        elif self._selected_superkey:
            self._selected_superkey = None

    def _describe_superkey_row(self, config: SuperkeyConfig) -> str:
        if config.mode.value == "overload":
            count = (
                len(config.overload_actions)
                + len(config.overload_down_actions)
                + len(config.overload_up_actions)
            )
            noun = "action" if count == 1 else "actions"
            suffix = (
                " · down/up"
                if config.overload_down_actions or config.overload_up_actions
                else ""
            )
            return f"Overload{suffix} · {count} {noun}"

        slots = sum(
            1
            for actions in (
                config.tap_actions,
                config.double_tap_actions,
                config.hold_actions,
                config.tap_hold_actions,
            )
            if actions
        )
        noun = "slot" if slots == 1 else "slots"
        return f"Pattern · {slots} {noun}"

    def _on_superkey_refresh(self, btn) -> None:
        self._load_superkey_list()

    def _on_superkey_row_selected(self, listbox, row) -> None:
        if row and hasattr(row, "_superkey_name"):
            self._selected_superkey = row._superkey_name
        else:
            self._selected_superkey = None
        if self.stack.get_visible_child_name() == "superkey":
            self.map_btn.set_sensitive(self._selected_superkey is not None)

    def _load_macro_list(self) -> bool:
        session_request_async({"command": "list_macros"}, self._on_macro_list_loaded)
        return False

    def _on_macro_list_loaded(self, result: dict | None) -> bool:
        self._macro_list = (result or {}).get("macros", [])
        self._populate_macro_listbox()
        return False

    def _populate_macro_listbox(self) -> None:
        while self._macro_listbox.get_first_child():
            self._macro_listbox.remove(self._macro_listbox.get_first_child())

        if not self._macro_list:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            lbl = Gtk.Label(label="No macros saved yet")
            lbl.add_css_class("dim-label")
            lbl.set_margin_top(12)
            lbl.set_margin_bottom(12)
            row.set_child(lbl)
            self._macro_listbox.append(row)
            return

        for macro in self._macro_list:
            row = Gtk.ListBoxRow()
            row._macro_name = macro["name"]

            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row_box.set_margin_top(8)
            row_box.set_margin_bottom(8)
            row_box.set_margin_start(12)
            row_box.set_margin_end(12)

            name_label = Gtk.Label(label=macro["name"])
            name_label.set_halign(Gtk.Align.START)
            name_label.set_hexpand(True)
            row_box.append(name_label)

            duration_s = macro.get("duration_us", 0) / 1_000_000
            device_types = ", ".join(macro.get("device_types", []))
            info_label = Gtk.Label(label=f"{duration_s:.1f}s · {device_types}")
            info_label.add_css_class("dim-label")
            info_label.add_css_class("caption")
            row_box.append(info_label)

            row.set_child(row_box)
            self._macro_listbox.append(row)

        if self._selected_macro:
            for i, macro in enumerate(self._macro_list):
                if macro["name"] == self._selected_macro:
                    row = self._macro_listbox.get_row_at_index(i)
                    if row:
                        self._macro_listbox.select_row(row)
                    break

    def _on_macro_refresh(self, btn) -> None:
        self._load_macro_list()

    def _on_macro_row_selected(self, listbox, row) -> None:
        if row and hasattr(row, "_macro_name"):
            self._selected_macro = row._macro_name
            self._macro_options_box.set_visible(self._allow_macro_options)
        else:
            self._selected_macro = None
            self._macro_options_box.set_visible(False)
        self.map_btn.set_sensitive(self._selected_macro is not None)

    def _on_macro_movement_toggled(self, check) -> None:
        self._macro_replay_movement = check.get_active()

    def _on_macro_clicks_toggled(self, check) -> None:
        self._macro_replay_clicks = check.get_active()

    def _on_macro_speed_changed(self, spin) -> None:
        self._macro_speed = spin.get_value()

    def _on_macro_special_action_clicked(self, _btn, action_name: str) -> None:
        if action_name == "toggle_recording":
            self._warn_and_clear_unsupported_rapidfire(ActionType.START_MACRO_RECORDING)
            action = MappingAction(action_type=ActionType.START_MACRO_RECORDING)
        elif action_name == "cancel_macro_playback":
            self._warn_and_clear_unsupported_rapidfire(ActionType.CANCEL_MACRO_PLAYBACK)
            action = MappingAction(action_type=ActionType.CANCEL_MACRO_PLAYBACK)
        else:
            return
        self.emit("key-selected", action)
        self.close()

    def _on_map_clicked(self, btn) -> None:
        child_name = self.stack.get_visible_child_name()
        if child_name == "superkey":
            self._on_superkey_map_clicked(btn)
        elif child_name == "macro":
            self._on_macro_map_clicked(btn)
        elif child_name == "profile":
            self._on_profile_map_clicked(btn)

    def _on_superkey_map_clicked(self, btn) -> None:
        if not self._selected_superkey:
            return
        self._warn_and_clear_unsupported_rapidfire(ActionType.SUPERKEY)
        action = MappingAction(
            action_type=ActionType.SUPERKEY,
            superkey_name=self._selected_superkey,
        )
        self.emit("key-selected", action)
        self.close()

    def _on_macro_map_clicked(self, btn) -> None:
        if not self._selected_macro:
            return
        self._warn_and_clear_unsupported_rapidfire(ActionType.MACRO)
        action = MappingAction(
            action_type=ActionType.MACRO,
            macro_name=self._selected_macro,
            macro_replay_mouse_movement=self._macro_replay_movement,
            macro_replay_mouse_clicks=self._macro_replay_clicks,
            macro_speed=self._macro_speed,
        )
        self.emit("key-selected", action)
        self.close()

    def _on_profile_map_clicked(self, btn) -> None:
        if not self._selected_profile_name:
            return

        action_type = ActionType.PROFILE_TOGGLE
        if self._selected_profile_action == "enable":
            action_type = ActionType.PROFILE_ENABLE
        elif self._selected_profile_action == "disable":
            action_type = ActionType.PROFILE_DISABLE

        self._warn_and_clear_unsupported_rapidfire(action_type)
        action = MappingAction(
            action_type=action_type,
            profile_name=self._selected_profile_name,
        )
        self.emit("key-selected", action)
        self.close()

    def _set_initial_tab(self):
        if not self._current_action:
            return
        compositor_tab = compositor_action_tab_name(
            self._current_action,
            self._compositor_action_status,
        )
        if compositor_tab not in self._compositor_action_page_ids:
            compositor_tab = None
        tab_map = {
            ActionType.PASSTHROUGH: "special",
            ActionType.SUPPRESS: "special",
            ActionType.SUPERKEY: "superkey",
            ActionType.START_MACRO_RECORDING: "macro",
            ActionType.STOP_MACRO_RECORDING: "macro",
            ActionType.CANCEL_MACRO_PLAYBACK: "macro",
            ActionType.EMERGENCY_RESET: "macro",
            ActionType.EXEC: "special",
            ActionType.KEYBOARD: (
                "media" if self._current_action.target in MEDIA_KEY_TARGETS else "keyboard"
            ),
            ActionType.MOUSE: "mouse",
            ActionType.MOUSE_MOVE_REL: "mouse",
            ActionType.MOUSE_MOVE_ABS: "mouse",
            ActionType.GAMEPAD: "gamepad",
            ActionType.MACRO: "macro",
            ActionType.PROFILE_ENABLE: "profile",
            ActionType.PROFILE_DISABLE: "profile",
            ActionType.PROFILE_TOGGLE: "profile",
        }
        name = compositor_tab or tab_map.get(self._current_action.action_type)
        if name == "superkey" and not self._allow_superkey:
            return
        if name:
            self.stack.set_visible_child_name(name)

    def _resolve_compositor_action_status(
        self,
        compositor_action_status: dict[str, object] | None,
    ) -> dict[str, bool | str | None]:
        resolved: dict[str, bool | str | None] = {
            "compositor_id": None,
            "listener_name": None,
            "compositor_dispatch_available": False,
        }
        if isinstance(compositor_action_status, dict):
            for key in resolved:
                value = compositor_action_status.get(key)
                if isinstance(value, (bool, str)) or value is None:
                    resolved[key] = value
            return resolved

        root = self._parent.get_root() if hasattr(self._parent, "get_root") else None
        get_status = getattr(root, "get_compositor_action_status", None)
        if callable(get_status):
            status = get_status()
            if isinstance(status, dict):
                for key in resolved:
                    value = status.get(key)
                    if isinstance(value, (bool, str)) or value is None:
                        resolved[key] = value
        return resolved

    def _describe_current_action(self) -> str:
        return describe_mapping_action_verbose(
            self._current_action,
            keyboard_label=lambda value: EVDEV_TO_KEY.get(value, value),
            gamepad_label=lambda value: EVDEV_TO_GAMEPAD.get(value, value),
        )

    def _on_f_dropdown_changed(self, dropdown, pspec, btn: Gtk.Button):
        idx = dropdown.get_selected()
        f_key = F_EXTRA[idx]
        btn.set_label(f"Map {f_key}")


class SuperkeyActionDialog(Adw.Dialog):
    __gsignals__ = {
        "action-selected": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(
        self,
        parent: Gtk.Widget,
        action_type: str,
        current_action: SuperkeyAction | None = None,
    ):
        self._action_type = action_type
        self._current_action = current_action
        self._parent = parent

        title = f"Configure {action_type.replace('_', ' ').title()} Action"
        super().__init__(title=title, content_width=540, content_height=520)

        self._rapidfire_enabled = False
        self._rapidfire_hold = 20
        self._rapidfire_wait = 20
        self._superkey_macro_list: list[dict] = []
        self._superkey_selected_macro: str | None = None
        self._selected_gamepad_output_id: str | None = (
            current_action.output_id
            if current_action and current_action.action_type == ActionType.GAMEPAD
            else None
        )
        self._gamepad_output_ids: list[str | None] = []

        if current_action:
            self._rapidfire_enabled = current_action.rapidfire_enabled
            self._rapidfire_hold = current_action.rapidfire_hold_ms
            self._rapidfire_wait = current_action.rapidfire_wait_ms
            if current_action.action_type == ActionType.MACRO:
                self._superkey_selected_macro = current_action.macro_name

        self._build_ui()

    def _build_ui(self):
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main_box.set_margin_top(12)
        main_box.set_margin_bottom(12)
        main_box.set_margin_start(12)
        main_box.set_margin_end(12)

        self.options_box = self._build_options_box()

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.connect("notify::visible-child", self._on_tab_changed)
        self.stack.add_titled(self._build_keyboard_tab(), "keyboard", "Keyboard")
        self.stack.add_titled(self._build_navigation_tab(), "navigation", "Navigation")
        self.stack.add_titled(self._build_media_tab(), "media", "Media")
        self.stack.add_titled(self._build_mouse_tab(), "mouse", "Mouse")
        self.stack.add_titled(self._build_gamepad_tab(), "gamepad", "Gamepad")
        self.stack.add_titled(self._build_macro_tab(), "macro", "Macro")
        self.stack.add_titled(self._build_exec_tab(), "exec", "Command")

        self._set_initial_tab()

        frame = Gtk.Frame()
        frame.set_vexpand(True)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        title_label = Gtk.Label(label=self.get_title())
        title_label.add_css_class("title-3")
        title_label.set_halign(Gtk.Align.CENTER)
        title_label.set_margin_bottom(12)
        inner.append(title_label)

        inner.append(Gtk.Separator())

        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self.stack)
        switcher.set_halign(Gtk.Align.CENTER)
        switcher.set_margin_top(8)
        switcher.set_margin_bottom(8)
        inner.append(switcher)

        inner.append(Gtk.Separator())
        inner.append(self.stack)
        inner.append(Gtk.Separator())

        options_pad = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        options_pad.set_margin_top(8)
        options_pad.set_margin_bottom(8)
        options_pad.set_margin_start(12)
        options_pad.set_margin_end(12)
        options_pad.append(self.options_box)
        inner.append(options_pad)

        inner.append(Gtk.Separator())

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_hexpand(True)
        btn_box.set_margin_top(8)
        btn_box.set_margin_bottom(8)
        btn_box.set_margin_start(12)
        btn_box.set_margin_end(12)

        self.actions_docs_btn = _create_actions_docs_button()
        self.actions_docs_btn.connect("clicked", self._on_actions_docs_clicked)
        btn_box.append(self.actions_docs_btn)

        footer_spacer = Gtk.Box()
        footer_spacer.set_hexpand(True)
        btn_box.append(footer_spacer)

        clear_btn = Gtk.Button(label="Clear")
        clear_btn.connect("clicked", self._on_clear_clicked)
        btn_box.append(clear_btn)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_cancel_clicked)
        btn_box.append(cancel_btn)

        inner.append(btn_box)

        frame.set_child(inner)
        main_box.append(frame)
        self.set_child(main_box)

        GLib.idle_add(self._load_superkey_macro_list)

    def _on_cancel_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _build_options_box(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        is_hold = self._action_type in ("hold", "tap_hold")

        if is_hold:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.set_halign(Gtk.Align.START)

            self.rapidfire_check = Gtk.CheckButton(label="Rapidfire")
            self.rapidfire_check.set_active(self._rapidfire_enabled)
            self.rapidfire_check.set_tooltip_text("Repeatedly send while held")
            self.rapidfire_check.connect("toggled", self._on_rapidfire_toggled)
            row.append(self.rapidfire_check)

            self.hold_label = Gtk.Label(label="Hold:")
            row.append(self.hold_label)

            self.hold_spin = Gtk.SpinButton()
            hold_adj = Gtk.Adjustment(
                value=self._rapidfire_hold,
                lower=MIN_RAPIDFIRE_HOLD_MS,
                upper=1000,
                step_increment=1,
            )
            self.hold_spin.set_adjustment(hold_adj)
            row.append(self.hold_spin)

            self.hold_ms_label = Gtk.Label(label="ms")
            row.append(self.hold_ms_label)

            self.wait_label = Gtk.Label(label="Wait:")
            row.append(self.wait_label)

            self.wait_spin = Gtk.SpinButton()
            wait_adj = Gtk.Adjustment(
                value=self._rapidfire_wait,
                lower=MIN_RAPIDFIRE_WAIT_MS,
                upper=1000,
                step_increment=1,
            )
            self.wait_spin.set_adjustment(wait_adj)
            row.append(self.wait_spin)

            self.wait_ms_label = Gtk.Label(label="ms")
            row.append(self.wait_ms_label)

            box.append(row)
            self._update_options_visibility()
        else:
            self.rapidfire_check = None

        return box

    def _update_options_visibility(self):
        if self.rapidfire_check:
            rf_active = self.rapidfire_check.get_active()
            self.hold_label.set_visible(rf_active)
            self.hold_spin.set_visible(rf_active)
            self.hold_ms_label.set_visible(rf_active)
            self.wait_label.set_visible(rf_active)
            self.wait_spin.set_visible(rf_active)
            self.wait_ms_label.set_visible(rf_active)

    def _on_tab_changed(self, stack, param):
        is_exec = self.stack.get_visible_child_name() == "exec"
        if self.rapidfire_check:
            self.options_box.set_visible(not is_exec)
        self._update_actions_docs_button()

    def _active_actions_docs_link(self) -> tuple[str, str] | None:
        child_name = self.stack.get_visible_child_name()
        if not child_name:
            return None
        return ACTION_DOC_LINKS.get(child_name)

    def _update_actions_docs_button(self) -> None:
        if not hasattr(self, "actions_docs_btn"):
            return
        link = self._active_actions_docs_link()
        self.actions_docs_btn.set_visible(link is not None)
        if link is None:
            return
        _anchor, title = link
        self.actions_docs_btn.set_tooltip_text(f"Open {title} documentation")

    def _on_actions_docs_clicked(self, _button: Gtk.Button) -> None:
        link = self._active_actions_docs_link()
        if link is None:
            return
        anchor, _title = link
        url = _actions_docs_url(anchor)
        try:
            launcher = Gtk.UriLauncher.new(url)
            launcher.launch(None, None, None)
        except Exception as exc:
            log.warning("Could not open action documentation %s: %s", url, exc)

    def _warn_and_clear_unsupported_rapidfire(self, action_type: ActionType) -> None:
        if not self._rapidfire_enabled or action_type_supports_rapidfire(action_type):
            return
        log.warning(
            "Ignoring rapidfire for unsupported %s action in superkey action dialog",
            action_type.value,
        )
        if self.rapidfire_check and self.rapidfire_check.get_active():
            self.rapidfire_check.set_active(False)
        else:
            self._rapidfire_enabled = False
            self._update_options_visibility()

    def _on_rapidfire_toggled(self, check):
        self._rapidfire_enabled = check.get_active()
        self._update_options_visibility()

    def _build_exec_tab(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_valign(Gtk.Align.CENTER)

        label = Gtk.Label(label="Execute a shell command:")
        label.set_halign(Gtk.Align.START)
        box.append(label)

        self.cmd_entry = Gtk.Entry()
        self.cmd_entry.set_placeholder_text("e.g., notify-send 'Hello'")
        self.cmd_entry.set_hexpand(True)
        if self._current_action and self._current_action.action_type == ActionType.EXEC:
            self.cmd_entry.set_text(self._current_action.cmd or "")
        box.append(self.cmd_entry)

        exec_btn = Gtk.Button(label="Execute Command")
        exec_btn.add_css_class("suggested-action")
        exec_btn.connect("clicked", self._on_exec_clicked)
        box.append(exec_btn)

        return box

    def _build_macro_tab(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        outer.set_margin_start(12)
        outer.set_margin_end(12)

        label = Gtk.Label(label="Trigger a saved macro")
        label.add_css_class("dim-label")
        label.set_halign(Gtk.Align.START)
        outer.append(label)

        self._superkey_macro_listbox = Gtk.ListBox()
        self._superkey_macro_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._superkey_macro_listbox.connect("row-selected", self._on_superkey_macro_selected)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_child(self._superkey_macro_listbox)
        outer.append(scrolled)

        self._superkey_macro_map_btn = Gtk.Button(label="Map Macro")
        self._superkey_macro_map_btn.add_css_class("suggested-action")
        self._superkey_macro_map_btn.set_sensitive(False)
        self._superkey_macro_map_btn.connect("clicked", self._on_superkey_macro_map_clicked)
        outer.append(self._superkey_macro_map_btn)

        return outer

    def _load_superkey_macro_list(self) -> bool:
        session_request_async({"command": "list_macros"}, self._on_superkey_macro_list_loaded)
        return False

    def _on_superkey_macro_list_loaded(self, result: dict | None) -> bool:
        self._superkey_macro_list = (result or {}).get("macros", [])
        self._populate_superkey_macro_listbox()
        return False

    def _populate_superkey_macro_listbox(self) -> None:
        while self._superkey_macro_listbox.get_first_child():
            self._superkey_macro_listbox.remove(self._superkey_macro_listbox.get_first_child())

        if not self._superkey_macro_list:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            lbl = Gtk.Label(label="No macros saved yet")
            lbl.add_css_class("dim-label")
            lbl.set_margin_top(12)
            lbl.set_margin_bottom(12)
            row.set_child(lbl)
            self._superkey_macro_listbox.append(row)
            self._superkey_macro_map_btn.set_sensitive(False)
            return

        sel_row = None
        for _i, macro in enumerate(self._superkey_macro_list):
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.set_margin_top(8)
            box.set_margin_bottom(8)
            box.set_margin_start(8)
            box.set_margin_end(8)

            name = macro.get("name", "")
            lbl = Gtk.Label(label=name)
            lbl.set_halign(Gtk.Align.START)
            lbl.set_hexpand(True)
            box.append(lbl)

            count = int(macro.get("event_count", 0))
            meta = Gtk.Label(label=f"{count} events")
            meta.add_css_class("caption")
            meta.add_css_class("dim-label")
            box.append(meta)

            row.set_child(box)
            row._macro_name = name
            self._superkey_macro_listbox.append(row)

            if self._superkey_selected_macro and name == self._superkey_selected_macro:
                sel_row = row

        if sel_row is not None:
            self._superkey_macro_listbox.select_row(sel_row)

    def _on_superkey_macro_selected(self, list_box, row) -> None:
        if row and hasattr(row, "_macro_name"):
            self._superkey_selected_macro = row._macro_name
        self._superkey_macro_map_btn.set_sensitive(bool(self._superkey_selected_macro))

    def _on_superkey_macro_map_clicked(self, btn) -> None:
        if not self._superkey_selected_macro:
            return
        self._warn_and_clear_unsupported_rapidfire(ActionType.MACRO)
        action = SuperkeyAction(
            action_type=ActionType.MACRO,
            macro_name=self._superkey_selected_macro,
            rapidfire_enabled=self._rapidfire_enabled if self.rapidfire_check else False,
            rapidfire_hold_ms=int(self.hold_spin.get_value()) if self.rapidfire_check else 20,
            rapidfire_wait_ms=int(self.wait_spin.get_value()) if self.rapidfire_check else 20,
        )
        self.emit("action-selected", action)
        self.close()

    def _on_exec_clicked(self, btn):
        cmd = self.cmd_entry.get_text().strip()
        if cmd:
            self._warn_and_clear_unsupported_rapidfire(ActionType.EXEC)
            action = SuperkeyAction(
                action_type=ActionType.EXEC,
                cmd=cmd,
            )
            self.emit("action-selected", action)
            self.close()

    def _on_clear_clicked(self, btn):
        self.emit("action-selected", None)
        self.close()

    def _on_keyboard_clicked(self, btn, evdev_name: str):
        action = SuperkeyAction(
            action_type=ActionType.KEYBOARD,
            target=evdev_name,
            rapidfire_enabled=self._rapidfire_enabled if self.rapidfire_check else False,
            rapidfire_hold_ms=int(self.hold_spin.get_value()) if self.rapidfire_check else 20,
            rapidfire_wait_ms=int(self.wait_spin.get_value()) if self.rapidfire_check else 20,
        )
        self.emit("action-selected", action)
        self.close()

    def _on_mouse_clicked(self, btn, evdev_name: str):
        action = SuperkeyAction(
            action_type=ActionType.MOUSE,
            target=evdev_name,
            rapidfire_enabled=self._rapidfire_enabled if self.rapidfire_check else False,
            rapidfire_hold_ms=int(self.hold_spin.get_value()) if self.rapidfire_check else 20,
            rapidfire_wait_ms=int(self.wait_spin.get_value()) if self.rapidfire_check else 20,
        )
        self.emit("action-selected", action)
        self.close()

    def _on_gamepad_clicked(self, btn, evdev_name: str):
        action = SuperkeyAction(
            action_type=ActionType.GAMEPAD,
            target=evdev_name,
            output_id=self._selected_gamepad_output_id,
            rapidfire_enabled=self._rapidfire_enabled if self.rapidfire_check else False,
            rapidfire_hold_ms=int(self.hold_spin.get_value()) if self.rapidfire_check else 20,
            rapidfire_wait_ms=int(self.wait_spin.get_value()) if self.rapidfire_check else 20,
        )
        self.emit("action-selected", action)
        self.close()

    def _set_initial_tab(self):
        if not self._current_action:
            return
        tab_map = {
            ActionType.KEYBOARD: (
                "media" if self._current_action.target in MEDIA_KEY_TARGETS else "keyboard"
            ),
            ActionType.MOUSE: "mouse",
            ActionType.GAMEPAD: "gamepad",
            ActionType.MACRO: "macro",
            ActionType.EXEC: "exec",
        }
        name = tab_map.get(self._current_action.action_type)
        if name:
            self.stack.set_visible_child_name(name)

    def _create_key_button(
        self, label: str, evdev: str, width: float = 1, large: bool = False, protected: bool = False
    ) -> Gtk.Button:
        btn = Gtk.Button(label=label)
        btn.add_css_class("key-button")

        if large:
            btn.set_size_request(200, 50)
        else:
            base_width = 36
            btn.set_size_request(int(base_width * width), 34)

        btn._evdev_name = evdev
        return btn

    def _build_keyboard_tab(self) -> Gtk.Widget:
        return build_shared_keyboard_tab(
            self,
            keyboard_layout=KEYBOARD_LAYOUT,
            key_to_evdev=KEY_TO_EVDEV,
            key_widths=KEY_WIDTHS,
        )

    def _build_navigation_tab(self) -> Gtk.Widget:
        return build_shared_navigation_tab(self, f_extra=F_EXTRA)

    def _build_media_tab(self) -> Gtk.Widget:
        return build_shared_media_tab(self, media_groups=MEDIA_KEY_GROUPS)

    def _build_mouse_tab(self) -> Gtk.Widget:
        return build_shared_mouse_tab(self)

    def _build_gamepad_tab(self) -> Gtk.Widget:
        choices = self._gamepad_output_choices()
        concrete_count = sum(1 for output_id, _label in choices if output_id is not None)
        if concrete_count <= 1 and not self._selected_gamepad_output_id:
            return build_shared_gamepad_tab(self)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_margin_top(8)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_start(12)
        row.set_margin_end(12)
        label = Gtk.Label(label="Output")
        label.set_xalign(0)
        label.set_hexpand(True)
        self._gamepad_output_ids = [output_id for output_id, _label in choices]
        dropdown = Gtk.DropDown.new_from_strings([label for _output_id, label in choices])
        selected = 0
        for index, output_id in enumerate(self._gamepad_output_ids):
            if output_id == self._selected_gamepad_output_id:
                selected = index
                break
        dropdown.set_selected(selected)
        dropdown.connect("notify::selected", self._on_gamepad_output_selected)
        row.append(label)
        row.append(dropdown)
        outer.append(row)
        outer.append(build_shared_gamepad_tab(self))
        return outer

    def _gamepad_output_choices(self) -> list[tuple[str | None, str]]:
        choices: list[tuple[str | None, str]] = [(None, "Default")]
        try:
            count = load_virtual_gamepad_count()
        except Exception:
            count = 1
        for index in range(1, count + 1):
            output_id = virtual_gamepad_output_id(index)
            label = "Virtual Gamepad 1" if index == 1 else f"Virtual Gamepad {index}"
            choices.append((output_id, label))
        try:
            for config in HardwareManager().list_hardware():
                if KeySelectorDialog._is_hardware_gamepad(self, config):
                    choices.append((config.hardware_id, config.hardware_id))
        except Exception:
            pass
        if self._selected_gamepad_output_id and all(
            output_id != self._selected_gamepad_output_id for output_id, _label in choices
        ):
            choices.append(
                (
                    self._selected_gamepad_output_id,
                    f"{self._selected_gamepad_output_id} (unknown)",
                )
            )
        return choices

    def _on_gamepad_output_selected(self, dropdown: Gtk.DropDown, _param) -> None:
        selected = int(dropdown.get_selected())
        if 0 <= selected < len(self._gamepad_output_ids):
            self._selected_gamepad_output_id = self._gamepad_output_ids[selected]

    def _on_f_key_selected(self, btn):
        idx = self.f_dropdown.get_selected()
        f_key = F_EXTRA[idx]
        evdev_name = KEY_TO_EVDEV.get(f_key)
        if evdev_name:
            self._on_keyboard_clicked(btn, evdev_name)

    def _on_f_dropdown_changed(self, dropdown, pspec, btn: Gtk.Button):
        idx = dropdown.get_selected()
        f_key = F_EXTRA[idx]
        btn.set_label(f"Map {f_key}")
