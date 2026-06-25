import logging
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any, cast

import evdev
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import (
    Adw,  # pyright: ignore[reportAttributeAccessIssue]
    Gdk,  # pyright: ignore[reportAttributeAccessIssue]
    GObject,  # pyright: ignore[reportAttributeAccessIssue]
    Gtk,  # pyright: ignore[reportAttributeAccessIssue]
)

from keymasq.common.devices import (
    capability_names_from_capabilities,
    find_all_interfaces,
    input_classes_include_gamepad,
    make_keymasq_device_path,
    resolve_stable_path,
)
from keymasq.common.models import (
    AnalogInputDefinition,
    ButtonDefinition,
    DeviceType,
    EvdevDevice,
    HardwareConfig,
)
from keymasq.gui.session_client import (
    GuiTaskResult,
    run_gui_task,
)
from keymasq.gui.widgets.fuzzy_search import fuzzy_query_matches, install_listbox_fuzzy_filter
from keymasq.gui.wizards.hardware_setup import discovery, inventory, rows, templates
from keymasq.gui.wizards.hardware_setup.identity import (
    config_path_for_detected_interface as _config_path_for_detected_interface,
)
from keymasq.gui.wizards.hardware_setup.identity import (
    interface_id_for_config as _interface_id_for_config,
)
from keymasq.gui.wizards.hardware_setup.identity import (
    interface_source_fields as _interface_source_fields,
)
from keymasq.gui.wizards.hardware_setup.types import (
    DetectedDevice,
    DetectedInterface,
)
from keymasq.session.hardware import HardwareManager

log = logging.getLogger("keymasq.gui.hardware_setup")


