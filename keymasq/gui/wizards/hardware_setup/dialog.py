from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, cast

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GObject, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.devices import find_all_interfaces, resolve_stable_path
from keymasq.gui.session_client import GuiTaskResult, run_gui_task
from keymasq.gui.widgets.fuzzy_search import fuzzy_query_matches, install_listbox_fuzzy_filter
from keymasq.gui.wizards.hardware_setup.flow import DiscoveryMixin
from keymasq.gui.wizards.hardware_setup.persistence import PersistenceMixin
from keymasq.gui.wizards.hardware_setup.selection import SelectionMixin
from keymasq.gui.wizards.hardware_setup.state import (
    DiscoverySelection,
    TemplateSelection,
    WizardNavigation,
)
from keymasq.gui.wizards.hardware_setup.types import EvdevDeviceSelection
from keymasq.session.hardware import HardwareManager


class HardwareSetupDialog(
    DiscoveryMixin,
    SelectionMixin,
    PersistenceMixin,
    Adw.Dialog,
):
    __gsignals__ = {
        "device-created": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "evdev-devices-selected": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(
        self,
        parent: Gtk.Window,
        hardware_manager: HardwareManager,
        *,
        raw_evdev_only: bool = False,
        select_evdev_only: bool = False,
    ) -> None:
        super().__init__()
        self.set_title("Add Event Device" if select_evdev_only else "Add New Device")
        self.set_content_width(500)
        self.set_content_height(520)
        if hasattr(self, "set_modal"):
            self.set_modal(True)

        self.hardware_manager = hardware_manager
        self._raw_evdev_only = raw_evdev_only
        self._select_evdev_only = select_evdev_only
        self._navigation = WizardNavigation(select_evdev_only=select_evdev_only)
        self._discovery_state = DiscoverySelection(show_raw=raw_evdev_only)
        self._template_state = TemplateSelection()

        self._setup_escape_close()
        self._setup_ui()
        self._detect_devices()

    def _setup_escape_close(self) -> None:
        controller = Gtk.EventControllerKey.new()
        controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(controller)

    def _on_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if keyval in (Gdk.KEY_f, Gdk.KEY_F) and state & Gdk.ModifierType.CONTROL_MASK:
            self._show_device_search()
            return True
        if keyval == Gdk.KEY_Escape and getattr(self, "device_search_entry", None):
            if self.device_search_entry.get_visible():
                self._hide_device_search()
                return True
        if keyval != Gdk.KEY_Escape:
            return False
        self.close()
        return True

    def _setup_ui(self) -> None:
        self.stack = Adw.ViewStack()
        self._setup_page_select()
        self._setup_page_describe()

        header = Adw.HeaderBar()
        self.back_btn = Gtk.Button(label="Back")
        self.back_btn.connect("clicked", self._on_back)
        self.back_btn.set_visible(False)
        header.pack_start(self.back_btn)

        self.cancel_btn = Gtk.Button(label="Cancel")
        self.cancel_btn.connect("clicked", self._on_cancel_clicked)
        header.pack_start(self.cancel_btn)

        self.next_btn = Gtk.Button(label="Add" if self._select_evdev_only else "Next")
        self.next_btn.connect("clicked", self._on_next)
        self.next_btn.add_css_class("suggested-action")
        self.next_btn.set_sensitive(False)
        header.pack_end(self.next_btn)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.stack)
        self.set_child(toolbar)

    def _setup_page_select(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)

        title = Gtk.Label(
            label="Select Event Device" if self._select_evdev_only else "Select Your Device"
        )
        title.add_css_class("title-1")
        box.append(title)
        subtitle = Gtk.Label(
            label=(
                "Choose the raw event device to attach"
                if self._select_evdev_only
                else "Choose the device you want to configure"
            )
        )
        subtitle.add_css_class("dim-label")
        box.append(subtitle)

        device_tools = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.raw_evdev_check = Gtk.CheckButton(label="Show raw evdev devices")
        self.raw_evdev_check.set_active(self._discovery_state.show_raw)
        self.raw_evdev_check.set_tooltip_text(
            "Show each event node separately, including unknown device types."
        )
        if self._raw_evdev_only:
            self.raw_evdev_check.set_sensitive(False)
        self.raw_evdev_check.connect("toggled", self._on_raw_evdev_toggled)
        device_tools.append(self.raw_evdev_check)

        search_spacer = Gtk.Box()
        search_spacer.set_hexpand(True)
        device_tools.append(search_spacer)
        self.device_search_button = Gtk.Button()
        self.device_search_button.set_icon_name("system-search-symbolic")
        self.device_search_button.set_tooltip_text("Search devices")
        self.device_search_button.connect("clicked", self._on_device_search_clicked)
        device_tools.append(self.device_search_button)
        box.append(device_tools)

        self.device_search_entry = Gtk.SearchEntry()
        self.device_search_entry.set_placeholder_text("Search devices")
        self.device_search_entry.set_tooltip_text("Filter devices by name, type, ID, or evdev path")
        self.device_search_entry.set_visible(False)
        self.device_search_entry.connect("stop-search", self._on_device_search_stop)
        box.append(self.device_search_entry)

        self.device_list = Gtk.ListBox()
        self.device_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.device_list.connect("row-selected", self._on_device_selected)
        install_listbox_fuzzy_filter(
            self.device_list,
            self.device_search_entry,
            after_filter_changed=self._after_device_search_filter_changed,
        )
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_child(self.device_list)
        box.append(scrolled)

        refresh_btn = Gtk.Button(label="Refresh")
        refresh_btn.connect("clicked", self._on_refresh_clicked)
        box.append(refresh_btn)
        self.stack.add_titled(box, "select", "Select Device")

    def _show_device_search(self) -> None:
        self.device_search_entry.set_visible(True)
        self.device_search_entry.grab_focus()
        self.device_search_entry.select_region(0, -1)

    def _hide_device_search(self) -> None:
        self.device_search_entry.set_text("")
        self.device_search_entry.set_visible(False)

    def _on_device_search_clicked(self, _button: Gtk.Button) -> None:
        self._show_device_search()

    def _on_device_search_stop(self, _entry: Gtk.SearchEntry) -> None:
        self._hide_device_search()

    def _after_device_search_filter_changed(self) -> None:
        selected_row = self.device_list.get_selected_row()
        if selected_row is None:
            self._clear_device_selection()
            return
        if fuzzy_query_matches(
            self.device_search_entry.get_text(),
            getattr(selected_row, "_search_text", ""),
        ):
            return
        self.device_list.unselect_row(selected_row)
        self._clear_device_selection()

    def _setup_page_describe(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)

        self.describe_title = Gtk.Label()
        self.describe_title.add_css_class("title-1")
        box.append(self.describe_title)
        self.describe_subtitle = Gtk.Label(label="")
        self.describe_subtitle.add_css_class("dim-label")
        box.append(self.describe_subtitle)

        self.mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mode_label = Gtk.Label(label="Configure as:")
        mode_label.set_halign(Gtk.Align.START)
        self.mode_row.append(mode_label)
        self.mode_combo_model = Gtk.StringList()
        self.mode_combo = Gtk.DropDown(model=self.mode_combo_model)
        self.mode_combo.connect("notify::selected", self._on_mode_changed)
        self.mode_row.append(self.mode_combo)
        box.append(self.mode_row)

        def add_mode_info_label(label: str) -> Gtk.Label:
            info_label = Gtk.Label(label=label)
            info_label.add_css_class("dim-label")
            info_label.set_wrap(True)
            info_label.set_halign(Gtk.Align.START)
            box.append(info_label)
            return info_label

        self.keyboard_mode_info = add_mode_info_label(
            "Keyboard template creates a full standard keyboard hardware profile."
        )
        self.mouse_mode_info = add_mode_info_label(
            "Mouse template creates standard mouse buttons and scroll wheel directions."
        )
        self.mouse_keyboard_mode_info = add_mode_info_label(
            "Mouse + Keyboard template creates a full standard keyboard profile plus a "
            "standard mouse with scroll wheel directions."
        )
        self.gamepad_mode_info = add_mode_info_label(
            "Gamepad template includes detected digital buttons and standard stick "
            "inputs. Use Learn Analog from the device tab to add triggers or other "
            "analog axes."
        )
        self.custom_mode_info = add_mode_info_label(
            "Custom profile saves the selected raw evdev interface without preset "
            "buttons. Add controls later with Learn Buttons."
        )
        self.stack.add_titled(box, "describe", "Describe Device")

    def _on_cancel_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _on_refresh_clicked(self, _button: Gtk.Button) -> None:
        self._detect_devices()

    def _on_next(self, _button: Gtk.Button) -> None:
        visible_page = self.stack.get_visible_child_name()
        if visible_page not in {"select", "describe"}:
            return
        self._navigation.page = cast(Literal["select", "describe"], visible_page)
        action = self._navigation.next_action(
            has_selection=self._discovery_state.selected_device is not None,
            discovery_inflight=self._discovery_state.discovering,
            configure_mode=self._template_state.current,
        )
        if action == "none":
            return
        if action == "emit_evdev":
            self._emit_selected_evdev_devices()
            return
        if action == "show_describe":
            selected_device = self._discovery_state.selected_device
            if selected_device is None:
                return
            self._template_state.current = self._preferred_configure_mode()
            self.mode_combo.set_selected(
                self._template_state.values.index(self._template_state.current)
            )
            self.describe_title.set_label(f"Configure {selected_device.get('name', 'Device')}")
            self._update_describe_mode_ui()
            self.stack.set_visible_child_name("describe")
            self.back_btn.set_visible(True)
            self.cancel_btn.set_visible(False)
            self.next_btn.set_label("Save")
            return
        {
            "save_keyboard": self._save_keyboard_config,
            "save_gamepad": self._save_gamepad_config,
            "save_mouse_keyboard": self._save_mouse_keyboard_config,
            "save_custom": self._save_custom_config,
            "save_mouse": self._save_mouse_config,
        }[action]()

    def _emit_selected_evdev_devices(self) -> None:
        interfaces = list(self._discovery_state.discovered_interfaces.values())
        evdev_devices = self._build_evdev_devices(interfaces)
        if not evdev_devices:
            return
        self.emit(
            "evdev-devices-selected",
            EvdevDeviceSelection(
                evdev_devices,
                self._build_motion_sensors(interfaces),
            ),
        )
        self.close()

    def _on_back(self, _button: Gtk.Button) -> None:
        visible_page = self.stack.get_visible_child_name()
        if visible_page not in {"select", "describe"}:
            return
        self._navigation.page = cast(Literal["select", "describe"], visible_page)
        if not self._navigation.back():
            return
        self.stack.set_visible_child_name("select")
        self.back_btn.set_visible(False)
        self.cancel_btn.set_visible(True)
        self.next_btn.set_sensitive(True)

    def _run_gui_task[T](
        self,
        worker: Callable[[], T],
        callback: Callable[[GuiTaskResult[T]], bool | None],
        *,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        run_gui_task(worker, callback, on_done=on_done)

    def _find_all_interfaces(
        self,
        vendor_id: str,
        product_id: str,
    ) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], find_all_interfaces(vendor_id, product_id))

    def _resolve_stable_path(self, path: str) -> str:
        return resolve_stable_path(path)

    def do_close_request(self) -> bool:
        return False
