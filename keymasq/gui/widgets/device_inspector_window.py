from dataclasses import dataclass, field
from typing import Any, cast

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.models import ActionType, HardwareConfig, MappingAction
from keymasq.gui.icons import device_icon_names, image_from_icon_names
from keymasq.gui.session_client import (
    JsonDict,
    register_session_event_callback,
    session_request_async,
    unregister_session_event_callback,
)
from keymasq.gui.widgets.action_labels import describe_mapping_action_compact
from keymasq.gui.widgets.device_control_layout import (
    device_layout_kind,
    group_pointer_controls,
    label_sort_key,
)

EVENT_ROW_LIMIT = 100
EVENT_RENDER_THROTTLE_MS = 33
DEFAULT_STICK_MIN = -32768
DEFAULT_STICK_MAX = 32767
DEFAULT_AXIS_MIN = 0
DEFAULT_AXIS_MAX = 255
KEYBOARD_WINDOW_WIDTH = 1180
KEYBOARD_WINDOW_HEIGHT = 800
MOUSE_WINDOW_WIDTH = 820
MOUSE_WINDOW_HEIGHT = 750
GAMEPAD_WINDOW_WIDTH = 1120
GAMEPAD_WINDOW_HEIGHT = 800
RAW_EVENTS_PANEL_WIDTH = 340
AXES_PANEL_WIDTH = 270
MAPPING_CARD_WIDTH = 122
MAPPING_NAME_CHARS = 14
MAPPING_ACTION_CHARS = 15
MAPPING_GRID_GAP = 6
EVENT_FILTERS: tuple[tuple[str, str, bool], ...] = (
    ("button", "Keys", True),
    ("axis", "Axes", False),
    ("mousemove", "Move", False),
    ("syn", "Syn", False),
    ("other", "Other", False),
)

KEYBOARD_SECTIONS: tuple[tuple[str, tuple[tuple[str, ...], ...], bool, int], ...] = (
    (
        "Number Keys",
        (
            ("key_1", "key_2", "key_3", "key_4", "key_5", "key_minus"),
            ("key_6", "key_7", "key_8", "key_9", "key_0", "key_equal"),
        ),
        True,
        6,
    ),
    (
        "Keyboard (Left)",
        (
            ("key_esc", "key_grave"),
            ("key_tab", "key_q", "key_w", "key_e", "key_r", "key_t"),
            ("key_capslock", "key_a", "key_s", "key_d", "key_f", "key_g"),
            ("key_leftshift", "key_z", "key_x", "key_c", "key_v", "key_b"),
            ("key_leftctrl", "key_leftmeta", "key_leftalt", "key_space"),
        ),
        True,
        6,
    ),
    (
        "Keyboard (Right)",
        (
            ("key_backspace", "key_y", "key_u", "key_i", "key_o", "key_p"),
            ("key_enter", "key_h", "key_j", "key_k", "key_l"),
            ("key_n", "key_m", "key_rightshift"),
            ("key_rightmeta", "key_rightalt", "key_rightctrl"),
        ),
        False,
        6,
    ),
    (
        "Symbols",
        (
            (
                "key_leftbrace",
                "key_rightbrace",
                "key_backslash",
                "key_semicolon",
                "key_apostrophe",
            ),
            ("key_comma", "key_dot", "key_slash"),
        ),
        False,
        5,
    ),
    (
        "F Row",
        (
            ("key_f1", "key_f2", "key_f3", "key_f4", "key_f5", "key_f6"),
            ("key_f7", "key_f8", "key_f9", "key_f10", "key_f11", "key_f12"),
        ),
        False,
        6,
    ),
    (
        "Navigation",
        (
            ("key_up", "key_down"),
            ("key_left", "key_right"),
            ("key_sysrq", "key_scrolllock", "key_pause"),
            ("key_insert", "key_home", "key_pageup"),
            ("key_delete", "key_end", "key_pagedown"),
        ),
        False,
        4,
    ),
    (
        "Special",
        (
            ("key_numlock", "key_kpslash", "key_kpasterisk", "key_kpminus"),
            ("key_kp7", "key_kp8", "key_kp9", "key_kpplus"),
            ("key_kp4", "key_kp5", "key_kp6"),
            ("key_kp1", "key_kp2", "key_kp3", "key_kpenter"),
            ("key_kp0", "key_kpdot"),
        ),
        False,
        4,
    ),
)


@dataclass
class AnalogViewer:
    analog_id: str
    analog_type: str
    axes: dict[str, JsonDict]
    value_labels: dict[str, Gtk.Label] = field(default_factory=dict)
    drawing_area: Gtk.DrawingArea | None = None
    level_bar: Gtk.LevelBar | None = None
    normalized: dict[str, float] = field(default_factory=dict)