class HardwareSetupDialog(Adw.Dialog):
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
        super().__init__(
            title="Add Event Device" if select_evdev_only else "Add New Device",
            content_width=500,
            content_height=520,
        )
        if hasattr(self, "set_modal"):
            self.set_modal(True)

        self.hardware_manager = hardware_manager
        self._raw_evdev_only = raw_evdev_only
        self._select_evdev_only = select_evdev_only
        self.detected_devices: dict[str, DetectedDevice] = {}
        self.selected_device: DetectedDevice | None = None
        self.discovered_interfaces: dict[str, DetectedInterface] = {}
        self._configure_mode: str = ""
        self._configure_mode_values: list[str] = ["mouse"]
        self._detect_devices_inflight = False
        self._discover_interfaces_inflight = False
        self._discover_interfaces_request_id = 0
        self._show_raw_evdev_devices = raw_evdev_only

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
        _state: Gdk.ModifierType,
    ) -> bool:
        if keyval in (Gdk.KEY_f, Gdk.KEY_F) and _state & Gdk.ModifierType.CONTROL_MASK:
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
        self.raw_evdev_check.set_active(self._show_raw_evdev_devices)
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
        self.device_search_entry.set_tooltip_text(
            "Filter devices by name, type, ID, or evdev path"
        )
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

        subtitle = Gtk.Label(label="")
        subtitle.add_css_class("dim-label")
        box.append(subtitle)
        self.describe_subtitle = subtitle

        mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mode_label = Gtk.Label(label="Configure as:")
        mode_label.set_halign(Gtk.Align.START)
        mode_row.append(mode_label)

        self.mode_combo_model = Gtk.StringList()
        self.mode_combo = Gtk.DropDown(model=self.mode_combo_model)
        self.mode_combo.connect("notify::selected", self._on_mode_changed)
        mode_row.append(self.mode_combo)
        box.append(mode_row)
        self.mode_row = mode_row

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

    def _detect_devices(self) -> None:
        if self._detect_devices_inflight:
            return

        self._clear_device_selection()
        while row := self.device_list.get_row_at_index(0):
            self.device_list.remove(row)

        self.next_btn.set_sensitive(False)
        self.raw_evdev_check.set_sensitive(False)
        loading_row = Gtk.ListBoxRow()
        loading_row.set_selectable(False)
        loading_label = Gtk.Label(label="Loading devices...")
        loading_label.add_css_class("dim-label")
        loading_label.set_margin_top(8)
        loading_label.set_margin_bottom(8)
        loading_row.set_child(loading_label)
        self.device_list.append(loading_row)
        self._detect_devices_inflight = True
        run_gui_task(
            self._collect_detected_devices,
            self._on_detected_devices_ready,
            on_done=self._on_detected_devices_done,
        )

    def _collect_detected_devices(self) -> dict[str, DetectedDevice]:
        detected_devices: dict[str, DetectedDevice] = {}
        self._detect_devices_via_session(detected_devices)
        return detected_devices

    def _on_detected_devices_done(self) -> None:
        self._detect_devices_inflight = False
        self.raw_evdev_check.set_sensitive(not self._raw_evdev_only)

    def _on_detected_devices_ready(
        self,
        result: GuiTaskResult[dict[str, DetectedDevice]],
    ) -> bool:
        detected_devices = result.value if result.ok and result.value is not None else {}
        while row := self.device_list.get_row_at_index(0):
            self.device_list.remove(row)
        self.detected_devices = detected_devices

        sorted_devices = sorted(
            self.detected_devices.items(),
            key=lambda item: (
                rows.device_type_sort_order(rows.group_device_type(item[1])),
                str(item[1].get("name", "")).lower(),
            ),
        )

        for hardware_id, dev_info in sorted_devices:
            self.device_list.append(
                rows.build_detected_device_row(
                    hardware_id,
                    dev_info,
                    show_raw_evdev_devices=self._show_raw_evdev_devices,
                )
            )

        if not detected_devices:
            self.device_list.append(rows.build_no_devices_row())
        return False

    def _on_cancel_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _on_refresh_clicked(self, _button: Gtk.Button) -> None:
        self._detect_devices()

    def _on_raw_evdev_toggled(self, check: Gtk.CheckButton) -> None:
        if self._raw_evdev_only:
            if not check.get_active():
                check.set_active(True)
            self._show_raw_evdev_devices = True
            return
        self._show_raw_evdev_devices = check.get_active()
        self.selected_device = None
        self.next_btn.set_sensitive(False)
        self._detect_devices()

    def _should_show_interface_expander(
        self,
        interfaces: Sequence[Mapping[str, Any]],
    ) -> bool:
        return rows.should_show_interface_expander(self._show_raw_evdev_devices, interfaces)

    def _raw_device_summary(self, interfaces: Sequence[Mapping[str, Any]]) -> str:
        return rows.raw_device_summary(self._show_raw_evdev_devices, interfaces)

    def _device_in_use(self, dev_info: Mapping[str, Any]) -> bool:
        return rows.device_in_use(dev_info)

    @staticmethod
    def _device_in_use_summary(dev_info: Mapping[str, Any]) -> str:
        return rows.device_in_use_summary(dev_info)

    def _interface_detail_lines(self, iface: Mapping[str, Any]) -> list[str]:
        return rows.interface_detail_lines(iface)

    def _detected_identity_key(
        self,
        *,
        model_id: str,
        device_types: list[str],
        stable_path: str,
        phys: str = "",
        path: str = "",
        config_path: str = "",
    ) -> str:
        return discovery.detected_identity_key(
            show_raw_evdev_devices=self._show_raw_evdev_devices,
            model_id=model_id,
            device_types=device_types,
            stable_path=stable_path,
            phys=phys,
            path=path,
            config_path=config_path,
        )

    def _detect_devices_via_session(
        self,
        detected_devices: dict[str, DetectedDevice],
    ) -> bool:
        return discovery.detect_devices_via_session(
            detected_devices,
            hardware_manager=self.hardware_manager,
            show_raw_evdev_devices=self._show_raw_evdev_devices,
        )

    def _should_include_detected_interface(self, device_types: list[str]) -> bool:
        return discovery.should_include_detected_interface(
            device_types,
            show_raw_evdev_devices=self._show_raw_evdev_devices,
        )

    def _hardware_config_exists(self, hardware_id: str) -> bool:
        return inventory.hardware_config_exists(self.hardware_manager, hardware_id)

    def _configured_hardware_ids(self) -> set[str]:
        return inventory.configured_hardware_ids(self.hardware_manager)

    def _configured_identity_hardware_ids(self) -> dict[str, str]:
        return inventory.configured_identity_hardware_ids(self.hardware_manager)

    def _configured_raw_identity_keys(self, path: str) -> set[str]:
        return inventory.configured_raw_identity_keys(path)

    def _configured_device_stable_path(self, path: str) -> str:
        return inventory.configured_device_stable_path(path)

    def _configured_device_phys(self, device: object) -> str:
        return inventory.configured_device_phys(device)

    def _allocate_hardware_id(self, model_id: str, used_hardware_ids: set[str]) -> str:
        return discovery.allocate_hardware_id(model_id, used_hardware_ids)

    @staticmethod
    def _selected_device_fields(selected_device: DetectedDevice) -> tuple[str, str, str]:
        return (
            str(selected_device.get("vendor_id", "") or ""),
            str(selected_device.get("product_id", "") or ""),
            str(selected_device.get("name", "") or "Device"),
        )

    def _selected_config_id(self, selected_device: DetectedDevice) -> str | None:
        vendor_id, product_id, _name = self._selected_device_fields(selected_device)
        model_id = f"{vendor_id}:{product_id}"
        hardware_id = str(selected_device.get("hardware_id") or model_id)
        if (
            (self._show_raw_evdev_devices or self._selected_uses_model_path(selected_device))
            and not self._device_in_use(selected_device)
        ):
            hardware_id = self._allocate_hardware_id(model_id, self._configured_hardware_ids())
        return hardware_id if hardware_id != model_id else None

    def _selected_uses_model_path(self, selected_device: DetectedDevice) -> bool:
        vendor_id = str(selected_device.get("vendor_id", "") or "")
        product_id = str(selected_device.get("product_id", "") or "")
        if not vendor_id or not product_id:
            return False
        for iface in selected_device.get("interfaces", []) or []:
            if not input_classes_include_gamepad(
                iface.get("device_types"),
                iface.get("device_type"),
            ):
                continue
            stable_path = str(iface.get("stable_path", "") or iface.get("path", "") or "")
            config_path = str(iface.get("config_path") or "")
            if not config_path:
                config_path = _config_path_for_detected_interface(
                    vendor_id,
                    product_id,
                    stable_path,
                )
            if config_path == make_keymasq_device_path(vendor_id, product_id):
                return True
        return False

    def _on_device_selected(self, list_box, row) -> None:
        if row is None:
            self._clear_device_selection()
            return
        if row:
            self.selected_device = self.detected_devices[row.hardware_id]
            if hasattr(row, "_expander"):
                row._expander.set_expanded(True)

            idx = 0
            while True:
                other = self.device_list.get_row_at_index(idx)
                if other is None:
                    break
                if other is not row and hasattr(other, "_expander"):
                    other._expander.set_expanded(False)
                idx += 1

            self._start_discover_interfaces()

    def _clear_device_selection(self) -> None:
        self.selected_device = None
        self.discovered_interfaces = {}
        self._discover_interfaces_request_id += 1
        self._discover_interfaces_inflight = False
        self.next_btn.set_sensitive(False)

    def _interface_device_types(self, iface: Mapping[str, Any]) -> list[str]:
        return templates.interface_device_types(iface)

    def _interface_has_role(self, iface: Mapping[str, Any], role: str) -> bool:
        return templates.interface_has_role(iface, role)

    def _device_type_label(self, device_type: str) -> str:
        return rows.device_type_label(device_type)

    def _refresh_configure_modes(self) -> None:
        if not self.selected_device:
            return

        has_gamepad = False
        has_mouse = False
        has_keyboard = False
        interfaces: Sequence[Mapping[str, Any]] = (
            list(self.discovered_interfaces.values())
            if self.discovered_interfaces
            else self.selected_device.get("interfaces", []) or []
        )
        for iface in interfaces:
            iface_types = self._interface_device_types(iface)
            if "gamepad" in iface_types:
                has_gamepad = True
            if "mouse" in iface_types or "pointstick" in iface_types:
                has_mouse = True
            if "keyboard" in iface_types:
                has_keyboard = True

        self.mode_combo_model.splice(0, self.mode_combo_model.get_n_items(), [])
        self._configure_mode_values = []

        if has_gamepad:
            self.mode_combo_model.append("Gamepad")
            self._configure_mode_values.append("gamepad")
        if has_mouse and has_keyboard:
            self.mode_combo_model.append("Mouse + Keyboard")
            self._configure_mode_values.append("mouse_keyboard")
        if has_mouse:
            self.mode_combo_model.append("Mouse")
            self._configure_mode_values.append("mouse")
        if has_keyboard:
            self.mode_combo_model.append("Keyboard")
            self._configure_mode_values.append("keyboard")

        if not self._configure_mode_values and self._show_raw_evdev_devices:
            self.mode_combo_model.append("Custom")
            self._configure_mode_values = ["custom"]
        elif not self._configure_mode_values:
            self.mode_combo_model.append("Mouse")
            self._configure_mode_values = ["mouse"]

        self._configure_mode = self._preferred_configure_mode()
        self.mode_combo.set_selected(self._configure_mode_values.index(self._configure_mode))
        self.mode_row.set_visible(len(self._configure_mode_values) > 1)
        self._update_describe_mode_ui()

    def _preferred_configure_mode(self) -> str:
        if self._configure_mode in self._configure_mode_values:
            return self._configure_mode
        if "gamepad" in self._configure_mode_values:
            return "gamepad"
        if "mouse_keyboard" in self._configure_mode_values:
            return "mouse_keyboard"
        if "mouse" in self._configure_mode_values:
            return "mouse"
        if "keyboard" in self._configure_mode_values:
            return "keyboard"
        if "custom" in self._configure_mode_values:
            return "custom"
        return self._configure_mode_values[0]

    def _on_mode_changed(self, combo, param) -> None:
        idx = combo.get_selected()
        if idx < 0 or idx >= len(self._configure_mode_values):
            return
        self._configure_mode = self._configure_mode_values[idx]
        self._update_describe_mode_ui()

    def _update_describe_mode_ui(self) -> None:
        is_keyboard = self._configure_mode == "keyboard"
        is_mouse = self._configure_mode == "mouse"
        is_mouse_keyboard = self._configure_mode == "mouse_keyboard"
        is_gamepad = self._configure_mode == "gamepad"
        is_custom = self._configure_mode == "custom"
        self.keyboard_mode_info.set_visible(is_keyboard)
        self.mouse_mode_info.set_visible(is_mouse)
        self.mouse_keyboard_mode_info.set_visible(is_mouse_keyboard)
        self.gamepad_mode_info.set_visible(is_gamepad)
        self.custom_mode_info.set_visible(is_custom)
        if is_gamepad:
            self.describe_subtitle.set_label("Review the detected controller controls")
        elif is_custom:
            self.describe_subtitle.set_label("Create an empty profile for this raw device")
        elif is_mouse_keyboard:
            self.describe_subtitle.set_label("Create a standard keyboard and mouse profile")
        elif is_keyboard:
            self.describe_subtitle.set_label("Create a standard keyboard profile")
        else:
            self.describe_subtitle.set_label("Create a standard mouse profile")

        if self.stack.get_visible_child_name() == "describe":
            self.next_btn.set_label("Save")

    def _device_type_sort_order(self, device_type: DeviceType) -> int:
        return rows.device_type_sort_order(device_type)

    def _group_device_type(self, dev_info: dict) -> DeviceType:
        return rows.group_device_type(dev_info)

    def _group_device_types(self, dev_info: dict) -> list[str]:
        return rows.group_device_types(dev_info)

    def _selected_device_request_key(self, selected_device: DetectedDevice) -> str:
        vendor_id = str(selected_device.get("vendor_id", "") or "")
        product_id = str(selected_device.get("product_id", "") or "")
        return str(
            selected_device.get("hardware_id")
            or selected_device.get("model_id")
            or f"{vendor_id}:{product_id}"
        )

    def _start_discover_interfaces(self) -> None:
        selected_device = self.selected_device
        self.discovered_interfaces = {}
        self._discover_interfaces_request_id += 1
        request_id = self._discover_interfaces_request_id
        self.next_btn.set_sensitive(False)

        if not selected_device:
            self._discover_interfaces_inflight = False
            return

        selected_snapshot = deepcopy(selected_device)
        selected_key = self._selected_device_request_key(selected_device)
        self._discover_interfaces_inflight = True
        run_gui_task(
            lambda: self._discover_interfaces(selected_snapshot),
            lambda result: self._on_discovered_interfaces_ready(
                request_id,
                selected_key,
                result,
            ),
            on_done=lambda: self._on_discovered_interfaces_done(request_id),
        )

    def _on_discovered_interfaces_ready(
        self,
        request_id: int,
        selected_key: str,
        result: GuiTaskResult[dict[str, DetectedInterface]],
    ) -> bool:
        selected_device = self.selected_device
        if (
            request_id != self._discover_interfaces_request_id
            or selected_device is None
            or selected_key != self._selected_device_request_key(selected_device)
        ):
            return False

        self.discovered_interfaces = result.value if result.ok and result.value is not None else {}
        self._refresh_configure_modes()
        self.next_btn.set_sensitive(
            bool(self.discovered_interfaces) and not self._device_in_use(selected_device)
        )
        return False

    def _on_discovered_interfaces_done(self, request_id: int) -> None:
        if request_id == self._discover_interfaces_request_id:
            self._discover_interfaces_inflight = False

    def _discover_interfaces(
        self,
        selected_device: DetectedDevice,
    ) -> dict[str, DetectedInterface]:
        if not selected_device:
            return {}

        vid = str(selected_device.get("vendor_id", "") or "")
        pid = str(selected_device.get("product_id", "") or "")

        discovered_interfaces: dict[str, DetectedInterface] = {}

        selected_interfaces = list(selected_device.get("interfaces", []) or [])
        interfaces = []
        for iface in selected_interfaces:
            raw_path = str(iface.get("path", "") or "")
            if not raw_path:
                continue
            stable_path = str(iface.get("stable_path", "") or resolve_stable_path(raw_path))
            default_config_path = _config_path_for_detected_interface(vid, pid, stable_path)
            config_path = str(iface.get("config_path") or default_config_path)
            capability_names, raw_capabilities = self._read_interface_capabilities(raw_path)
            interfaces.append(
                {
                    "path": raw_path,
                    "stable_path": stable_path,
                    "config_path": config_path,
                    "name": str(iface.get("name", "") or raw_path),
                    "phys": str(iface.get("phys", "") or ""),
                    "device_type": iface.get("device_type", DeviceType.OTHER),
                    "device_types": self._interface_device_types(iface),
                    "capabilities": capability_names,
                    "raw_capabilities": raw_capabilities,
                    **_interface_source_fields(iface),
                }
            )

        if not interfaces:
            interfaces = find_all_interfaces(vid, pid)
            for iface in interfaces:
                raw_path = str(iface.get("path", "") or "")
                stable_path = str(iface.get("stable_path", "") or raw_path)
                iface["config_path"] = _config_path_for_detected_interface(
                    vid,
                    pid,
                    stable_path,
                )
        used_interface_ids: set[str] = set()
        for iface in interfaces:
            merged_iface = {
                **iface,
                "device_types": self._interface_device_types(iface),
            }
            raw_iface_id = _interface_id_for_config(merged_iface, used_interface_ids)
            iface_key = raw_iface_id
            duplicate_index = 2
            while iface_key in discovered_interfaces:
                iface_key = f"{raw_iface_id}_{duplicate_index}"
                duplicate_index += 1

            discovered_interfaces[iface_key] = cast(
                DetectedInterface,
                {
                    "id": raw_iface_id,
                    "stable_path": iface["stable_path"],
                    "config_path": str(iface.get("config_path") or iface["stable_path"]),
                    "path": iface["path"],
                    "name": iface["name"],
                    "phys": str(iface.get("phys", "") or ""),
                    **_interface_source_fields(iface),
                    "device_type": iface.get("device_type", DeviceType.OTHER),
                    "device_types": self._interface_device_types(iface),
                    "capabilities": list(iface.get("capabilities", [])),
                    "raw_capabilities": cast(
                        dict[int, list[object]],
                        iface.get("raw_capabilities") or {},
                    ),
                },
            )
        return discovered_interfaces

    def _on_next(self, button: Gtk.Button) -> None:
        visible_page = self.stack.get_visible_child_name()

        if visible_page == "select":
            selected_device = self.selected_device
            if selected_device is None:
                return
            if self._discover_interfaces_inflight:
                return
            if self._select_evdev_only:
                self._emit_selected_evdev_devices()
                return
            self._configure_mode = self._preferred_configure_mode()
            self.mode_combo.set_selected(self._configure_mode_values.index(self._configure_mode))
            self.describe_title.set_label(
                f"Configure {selected_device.get('name', 'Device')}"
            )
            self._update_describe_mode_ui()
            self.stack.set_visible_child_name("describe")
            self.back_btn.set_visible(True)
            self.cancel_btn.set_visible(False)
            self.next_btn.set_label("Save")

        elif visible_page == "describe":
            if self._configure_mode == "keyboard":
                self._save_keyboard_config()
                return
            if self._configure_mode == "gamepad":
                self._save_gamepad_config()
                return
            if self._configure_mode == "mouse_keyboard":
                self._save_mouse_keyboard_config()
                return
            if self._configure_mode == "custom":
                self._save_custom_config()
                return
            self._save_mouse_config()

    def _emit_selected_evdev_devices(self) -> None:
        evdev_devices = self._build_evdev_devices(list(self.discovered_interfaces.values()))
        if not evdev_devices:
            return
        self.emit("evdev-devices-selected", evdev_devices)
        self.close()

    def _on_back(self, button: Gtk.Button) -> None:
        visible_page = self.stack.get_visible_child_name()

        if visible_page == "describe":
            self.stack.set_visible_child_name("select")
            self.back_btn.set_visible(False)
            self.cancel_btn.set_visible(True)
            self.next_btn.set_sensitive(True)

    def _save_custom_config(self) -> None:
        selected_device = self.selected_device
        if selected_device is None:
            return

        vendor_id, product_id, name = self._selected_device_fields(selected_device)
        interfaces = list(self.discovered_interfaces.values())
        config = HardwareConfig(
            vendor_id=vendor_id,
            product_id=product_id,
            name=name,
            evdev_devices=self._build_evdev_devices(interfaces),
            buttons=[],
            id=self._selected_config_id(selected_device),
        )

        self.hardware_manager.save_hardware(config)
        self.emit("device-created", config)
        self.close()

    def _save_keyboard_config(self) -> None:
        selected_device = self.selected_device
        if selected_device is None:
            return

        vendor_id, product_id, name = self._selected_device_fields(selected_device)
        keyboard_interfaces = self._interfaces_for_roles({"keyboard"})
        interfaces = self._merge_interface_lists(
            keyboard_interfaces,
            list(self.discovered_interfaces.values()),
        )

        primary_keyboard_source = ""
        if keyboard_interfaces:
            primary_keyboard_source = str(keyboard_interfaces[0].get("id", "") or "")

        evdev_devices = self._build_evdev_devices(interfaces)

        buttons = self._build_standard_keyboard_buttons(primary_keyboard_source)

        config = HardwareConfig(
            vendor_id=vendor_id,
            product_id=product_id,
            name=name,
            evdev_devices=evdev_devices,
            buttons=buttons,
            id=self._selected_config_id(selected_device),
        )

        self.hardware_manager.save_hardware(config)
        self.emit("device-created", config)
        self.close()

    def _save_mouse_config(self) -> None:
        selected_device = self.selected_device
        if selected_device is None:
            return

        vendor_id, product_id, name = self._selected_device_fields(selected_device)
        mouse_interfaces = self._interfaces_for_roles({"mouse", "pointstick"})
        interfaces = self._merge_interface_lists(
            mouse_interfaces,
            list(self.discovered_interfaces.values()),
        )
        primary_mouse_source = ""
        if mouse_interfaces:
            primary_mouse_source = str(mouse_interfaces[0].get("id", "") or "")

        config = HardwareConfig(
            vendor_id=vendor_id,
            product_id=product_id,
            name=name,
            evdev_devices=self._build_evdev_devices(interfaces),
            buttons=self._build_standard_mouse_buttons(
                primary_mouse_source,
                include_horizontal=self._interfaces_have_capability(
                    mouse_interfaces,
                    "rel_hwheel",
                ),
            ),
            id=self._selected_config_id(selected_device),
        )

        self.hardware_manager.save_hardware(config)
        self.emit("device-created", config)
        self.close()

    def _save_mouse_keyboard_config(self) -> None:
        selected_device = self.selected_device
        if selected_device is None:
            return

        vendor_id, product_id, name = self._selected_device_fields(selected_device)
        keyboard_interfaces = self._interfaces_for_roles({"keyboard"})
        mouse_interfaces = self._interfaces_for_roles({"mouse", "pointstick"})

        primary_keyboard_source = ""
        if keyboard_interfaces:
            primary_keyboard_source = str(keyboard_interfaces[0].get("id", "") or "")

        primary_mouse_source = ""
        if mouse_interfaces:
            primary_mouse_source = str(mouse_interfaces[0].get("id", "") or "")

        interfaces = self._merge_interface_lists(
            mouse_interfaces,
            keyboard_interfaces,
            list(self.discovered_interfaces.values()),
        )
        buttons = self._build_standard_mouse_buttons(
            primary_mouse_source,
            include_horizontal=self._interfaces_have_capability(
                mouse_interfaces,
                "rel_hwheel",
            ),
        )
        buttons.extend(self._build_standard_keyboard_buttons(primary_keyboard_source))

        config = HardwareConfig(
            vendor_id=vendor_id,
            product_id=product_id,
            name=name,
            evdev_devices=self._build_evdev_devices(interfaces),
            buttons=buttons,
            id=self._selected_config_id(selected_device),
        )

        self.hardware_manager.save_hardware(config)
        self.emit("device-created", config)
        self.close()

    def _read_interface_capabilities(
        self,
        raw_path: str,
    ) -> tuple[list[str], dict[int, list[object]]]:
        if not raw_path:
            return ([], {})

        try:
            device = evdev.InputDevice(raw_path)
        except (OSError, RuntimeError) as exc:
            log.debug("Unable to open interface capability probe device %s: %s", raw_path, exc)
            return ([], {})

        try:
            raw_capabilities = cast(dict[int, list[object]], device.capabilities())
        except Exception:
            log.exception("Unable to read capabilities from %s", raw_path)
            raw_capabilities = {}
        finally:
            try:
                device.close()
            except (OSError, RuntimeError) as exc:
                log.debug("Failed to close capability probe device %s: %s", raw_path, exc)

        return (capability_names_from_capabilities(raw_capabilities), raw_capabilities)

    def _gamepad_interfaces(self) -> list[Mapping[str, Any]]:
        return self._interfaces_for_roles({"gamepad"})

    def _interfaces_for_roles(self, roles: set[str]) -> list[Mapping[str, Any]]:
        return templates.interfaces_for_roles(
            list(self.discovered_interfaces.values()),
            roles,
        )

    def _merge_interface_lists(
        self,
        *interface_lists: Sequence[Mapping[str, Any]],
    ) -> list[Mapping[str, Any]]:
        return templates.merge_interface_lists(*interface_lists)

    def _interfaces_have_capability(
        self,
        interfaces: Sequence[Mapping[str, Any]],
        capability: str,
    ) -> bool:
        return templates.interfaces_have_capability(interfaces, capability)

    def _build_evdev_devices(
        self,
        interfaces: Sequence[Mapping[str, Any]],
    ) -> list[EvdevDevice]:
        return templates.build_evdev_devices(interfaces)

    def _build_standard_mouse_buttons(
        self,
        source_id: str,
        *,
        include_horizontal: bool = False,
    ) -> list[ButtonDefinition]:
        return templates.build_standard_mouse_buttons(
            source_id,
            include_horizontal=include_horizontal,
        )

    def _standard_wheel_buttons(
        self,
        source_id: str,
        include_horizontal: bool,
    ) -> list[ButtonDefinition]:
        return templates.standard_wheel_buttons(source_id, include_horizontal)

    def _build_gamepad_buttons(
        self,
        interfaces: Sequence[Mapping[str, Any]],
    ) -> list[ButtonDefinition]:
        return templates.build_gamepad_buttons(interfaces)

    def _build_gamepad_analog_inputs(
        self,
        interfaces: Sequence[Mapping[str, Any]],
    ) -> list[AnalogInputDefinition]:
        return templates.build_gamepad_analog_inputs(interfaces)

    def _save_gamepad_config(self) -> None:
        selected_device = self.selected_device
        if selected_device is None:
            return

        vendor_id, product_id, name = self._selected_device_fields(selected_device)
        gamepad_interfaces = self._gamepad_interfaces()
        interfaces = self._merge_interface_lists(
            gamepad_interfaces,
            list(self.discovered_interfaces.values()),
        )

        config = HardwareConfig(
            vendor_id=vendor_id,
            product_id=product_id,
            name=name,
            evdev_devices=self._build_evdev_devices(interfaces),
            buttons=self._build_gamepad_buttons(gamepad_interfaces),
            analog_inputs=self._build_gamepad_analog_inputs(gamepad_interfaces),
            id=self._selected_config_id(selected_device),
        )

        self.hardware_manager.save_hardware(config)
        self.emit("device-created", config)
        self.close()

    def _build_standard_keyboard_buttons(self, source_id: str) -> list[ButtonDefinition]:
        return templates.build_standard_keyboard_buttons(source_id)

    def _keyboard_label_from_evdev(self, key_name: str) -> str:
        return templates.keyboard_label_from_evdev(key_name)

    def do_close_request(self) -> bool:
        return False
