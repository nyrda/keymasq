from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk, Pango  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.hardware import HardwareConfig
from keymasq.gui.icons import device_icon_names, image_from_icon_names
from keymasq.gui.session_client import (
    register_session_event_callback,
    session_request_async,
    unregister_session_event_callback,
)
from keymasq.gui.widgets.device_control_layout import resolve_device_layout_kind
from keymasq.gui.widgets.device_inspector.analog import AnalogMixin, AnalogViewer
from keymasq.gui.widgets.device_inspector.events import EventsMixin
from keymasq.gui.widgets.device_inspector.lifecycle import LifecycleMixin
from keymasq.gui.widgets.device_inspector.mapping import MappingMixin
from keymasq.gui.widgets.device_inspector.model import (
    EVENT_FILTERS,
    EventHistory,
    Payload,
)
from keymasq.gui.widgets.device_inspector.session import InspectorSession
from keymasq.gui.widgets.device_inspector.suppression import SuppressionMixin

KEYBOARD_WINDOW_WIDTH = 1180
KEYBOARD_WINDOW_HEIGHT = 800
MOUSE_WINDOW_WIDTH = 820
MOUSE_WINDOW_HEIGHT = 750
GAMEPAD_WINDOW_WIDTH = 1120
GAMEPAD_WINDOW_HEIGHT = 800
RAW_EVENTS_PANEL_WIDTH = 340
AXES_PANEL_WIDTH = 270


class DeviceInspectorWindow(
    LifecycleMixin,
    EventsMixin,
    AnalogMixin,
    MappingMixin,
    SuppressionMixin,
    Adw.Window,
):
    def __init__(self, parent: Gtk.Window, device: HardwareConfig):
        super().__init__()
        self._parent = parent
        self.device = device
        self._hardware_id = device.hardware_id
        self._syncing_suppression = False
        self._snapshot: Payload = {}
        self._device_kind = resolve_device_layout_kind(device)
        self._control_widgets: dict[str, Gtk.Widget] = {}
        self._event_history = EventHistory()
        self._event_order = 0
        self._event_rows: list[Gtk.ListBoxRow] = []
        self._event_filter_buttons: dict[str, Gtk.ToggleButton] = {}
        self._event_render_source_id = 0
        self._analog_viewers: dict[str, AnalogViewer] = {}
        self._flash_timeout_ids: dict[str, int] = {}
        self._session = InspectorSession(
            hardware_id=self._hardware_id,
            request=session_request_async,
            register=register_session_event_callback,
            unregister=unregister_session_event_callback,
        )

        self.set_title(f"Inspect {device.name}")
        window_width, window_height = _inspector_default_size(self._device_kind)
        self.set_default_size(window_width, window_height)
        self.set_transient_for(parent)
        self.set_modal(False)
        self._build_ui()

        self.connect("close-request", self._on_close_request)
        self.connect("destroy", self._on_destroy)
        self._session.start(
            {
                "device_inspector_event": self._on_inspector_event,
                "device_inspector_status": self._on_inspector_status,
                "profiles_changed": self._on_profiles_changed,
                "runtime_reset": self._on_runtime_reset,
                "keymasqd_status": self._on_keymasqd_status,
            },
            self._on_start_response,
        )

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()

        self._header_device_icon = image_from_icon_names(
            *device_icon_names(device_kind=resolve_device_layout_kind(self.device)),
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
            live_box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
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

    def _clear_box(self, box: Gtk.Box) -> None:
        child = box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            box.remove(child)
            child = next_child


def _inspector_default_size(device_kind: str) -> tuple[int, int]:
    if device_kind == "keyboard":
        return KEYBOARD_WINDOW_WIDTH, KEYBOARD_WINDOW_HEIGHT
    if device_kind == "gamepad":
        return GAMEPAD_WINDOW_WIDTH, GAMEPAD_WINDOW_HEIGHT
    return MOUSE_WINDOW_WIDTH, MOUSE_WINDOW_HEIGHT