def _text(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(cast(int | float | str | bytes, value))
    except (TypeError, ValueError):
        return None


def _bool_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _mapping_action_from_payload(action: object) -> MappingAction | None:
    if not isinstance(action, dict):
        return None
    try:
        action_type = ActionType(_text(action.get("action"), "passthrough"))
    except ValueError:
        return None

    analog_control_names: list[str] = []
    raw_analog_control_names = action.get("analog_control_names", [])
    if isinstance(raw_analog_control_names, list):
        analog_control_names = [str(name) for name in raw_analog_control_names if str(name).strip()]
    keys: list[str] | None = None
    raw_keys = action.get("keys", [])
    if isinstance(raw_keys, list):
        keys = [str(key) for key in raw_keys if str(key).strip()]
    move_x = _int_or_none(action.get("x"))
    move_y = _int_or_none(action.get("y"))
    axis_value = _int_or_none(action.get("value"))
    rapidfire_hold_ms = _int_or_none(action.get("rapidfire_hold_ms"))
    rapidfire_wait_ms = _int_or_none(action.get("rapidfire_wait_ms"))
    tap_hold_ms = _int_or_none(action.get("tap_hold_ms"))

    return MappingAction(
        action_type=action_type,
        target=_text(action.get("target")) or None,
        output_id=_text(action.get("output_id")) or None,
        keys=keys,
        cmd=_text(action.get("cmd")) or None,
        superkey_name=_text(action.get("superkey_name")) or None,
        analog_control_names=analog_control_names,
        macro_name=_text(action.get("macro_name"), _text(action.get("target"))) or None,
        profile_name=_text(action.get("profile_name"), _text(action.get("target"))) or None,
        compositor_id=_text(action.get("compositor")) or None,
        compositor_dispatcher=_text(action.get("dispatcher")) or None,
        compositor_args=_text(action.get("args")) or None,
        move_x=move_x if move_x is not None else 0,
        move_y=move_y if move_y is not None else 0,
        axis_value=axis_value if axis_value is not None else 0,
        rapidfire_enabled=_bool_value(action.get("rapidfire_enabled")),
        rapidfire_hold_ms=rapidfire_hold_ms if rapidfire_hold_ms is not None else 20,
        rapidfire_wait_ms=rapidfire_wait_ms if rapidfire_wait_ms is not None else 20,
        tap_enabled=_bool_value(action.get("tap_enabled")),
        tap_hold_ms=tap_hold_ms if tap_hold_ms is not None else 10,
    )


class DeviceInspectorWindow(Adw.Window):
    def __init__(self, parent: Gtk.Window, device: HardwareConfig):
        super().__init__()
        self._parent = parent
        self.device = device
        self._hardware_id = device.hardware_id
        self._closing = False
        self._finalized = False
        self._stop_sent = False
        self._syncing_suppression = False
        self._snapshot: JsonDict = {}
        self._device_kind = device_layout_kind(device)
        self._control_widgets: dict[str, Gtk.Widget] = {}
        self._event_history_by_category: dict[str, list[JsonDict]] = {
            filter_id: [] for filter_id, _label, _active in EVENT_FILTERS
        }
        self._event_order = 0
        self._event_rows: list[Gtk.ListBoxRow] = []
        self._event_filter_buttons: dict[str, Gtk.ToggleButton] = {}
        self._event_render_source_id = 0
        self._analog_viewers: dict[str, AnalogViewer] = {}
        self._flash_timeout_ids: dict[str, int] = {}

        self.set_title(f"Inspect {device.name}")
        window_width, window_height = _inspector_default_size(self._device_kind)
        self.set_default_size(window_width, window_height)
        self.set_transient_for(parent)
        self.set_modal(False)

        self._build_ui()

        register_session_event_callback("device_inspector_event", self._on_inspector_event)
        register_session_event_callback("device_inspector_status", self._on_inspector_status)
        register_session_event_callback("profiles_changed", self._on_profiles_changed)
        register_session_event_callback("runtime_reset", self._on_runtime_reset)
        register_session_event_callback("keymasqd_status", self._on_keymasqd_status)
        self.connect("close-request", self._on_close_request)
        self.connect("destroy", self._on_destroy)

        session_request_async(
            {"command": "start_device_inspector", "hardware_id": self._hardware_id},
            self._on_start_response,
            timeout=6.0,
        )

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()

        self._header_device_icon = image_from_icon_names(
            *device_icon_names(device_kind=device_layout_kind(self.device)),
            pixel_size=24,
        )
        self._header_device_icon.set_valign(Gtk.Align.CENTER)
        header.pack_start(self._header_device_icon)

        self._status_label = Gtk.Label(label="Starting inspector")
        self._status_label.add_css_class("inspector-header-title")
        self._status_label.set_halign(Gtk.Align.START)
        self._status_label.set_hexpand(True)
        self._status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._status_label.set_max_width_chars(54)
        header.pack_start(self._status_label)
        self._header_title_spacer = Gtk.Box()
        header.set_title_widget(self._header_title_spacer)

        switch_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        switch_box.set_valign(Gtk.Align.CENTER)
        self._suppression_hint_label = Gtk.Label(label="Esc stops suppression")
        self._suppression_hint_label.add_css_class("caption")
        self._suppression_hint_label.add_css_class("inspector-suppression-hint")
        self._suppression_hint_label.set_visible(False)
        self._suppression_hint_label.set_halign(Gtk.Align.END)
        switch_box.append(self._suppression_hint_label)
        switch_label = Gtk.Label(label="Suppress output")
        switch_label.set_halign(Gtk.Align.END)
        switch_box.append(switch_label)
        self._suppression_switch = Gtk.Switch()
        self._suppression_switch.set_valign(Gtk.Align.CENTER)
        self._suppression_switch.connect("notify::active", self._on_suppression_toggled)
        switch_box.append(self._suppression_switch)
        header.pack_end(switch_box)

        toolbar.add_top_bar(header)
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)

        window_width, _window_height = _inspector_default_size(self._device_kind)
        live_panel_width = (
            AXES_PANEL_WIDTH + RAW_EVENTS_PANEL_WIDTH
            if self._device_kind == "gamepad"
            else RAW_EVENTS_PANEL_WIDTH
        )
        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._paned.set_position(window_width - live_panel_width)
        self._paned.set_wide_handle(True)
        self._paned.set_resize_start_child(True)
        self._paned.set_resize_end_child(False)
        self._paned.set_shrink_start_child(True)
        self._paned.set_shrink_end_child(False)

        mapping_scrolled = Gtk.ScrolledWindow()
        mapping_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._mapping_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._mapping_box.set_margin_top(14)
        self._mapping_box.set_margin_bottom(14)
        self._mapping_box.set_margin_start(14)
        self._mapping_box.set_margin_end(14)
        mapping_scrolled.set_child(self._mapping_box)
        self._paned.set_start_child(mapping_scrolled)

        if self._device_kind == "gamepad":
            live_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            live_box.set_size_request(live_panel_width, -1)
            live_box.set_hexpand(False)
            axes_parent = self._build_live_panel_column(AXES_PANEL_WIDTH)
            events_parent = self._build_live_panel_column(RAW_EVENTS_PANEL_WIDTH)
            live_box.append(axes_parent)
            separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
            live_box.append(separator)
            live_box.append(events_parent)
        else:
            live_box = self._build_live_panel_column(RAW_EVENTS_PANEL_WIDTH)
            axes_parent = live_box
            events_parent = live_box

        self._axes_title = Gtk.Label(label="Configured Axes")
        self._axes_title.add_css_class("button-section-title")
        self._axes_title.set_halign(Gtk.Align.START)
        self._axes_title.set_visible(False)
        axes_parent.append(self._axes_title)

        self._axes_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._axes_box.set_visible(False)
        axes_parent.append(self._axes_box)

        events_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        events_header.set_halign(Gtk.Align.FILL)

        events_title = Gtk.Label(label="Raw Events")
        events_title.add_css_class("button-section-title")
        events_title.set_halign(Gtk.Align.START)
        events_title.set_hexpand(False)
        events_header.append(events_title)

        self._copy_events_button = Gtk.Button()
        self._copy_events_button.set_tooltip_text("Copy visible events")
        self._copy_events_button.add_css_class("flat")
        self._copy_events_button.add_css_class("inspector-copy-button")
        self._copy_events_button.set_child(
            image_from_icon_names("edit-copy-symbolic", "edit-paste-symbolic", pixel_size=14)
        )
        self._copy_events_button.connect("clicked", self._on_copy_events_clicked)
        events_header.append(self._copy_events_button)
        events_parent.append(events_header)

        filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        filter_box.add_css_class("linked")
        for filter_id, label, active in EVENT_FILTERS:
            button = Gtk.ToggleButton(label=label)
            button.add_css_class("inspector-event-filter-button")
            button.set_active(active)
            button.connect("toggled", self._on_event_filter_toggled)
            filter_box.append(button)
            self._event_filter_buttons[filter_id] = button
        events_parent.append(filter_box)

        self._event_list = Gtk.ListBox()
        self._event_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._event_list.add_css_class("boxed-list")

        event_scrolled = Gtk.ScrolledWindow()
        event_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        event_scrolled.set_vexpand(True)
        event_scrolled.set_child(self._event_list)
        events_parent.append(event_scrolled)
        self._paned.set_end_child(live_box)

        toolbar.set_content(self._paned)
        self.set_content(toolbar)

    def _build_live_panel_column(self, width: int) -> Gtk.Box:
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        column.set_size_request(width, -1)
        column.set_hexpand(False)
        column.set_margin_top(14)
        column.set_margin_bottom(14)
        column.set_margin_start(14)
        column.set_margin_end(14)
        return column

    def _on_start_response(self, result: JsonDict | None) -> bool:
        if self._closing:
            return False
        if not result or result.get("status") != "ok":
            self._set_status_title(
                _text((result or {}).get("message"), "Inspector could not start"),
                "stopped",
            )
            self._suppression_switch.set_sensitive(False)
            return False
        self._apply_snapshot(result)
        return False

    def _request_snapshot(self) -> None:
        if self._closing:
            return
        session_request_async(
            {"command": "get_device_inspector_snapshot", "hardware_id": self._hardware_id},
            self._on_snapshot_response,
            timeout=3.0,
        )

    def _on_snapshot_response(self, result: JsonDict | None) -> bool:
        if self._closing:
            return False
        if result and result.get("status") == "ok":
            self._apply_snapshot(result)
        return False

    def _apply_snapshot(self, snapshot: JsonDict) -> None:
        self._snapshot = dict(snapshot)
        self._sync_status(snapshot)
        self._render_mapping(snapshot)
        self._render_axes(snapshot)

    def _sync_status(self, data: JsonDict) -> None:
        active = bool(data.get("active", True))
        suppressed = bool(data.get("suppressed", False))
        if active:
            state = "suppressed" if suppressed else "monitoring"
            text = f"{self.device.name} - {'Output suppressed' if suppressed else 'Monitoring'}"
        else:
            state = "stopped"
            text = f"{self.device.name} - Stopped"
        self._set_status_title(text, state)
        self._syncing_suppression = True
        try:
            if self._suppression_switch.get_active() != suppressed:
                self._suppression_switch.set_active(suppressed)
        finally:
            self._syncing_suppression = False
        self._suppression_switch.set_sensitive(active and not self._closing)
        self._suppression_hint_label.set_visible(active and suppressed)

    def _set_status_title(self, text: str, state: str) -> None:
        self._status_label.set_text(text)
        self._status_label.set_tooltip_text(text)
        for css_class in (
            "inspector-header-monitoring",
            "inspector-header-suppressed",
            "inspector-header-stopped",
        ):
            self._status_label.remove_css_class(css_class)
        self._status_label.add_css_class(
            {
                "suppressed": "inspector-header-suppressed",
                "stopped": "inspector-header-stopped",
            }.get(state, "inspector-header-monitoring")
        )

    def _render_mapping(self, snapshot: JsonDict) -> None:
        self._clear_box(self._mapping_box)
        self._control_widgets.clear()
        buttons = [item for item in _list_of_dicts(snapshot.get("buttons"))]
        analog_inputs = [item for item in _list_of_dicts(snapshot.get("analog_inputs"))]

        title = Gtk.Label(label="Resolved Mapping")
        title.add_css_class("button-section-title")
        title.set_halign(Gtk.Align.START)
        self._mapping_box.append(title)

        profiles = snapshot.get("active_profiles")
        profile_label = Gtk.Label(
            label=", ".join(str(name) for name in profiles)
            if isinstance(profiles, list) and profiles
            else "No active profiles"
        )
        profile_label.add_css_class("caption")
        profile_label.add_css_class("dim-label")
        profile_label.set_halign(Gtk.Align.START)
        self._mapping_box.append(profile_label)

        if self._device_kind == "keyboard":
            self._render_keyboard_buttons(buttons)
        else:
            self._render_pointer_buttons(buttons)

        if analog_inputs:
            analog_label = Gtk.Label(label="Analog Inputs")
            analog_label.add_css_class("button-section-title")
            analog_label.set_halign(Gtk.Align.START)
            analog_label.set_margin_top(8)
            self._mapping_box.append(analog_label)
            grid = Gtk.Grid(column_spacing=MAPPING_GRID_GAP, row_spacing=MAPPING_GRID_GAP)
            grid.set_halign(Gtk.Align.START)
            for index, analog in enumerate(analog_inputs):
                widget = self._create_control_card(analog, is_keyboard=False)
                grid.attach(widget, index % 2, index // 2, 1, 1)
                self._control_widgets[_text(analog.get("id"))] = widget
            self._mapping_box.append(grid)

    def _render_keyboard_buttons(self, buttons: list[JsonDict]) -> None:
        buttons_by_id = {_text(button.get("id")): button for button in buttons}
        used_ids: set[str] = set()
        for title, layout_rows, expanded, max_cols in KEYBOARD_SECTIONS:
            grid = Gtk.Grid(column_spacing=MAPPING_GRID_GAP, row_spacing=MAPPING_GRID_GAP)
            grid.set_halign(Gtk.Align.START)
            for row_index, row_items in enumerate(layout_rows):
                for col_index, button_id in enumerate(row_items):
                    button = buttons_by_id.get(button_id)
                    if button is None:
                        spacer = Gtk.Box()
                        spacer.set_size_request(MAPPING_CARD_WIDTH, -1)
                        grid.attach(spacer, col_index, row_index, 1, 1)
                        continue
                    widget = self._create_control_card(button, is_keyboard=True)
                    grid.attach(widget, col_index, row_index, 1, 1)
                    self._control_widgets[button_id] = widget
                    used_ids.add(button_id)
                for col_index in range(len(row_items), max_cols):
                    spacer = Gtk.Box()
                    spacer.set_size_request(MAPPING_CARD_WIDTH, -1)
                    grid.attach(spacer, col_index, row_index, 1, 1)
            expander = Gtk.Expander(label=title)
            expander.add_css_class("device-section-expander")
            expander.set_expanded(expanded)
            expander.set_child(grid)
            self._mapping_box.append(expander)

        extras = [button for button in buttons if _text(button.get("id")) not in used_ids]
        if extras:
            self._append_flow_section("Extra Keys", extras, is_keyboard=True)

    def _render_pointer_buttons(self, buttons: list[JsonDict]) -> None:
        main_buttons, scroll_buttons, side_buttons, extra_buttons = group_pointer_controls(
            buttons,
            id_for_control=lambda button: _text(button.get("id")),
        )
        for title, group in (
            ("Extra Buttons", extra_buttons),
            ("Main Buttons", main_buttons),
            ("Scroll", scroll_buttons),
            ("Side Buttons", side_buttons),
        ):
            if group:
                self._append_flow_section(title, group, is_keyboard=False)

    def _append_flow_section(
        self,
        title: str,
        controls: list[JsonDict],
        *,
        is_keyboard: bool,
    ) -> None:
        label = Gtk.Label(label=title)
        label.add_css_class("button-section-title")
        label.set_halign(Gtk.Align.START)
        self._mapping_box.append(label)

        grid = Gtk.Grid(column_spacing=MAPPING_GRID_GAP, row_spacing=MAPPING_GRID_GAP)
        grid.set_halign(Gtk.Align.START)
        max_cols = 4 if is_keyboard else 3
        sorted_controls = sorted(controls, key=lambda item: label_sort_key(item.get("label")))
        for index, control in enumerate(sorted_controls):
            widget = self._create_control_card(control, is_keyboard=is_keyboard)
            grid.attach(widget, index % max_cols, index // max_cols, 1, 1)
            self._control_widgets[_text(control.get("id"))] = widget
        self._mapping_box.append(grid)

    def _create_control_card(self, control: JsonDict, *, is_keyboard: bool) -> Gtk.Button:
        action = control.get("action")
        mapped = isinstance(action, dict)
        button = Gtk.Button()
        button.add_css_class("card")
        button.add_css_class("inspector-control-card")
        button.add_css_class("button-card-mapped-active" if mapped else "button-card-passthrough")
        button.set_focusable(False)
        button.set_size_request(MAPPING_CARD_WIDTH, -1)
        button.set_halign(Gtk.Align.START)
        button.set_hexpand(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(7)
        horizontal_padding = 7
        box.set_margin_start(horizontal_padding)
        box.set_margin_end(horizontal_padding)
        box.set_size_request(MAPPING_CARD_WIDTH - (horizontal_padding * 2), -1)

        name_label = Gtk.Label(label=_text(control.get("label"), _text(control.get("id"))))
        name_label.add_css_class("heading")
        name_label.set_xalign(0.0)
        name_label.set_ellipsize(Pango.EllipsizeMode.END)
        name_label.set_width_chars(1)
        name_label.set_max_width_chars(MAPPING_NAME_CHARS)
        box.append(name_label)

        action_text = self._action_label(action, control) or self._passthrough_label(control)
        action_label = Gtk.Label(label=_ellipsize_middle(action_text, MAPPING_ACTION_CHARS))
        action_label.add_css_class("caption")
        action_label.add_css_class("button-card-action-label")
        action_label.set_xalign(0.0)
        action_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        action_label.set_width_chars(1)
        action_label.set_max_width_chars(MAPPING_ACTION_CHARS)
        if not mapped:
            action_label.add_css_class("dim-label")
        box.append(action_label)

        profile_name = _text(control.get("profile_name"))
        tooltip = action_text
        if profile_name:
            tooltip = f"{tooltip}\nProfile: {profile_name}"
        source = _text(control.get("source"))
        if source:
            tooltip = f"{tooltip}\nSource: {source}"
        button.set_tooltip_text(tooltip)
        button.set_child(box)
        return button

    def _render_axes(self, snapshot: JsonDict) -> None:
        self._clear_box(self._axes_box)
        self._analog_viewers.clear()
        analogs = [item for item in _list_of_dicts(snapshot.get("analog_inputs"))]
        self._axes_title.set_visible(bool(analogs))
        self._axes_box.set_visible(bool(analogs))
        if not analogs:
            return

        for analog in analogs:
            viewer = self._create_analog_viewer(analog)
            self._analog_viewers[viewer.analog_id] = viewer

    def _create_analog_viewer(self, analog: JsonDict) -> AnalogViewer:
        analog_id = _text(analog.get("id"))
        analog_type = _text(analog.get("type"), "axis").lower()
        axes = {
            _text(axis.get("role")): axis
            for axis in _list_of_dicts(analog.get("axes"))
            if _text(axis.get("role"))
        }
        viewer = AnalogViewer(
            analog_id=analog_id,
            analog_type=analog_type,
            axes=axes,
            normalized={role: _normalize_axis(axis, 0, analog_type) for role, axis in axes.items()},
        )

        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        section.add_css_class("inspector-axis-section")

        title = Gtk.Label(label=_text(analog.get("label"), analog_id))
        title.add_css_class("heading")
        title.set_halign(Gtk.Align.CENTER)
        section.append(title)

        if analog_type == "stick" and {"x", "y"} <= set(axes):
            area = Gtk.DrawingArea()
            area.set_content_width(150)
            area.set_content_height(150)
            area.set_halign(Gtk.Align.CENTER)
            area.add_css_class("inspector-axis-pad")

            def draw_stick(
                _area: Gtk.DrawingArea,
                cr: Any,
                width: int,
                height: int,
                _data: object,
            ) -> None:
                self._draw_stick(viewer, cr, width, height)

            area.set_draw_func(draw_stick, None)
            viewer.drawing_area = area
            section.append(area)
        else:
            bar = Gtk.LevelBar()
            bar.set_min_value(0.0)
            bar.set_max_value(1.0)
            first_role = sorted(axes)[0] if axes else ""
            bar.set_value(_level_bar_value(analog_type, viewer.normalized.get(first_role, 0.0)))
            bar.add_css_class("inspector-axis-bar")
            bar.set_halign(Gtk.Align.CENTER)
            bar.set_size_request(220, -1)
            viewer.level_bar = bar
            section.append(bar)

        values = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        values.set_halign(Gtk.Align.CENTER)
        for role in sorted(axes):
            label = Gtk.Label(label=_axis_value_label(role, 0, viewer.normalized.get(role, 0.0)))
            label.add_css_class("caption")
            label.add_css_class("dim-label")
            label.add_css_class("inspector-axis-value")
            label.set_xalign(0.5)
            label.set_width_chars(29)
            label.set_max_width_chars(29)
            values.append(label)
            viewer.value_labels[role] = label
        section.append(values)
        self._axes_box.append(section)
        return viewer

    def _draw_stick(self, viewer: AnalogViewer, cr: Any, width: int, height: int) -> None:
        size = min(width, height)
        cx = width / 2.0
        cy = height / 2.0
        radius = max(10.0, (size / 2.0) - 10.0)

        cr.set_line_width(1.0)
        cr.set_source_rgba(0.45, 0.45, 0.45, 0.28)
        cr.arc(cx, cy, radius, 0, 6.28318)
        cr.fill_preserve()
        cr.set_source_rgba(0.45, 0.45, 0.45, 0.55)
        cr.stroke()

        cr.set_source_rgba(0.45, 0.45, 0.45, 0.35)
        cr.move_to(cx - radius, cy)
        cr.line_to(cx + radius, cy)
        cr.move_to(cx, cy - radius)
        cr.line_to(cx, cy + radius)
        cr.stroke()

        x = max(-1.0, min(1.0, viewer.normalized.get("x", 0.0)))
        y = max(-1.0, min(1.0, viewer.normalized.get("y", 0.0)))
        cr.set_source_rgba(0.0, 0.45, 0.85, 0.95)
        cr.arc(cx + x * radius, cy + y * radius, 6.0, 0, 6.28318)
        cr.fill()

    def _on_inspector_event(self, event: JsonDict) -> bool:
        if _text(event.get("hardware_id")) != self._hardware_id:
            return False
        self._store_event(event)

        control_id = _text(event.get("control_id"))
        event_type = _text(event.get("type_name")).lower()
        value = int(event.get("value", 0) or 0)
        if control_id:
            if event_type == "ev_key":
                self._set_control_active(control_id, value != 0)
            else:
                self._flash_control(control_id)

        analog_id = _text(event.get("analog_id"))
        role = _text(event.get("analog_role"))
        if analog_id and role:
            self._update_analog_value(analog_id, role, value)
            self._flash_control(analog_id)
        return False

    def _store_event(self, event: JsonDict) -> None:
        category = _event_filter_category(event)
        if not category:
            return

        stored = dict(event)
        self._event_order += 1
        stored["_inspector_order"] = self._event_order
        history = self._event_history_by_category.setdefault(category, [])
        history.insert(0, stored)
        del history[EVENT_ROW_LIMIT:]

        if not self._event_filter_active(category):
            return
        if category == "button":
            self._render_event_rows()
        else:
            self._queue_event_render()

    def _prepend_event_row(self, event: JsonDict) -> None:
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.add_css_class("inspector-event-row")
        if int(event.get("value", 0) or 0) == 1:
            row.add_css_class("inspector-event-row-pressed")

        grid = Gtk.Grid(column_spacing=10, row_spacing=2)
        grid.set_margin_top(5)
        grid.set_margin_bottom(5)
        grid.set_margin_start(8)
        grid.set_margin_end(8)

        sequence = int(event.get("sequence", 0) or 0)
        event_type = _text(event.get("type_name"), _text(event.get("type")))
        code_name = _text(event.get("code_name"), _text(event.get("code")))
        value = _text(event.get("value"), "0")
        source = _text(event.get("source"))

        sequence_label = Gtk.Label(label=f"#{sequence}" if sequence else "")
        sequence_label.add_css_class("caption")
        sequence_label.add_css_class("dim-label")
        sequence_label.set_xalign(0.0)
        grid.attach(sequence_label, 0, 0, 1, 2)

        code_label = Gtk.Label(label=code_name)
        code_label.add_css_class("heading")
        code_label.set_xalign(0.0)
        code_label.set_hexpand(True)
        code_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        grid.attach(code_label, 1, 0, 1, 1)

        detail_text = f"{event_type} value={value}"
        if source:
            detail_text = f"{detail_text} source={source}"
        details = Gtk.Label(label=detail_text)
        details.add_css_class("caption")
        details.add_css_class("dim-label")
        details.set_xalign(0.0)
        details.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        grid.attach(details, 1, 1, 1, 1)

        row.set_child(grid)
        self._event_list.insert(row, 0)
        self._event_rows.insert(0, row)
        while len(self._event_rows) > EVENT_ROW_LIMIT:
            old = self._event_rows.pop()
            self._event_list.remove(old)

    def _on_event_filter_toggled(self, _button: Gtk.ToggleButton) -> None:
        self._cancel_event_render()
        self._render_event_rows()

    def _on_copy_events_clicked(self, _button: Gtk.Button) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        display.get_clipboard().set(self._visible_event_export_text())

    def _visible_event_export_text(self) -> str:
        return "\n".join(_event_export_line(event) for event in self._visible_event_history())

    def _render_event_rows(self) -> None:
        for row in list(self._event_rows):
            self._event_list.remove(row)
        self._event_rows.clear()
        for event in reversed(self._visible_event_history()):
            self._prepend_event_row(event)

    def _visible_event_history(self) -> list[JsonDict]:
        events: list[JsonDict] = []
        for category, history in self._event_history_by_category.items():
            if self._event_filter_active(category):
                events.extend(history)
        events.sort(
            key=lambda event: _int_or_none(event.get("_inspector_order")) or 0,
            reverse=True,
        )
        return events[:EVENT_ROW_LIMIT]

    def _event_filter_active(self, category: str) -> bool:
        button = self._event_filter_buttons.get(category)
        return bool(button and button.get_active())

    def _queue_event_render(self) -> None:
        if self._event_render_source_id:
            return
        self._event_render_source_id = GLib.timeout_add(
            EVENT_RENDER_THROTTLE_MS,
            self._flush_event_render,
        )

    def _flush_event_render(self) -> bool:
        self._event_render_source_id = 0
        if not self._closing:
            self._render_event_rows()
        return False

    def _cancel_event_render(self) -> None:
        if not self._event_render_source_id:
            return
        GLib.source_remove(self._event_render_source_id)
        self._event_render_source_id = 0

    def _update_analog_value(self, analog_id: str, role: str, value: int) -> None:
        viewer = self._analog_viewers.get(analog_id)
        if viewer is None:
            return
        axis = viewer.axes.get(role)
        if axis is None:
            return
        normalized = _normalize_axis(axis, value, viewer.analog_type)
        viewer.normalized[role] = normalized
        value_label = viewer.value_labels.get(role)
        if value_label is not None:
            value_label.set_text(_axis_value_label(role, value, normalized))
        if viewer.drawing_area is not None:
            viewer.drawing_area.queue_draw()
        if viewer.level_bar is not None:
            viewer.level_bar.set_value(_level_bar_value(viewer.analog_type, normalized))

    def _on_inspector_status(self, event: JsonDict) -> bool:
        if _text(event.get("hardware_id")) != self._hardware_id:
            return False
        self._sync_status(event)
        return False

    def _on_profiles_changed(self, _event: JsonDict) -> bool:
        self._request_snapshot()
        return False

    def _on_runtime_reset(self, _event: JsonDict) -> bool:
        self._request_snapshot()
        return False

    def _on_keymasqd_status(self, event: JsonDict) -> bool:
        if not bool(event.get("connected", False)):
            self._set_status_title(f"{self.device.name} - Daemon disconnected", "stopped")
            self._suppression_switch.set_sensitive(False)
        return False

    def _on_suppression_toggled(self, switch: Gtk.Switch, _param: object) -> None:
        if self._syncing_suppression or self._closing:
            return
        command = (
            "enable_device_inspector_suppression"
            if switch.get_active()
            else "disable_device_inspector_suppression"
        )
        payload: JsonDict = {"command": command, "hardware_id": self._hardware_id}
        if command == "disable_device_inspector_suppression":
            payload["reason"] = "manual"
        switch.set_sensitive(False)
        session_request_async(payload, self._on_suppression_response, timeout=3.0)

    def _on_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if (
            keyval != Gdk.KEY_Escape
            or self._closing
            or not self._suppression_switch.get_active()
        ):
            return False
        self._suppression_switch.set_sensitive(False)
        session_request_async(
            {
                "command": "disable_device_inspector_suppression",
                "hardware_id": self._hardware_id,
                "reason": "key_esc",
            },
            self._on_suppression_response,
            timeout=3.0,
        )
        return True

    def _on_suppression_response(self, result: JsonDict | None) -> bool:
        if self._closing:
            return False
        if result and result.get("status") == "ok":
            self._sync_status(result)
        else:
            self._request_snapshot()
        return False

    def _set_control_active(self, control_id: str, active: bool) -> None:
        widget = self._control_widgets.get(control_id)
        if widget is None:
            return
        if active:
            widget.add_css_class("inspector-control-active")
        else:
            widget.remove_css_class("inspector-control-active")

    def _flash_control(self, control_id: str) -> None:
        widget = self._control_widgets.get(control_id)
        if widget is None:
            return
        widget.add_css_class("inspector-control-active")
        old_source = self._flash_timeout_ids.pop(control_id, 0)
        if old_source:
            GLib.source_remove(old_source)

        def clear() -> bool:
            widget.remove_css_class("inspector-control-active")
            self._flash_timeout_ids.pop(control_id, None)
            return False

        self._flash_timeout_ids[control_id] = GLib.timeout_add(180, clear)

    def _on_close_request(self, *_args: object) -> bool:
        self._finalize()
        return False

    def _on_destroy(self, *_args: object) -> None:
        self._finalize()

    def _finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        self._closing = True
        self._cancel_event_render()
        for source_id in list(self._flash_timeout_ids.values()):
            GLib.source_remove(source_id)
        self._flash_timeout_ids.clear()
        unregister_session_event_callback("device_inspector_event", self._on_inspector_event)
        unregister_session_event_callback("device_inspector_status", self._on_inspector_status)
        unregister_session_event_callback("profiles_changed", self._on_profiles_changed)
        unregister_session_event_callback("runtime_reset", self._on_runtime_reset)
        unregister_session_event_callback("keymasqd_status", self._on_keymasqd_status)
        self._stop_inspector()

    def _stop_inspector(self) -> None:
        if self._stop_sent:
            return
        self._stop_sent = True
        session_request_async(
            {"command": "stop_device_inspector", "hardware_id": self._hardware_id},
            lambda _result: False,
            timeout=1.5,
        )

    def _action_label(self, action: object, control: JsonDict) -> str | None:
        mapping = _mapping_action_from_payload(action)
        if mapping is None:
            return None
        if mapping.action_type == ActionType.PASSTHROUGH:
            return self._passthrough_label(control)
        return describe_mapping_action_compact(mapping, include_state=True)

    def _passthrough_label(self, control: JsonDict) -> str:
        if _text(control.get("kind")) == "analog":
            if _text(control.get("type")) == "axis":
                return "Axis passthrough"
            return "Analog passthrough"
        evdev = _text(control.get("evdev"))
        evdev_value = _int_or_none(control.get("evdev_value"))
        if evdev == "rel_wheel" and evdev_value is not None:
            return "Scroll Up" if evdev_value > 0 else "Scroll Down"
        if evdev == "rel_hwheel" and evdev_value is not None:
            return "Scroll Right" if evdev_value > 0 else "Scroll Left"
        return f"Passthrough: {evdev or _text(control.get('id'), '?')}"

    def _clear_box(self, box: Gtk.Box) -> None:
        child = box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            box.remove(child)
            child = next_child


def _list_of_dicts(value: object) -> list[JsonDict]:
    if not isinstance(value, list):
        return []
    return [cast(JsonDict, item) for item in value if isinstance(item, dict)]


def _ellipsize_middle(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    head = max(1, (max_chars - 3) // 2)
    tail = max(1, max_chars - 3 - head)
    return f"{text[:head]}...{text[-tail:]}"


def _event_filter_category(event: JsonDict) -> str:
    event_type = _text(event.get("type_name"), _text(event.get("type"))).lower()
    code_name = _text(event.get("code_name"), _text(event.get("code"))).lower()
    if event_type in {"ev_key", "1"}:
        return "button"
    if event_type in {"ev_syn", "0"} or (
        event_type in {"ev_msc", "4"} and code_name in {"msc_scan", "4"}
    ):
        return "syn"
    if event_type in {"ev_abs", "3"}:
        return "axis"
    if event_type in {"ev_rel", "2"}:
        if code_name in {"rel_x", "rel_y"}:
            return "mousemove"
        return "axis"
    return "other"


def _event_export_line(event: JsonDict) -> str:
    sequence = int(event.get("sequence", 0) or 0)
    code_name = _text(event.get("code_name"), _text(event.get("code"), "unknown"))
    event_type = _text(event.get("type_name"), _text(event.get("type"), "unknown"))
    value = _text(event.get("value"), "0")
    source = _text(event.get("source"))
    parts = [f"#{sequence}" if sequence else "#-", code_name, event_type, f"value={value}"]
    if source:
        parts.append(f"source={source}")
    return " ".join(parts)


def _axis_min_max(axis: JsonDict, analog_type: str) -> tuple[int, int]:
    minimum = _int_or_none(axis.get("minimum"))
    maximum = _int_or_none(axis.get("maximum"))
    if minimum is None or maximum is None or maximum <= minimum:
        if analog_type == "axis":
            return DEFAULT_AXIS_MIN, DEFAULT_AXIS_MAX
        return DEFAULT_STICK_MIN, DEFAULT_STICK_MAX
    return minimum, maximum


def _normalize_axis(axis: JsonDict, value: int, analog_type: str) -> float:
    minimum, maximum = _axis_min_max(axis, analog_type)
    if analog_type == "axis":
        rest = _int_or_none(axis.get("rest"))
        if rest is None:
            rest = minimum if minimum >= 0 else 0
        positive_span = float(maximum) - float(rest)
        negative_span = float(minimum) - float(rest)
        active_span = positive_span if abs(positive_span) >= abs(negative_span) else negative_span
        if abs(active_span) < 1.0:
            active_span = float(maximum) - float(minimum)
        if abs(active_span) < 1.0:
            return 0.0
        normalized = (float(value) - float(rest)) / active_span
        return max(0.0, min(1.0, normalized))

    center = _int_or_none(axis.get("center"))
    midpoint = float(center) if center is not None else (float(minimum) + float(maximum)) / 2.0
    raw = float(value)
    if raw < midpoint:
        span = max(1.0, midpoint - float(minimum))
        normalized = (raw - midpoint) / span
    else:
        span = max(1.0, float(maximum) - midpoint)
        normalized = (raw - midpoint) / span
    if bool(axis.get("invert", False)):
        normalized = -normalized
    return max(-1.0, min(1.0, normalized))


def _level_bar_value(analog_type: str, normalized: float) -> float:
    if analog_type == "axis":
        return max(0.0, min(1.0, normalized))
    return max(0.0, min(1.0, (normalized + 1.0) / 2.0))


def _axis_value_label(role: str, raw_value: int, normalized: float) -> str:
    return f"{role}: raw {raw_value:6d} | norm {normalized:+.3f}"


def _inspector_default_size(device_kind: str) -> tuple[int, int]:
    if device_kind == "keyboard":
        return KEYBOARD_WINDOW_WIDTH, KEYBOARD_WINDOW_HEIGHT
    if device_kind == "gamepad":
        return GAMEPAD_WINDOW_WIDTH, GAMEPAD_WINDOW_HEIGHT
    return MOUSE_WINDOW_WIDTH, MOUSE_WINDOW_HEIGHT
