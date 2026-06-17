import logging
import os
import re
import subprocess
from typing import Any, cast

import evdev
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import (
    Adw,  # pyright: ignore[reportAttributeAccessIssue]
    Gdk,  # pyright: ignore[reportAttributeAccessIssue]
    GLib,  # pyright: ignore[reportAttributeAccessIssue]
    GObject,  # pyright: ignore[reportAttributeAccessIssue]
    Gtk,  # pyright: ignore[reportAttributeAccessIssue]
)

from keymasq.common.devices import (
    INPUT_CLASS_ORDER,
    canonical_gamepad_button_name,
    capability_name,
    capability_names_from_capabilities,
    detect_input_classes,
    find_all_interfaces,
    gamepad_button_label,
    get_interface_id,
    input_class_label,
    input_classes_include_gamepad,
    is_by_id_path,
    is_low_res_wheel_evdev,
    make_keymasq_device_path,
    normalize_input_classes,
    ordered_gamepad_button_names,
    primary_input_class,
    resolve_stable_path,
    wheel_button_id,
    wheel_label,
)
from keymasq.common.models import (
    AnalogAxisDefinition,
    AnalogInputDefinition,
    ButtonDefinition,
    DeviceType,
    EvdevDevice,
    HardwareConfig,
)
from keymasq.gui.session_client import (
    GuiTaskResult,
    run_gui_task,
    session_request,
    session_request_async,
)
from keymasq.gui.widgets.fuzzy_search import fuzzy_query_matches, install_listbox_fuzzy_filter
from keymasq.session.hardware import HardwareManager

DetectedDevice = dict[str, Any]
DetectedInterface = dict[str, Any]
DetectedButton = dict[str, Any]
log = logging.getLogger("keymasq.gui.hardware_setup")


def _make_capture_status_row(status_label: Gtk.Label) -> tuple[Gtk.Box, Gtk.Widget]:
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    row.set_halign(Gtk.Align.START)
    row.set_margin_top(12)
    dot = Gtk.Box()
    dot.add_css_class("capture-recording-dot")
    dot.set_size_request(10, 10)
    dot.set_valign(Gtk.Align.CENTER)
    dot.set_visible(False)
    row.append(dot)
    row.append(status_label)
    return row, dot


def _device_search_text(hardware_id: str, dev_info: DetectedDevice) -> str:
    interfaces = dev_info.get("interfaces", [])
    interface_text: list[str] = []
    if isinstance(interfaces, list):
        for iface in interfaces:
            if not isinstance(iface, dict):
                continue
            interface_text.extend(
                str(iface.get(key, "") or "")
                for key in (
                    "path",
                    "stable_path",
                    "config_path",
                    "phys",
                    "interface_id",
                    "device_type",
                )
            )
            interface_text.extend(str(t) for t in iface.get("device_types", []) or [])
    return " ".join(
        [
            hardware_id,
            str(dev_info.get("name", "") or ""),
            str(dev_info.get("display_name", "") or ""),
            str(dev_info.get("model_id", "") or ""),
            str(dev_info.get("vendor_id", "") or ""),
            str(dev_info.get("product_id", "") or ""),
            " ".join(str(t) for t in dev_info.get("device_types", []) or []),
            " ".join(interface_text),
        ]
    )


def _set_capture_status(
    status_label: Gtk.Label,
    dot: Gtk.Widget,
    text: object,
    *,
    recording: bool = False,
) -> None:
    status_label.set_label(str(text))
    dot.set_visible(recording)


def _strip_input_suffix(phys: str) -> str:
    return re.sub(r"/input\d+$", "", str(phys or "").strip())


def _is_usb_phys(phys: str) -> bool:
    return str(phys or "").startswith("usb-")


def _by_id_device_stem(stable_path: str) -> str:
    name = str(stable_path or "").rsplit("/", 1)[-1]
    name = re.sub(r"-event-[^-]+$", "", name)
    name = re.sub(r"-event$", "", name)
    name = re.sub(r"-(mouse|joystick|kbd)$", "", name)
    name = re.sub(r"-if\d+(?:_[^-]+)?$", "", name)
    return name


def _fallback_interface_id_for_type(device_type: DeviceType) -> str:
    if device_type == DeviceType.GAMEPAD:
        return "gamepad"
    if device_type == DeviceType.KEYBOARD:
        return "kbd"
    if device_type == DeviceType.MOUSE:
        return "mouse"
    return "input"


def _dedupe_interface_id(base_id: str, used_ids: set[str]) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_]+", "_", str(base_id or "").strip().lower()).strip("_")
    candidate = clean or "input"
    if candidate not in used_ids:
        used_ids.add(candidate)
        return candidate
    index = 2
    while f"{candidate}_{index}" in used_ids:
        index += 1
    deduped = f"{candidate}_{index}"
    used_ids.add(deduped)
    return deduped


def _interface_id_for_config(iface: dict, used_ids: set[str]) -> str:
    stable_path = str(iface.get("stable_path", "") or "")
    config_path = str(iface.get("config_path", "") or "")
    if is_by_id_path(stable_path) or is_by_id_path(config_path):
        by_id_path = stable_path if is_by_id_path(stable_path) else config_path
        return _dedupe_interface_id(get_interface_id(by_id_path), used_ids)
    return _dedupe_interface_id(
        _fallback_interface_id_for_type(primary_input_class(iface.get("device_types"))),
        used_ids,
    )


def _config_path_for_detected_interface(
    vendor_id: str,
    product_id: str,
    stable_path: str,
) -> str:
    if is_by_id_path(stable_path):
        return stable_path
    return make_keymasq_device_path(vendor_id, product_id)


def _interface_source_fields(dev: dict[str, Any]) -> dict[str, object]:
    fields: dict[str, object] = {}
    if bool(dev.get("grabbed_by_keymasq", False)):
        fields["grabbed_by_keymasq"] = True
    for key in (
        "source_hardware_id",
        "source_interface_id",
        "source_stable_path",
        "source_path",
    ):
        value = str(dev.get(key, "") or "")
        if value:
            fields[key] = value
    return fields


def _in_use_row_key(hardware_id: str, path: str, stable_path: str) -> str:
    suffix = path or stable_path or "in-use"
    return f"{hardware_id}#{suffix}"


def _raw_row_key(path: str, stable_path: str) -> str:
    return f"raw:{stable_path or path}"


def _logical_hardware_identity_key(
    *,
    model_id: str,
    device_types: list[str],
    stable_path: str,
    phys: str = "",
    path: str = "",
    config_path: str = "",
) -> str:
    normalized_types = normalize_input_classes(device_types)
    stable_key = str(stable_path or "").strip()
    config_key = str(config_path or "").strip()
    identity_path = stable_key
    # Preserve a durable by-id config identity when live udev only exposes eventN.
    if not is_by_id_path(identity_path) and is_by_id_path(config_key):
        identity_path = config_key
    if input_classes_include_gamepad(normalized_types) and is_by_id_path(identity_path):
        return f"path:{identity_path}"
    if is_by_id_path(identity_path):
        return f"by-id:{_by_id_device_stem(identity_path)}"
    phys_key = str(phys or "").strip()
    phys_base = _strip_input_suffix(phys_key)
    if phys_base == "py-evdev-uinput":
        return f"uinput-model:{model_id}"
    if phys_base and not _is_usb_phys(phys_base):
        return f"phys:{phys_base}"
    path_key = str(stable_key or path or "").strip()
    if path_key:
        return f"path:{path_key}"
    if config_key and not config_key.startswith("keymasq:"):
        return f"path:{config_key}"
    return f"model:{model_id}"


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
        self.button_definitions: list[DetectedButton] = []
        self.current_button_index = 0
        self._configure_mode: str = ""
        self._configure_mode_values: list[str] = ["mouse"]
        self._capturing = False
        self._capture_poll_id = None
        self._capture_poll_inflight = False
        self._capture_remaining_ids: list[str] = []
        self._capture_hardware_id: str | None = None
        self._detect_devices_inflight = False
        self._show_raw_evdev_devices = raw_evdev_only

        self._setup_escape_close()
        self._setup_ui()
        self._detect_devices()
        self.connect("closed", self._on_closed)

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

    def _on_closed(self, *_args: object) -> None:
        if self._capturing:
            self._stop_capture()

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

    def _setup_page_capture(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)

        self.capture_title = Gtk.Label()
        self.capture_title.add_css_class("title-2")
        box.append(self.capture_title)

        self.capture_instruction = Gtk.Label()
        self.capture_instruction.add_css_class("dim-label")
        box.append(self.capture_instruction)

        self.capture_status = Gtk.Label()
        capture_status_row, self.capture_status_dot = _make_capture_status_row(
            self.capture_status
        )
        box.append(capture_status_row)

        self.capture_progress = Gtk.ProgressBar()
        self.capture_progress.set_margin_top(12)
        box.append(self.capture_progress)

        self.captured_list = Gtk.ListBox()
        self.captured_list.set_margin_top(12)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_child(self.captured_list)
        box.append(scrolled)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        btn_box.set_margin_top(12)

        self.skip_btn = Gtk.Button(label="Skip")
        self.skip_btn.connect("clicked", self._on_skip)
        btn_box.append(self.skip_btn)

        self.capture_btn = Gtk.Button(label="Start Capture")
        self.capture_btn.connect("clicked", self._on_start_capture)
        self.capture_btn.add_css_class("suggested-action")
        btn_box.append(self.capture_btn)

        box.append(btn_box)

        self.stack.add_titled(box, "capture", "Capture Buttons")

    def _detect_devices(self) -> None:
        if self._detect_devices_inflight:
            return

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

    def _collect_detected_devices(self) -> dict[str, dict]:
        detected_devices: dict[str, dict] = {}
        lsusb_map = self._get_lsusb_name_map()
        loaded_from_session = self._detect_devices_via_session(detected_devices)
        if not loaded_from_session:
            self._detect_devices_locally(lsusb_map, detected_devices)
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
                self._device_type_sort_order(self._group_device_type(item[1])),
                str(item[1].get("name", "")).lower(),
            ),
        )

        for hardware_id, dev_info in sorted_devices:
            row = Gtk.ListBoxRow()
            row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            row_box.set_margin_top(8)
            row_box.set_margin_bottom(8)
            row_box.set_margin_start(12)
            row_box.set_margin_end(12)

            name = Gtk.Label(label=dev_info.get("display_name", dev_info["name"]))
            name.set_halign(Gtk.Align.START)
            row_box.append(name)

            grouped_types = self._group_device_types(dev_info)
            interfaces = dev_info.get("interfaces", [])
            iface_count = len(interfaces)
            type_text = " · ".join(self._device_type_label(t) for t in grouped_types)
            iface_text = "interface" if iface_count == 1 else "interfaces"

            model_id = str(dev_info.get("model_id", hardware_id))
            vidpid = Gtk.Label(
                label=f"{model_id} · {iface_count} evdev {iface_text} · {type_text}"
            )
            vidpid.add_css_class("dim-label")
            vidpid.add_css_class("caption")
            vidpid.set_halign(Gtk.Align.START)
            row_box.append(vidpid)

            raw_summary = self._raw_device_summary(interfaces)
            if raw_summary:
                raw_label = Gtk.Label(label=raw_summary)
                raw_label.add_css_class("dim-label")
                raw_label.add_css_class("caption")
                raw_label.set_halign(Gtk.Align.START)
                row_box.append(raw_label)

            in_use_summary = self._device_in_use_summary(dev_info)
            if in_use_summary:
                in_use = Gtk.Label(label=in_use_summary)
                in_use.add_css_class("caption")
                in_use.set_halign(Gtk.Align.START)
                row_box.append(in_use)

            expander: Gtk.Expander | None = None
            if self._should_show_interface_expander(interfaces):
                interface_expander = Gtk.Expander(label="Evdev devices")
                interface_expander.set_expanded(False)

                iface_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                iface_box.set_margin_top(4)
                iface_box.set_margin_start(12)

                for iface in interfaces:
                    for detail in self._interface_detail_lines(iface):
                        iface_detail = Gtk.Label(label=detail)
                        iface_detail.add_css_class("caption")
                        iface_detail.set_halign(Gtk.Align.START)
                        iface_box.append(iface_detail)

                interface_expander.set_child(iface_box)
                row_box.append(interface_expander)
                expander = interface_expander

            row.set_child(row_box)
            row.hardware_id = hardware_id
            row._search_text = _device_search_text(hardware_id, dev_info)
            if expander is not None:
                row._expander = expander
            self.device_list.append(row)

        if not detected_devices:
            row = Gtk.ListBoxRow()
            row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            row_box.set_margin_top(8)
            row_box.set_margin_bottom(8)
            row_box.set_margin_start(12)
            row_box.set_margin_end(12)

            label = Gtk.Label(label="No input devices reported by keymasqd")
            label.set_halign(Gtk.Align.START)
            row_box.append(label)

            hint = Gtk.Label(
                label=(
                    "Ensure keymasqd is running and has access to /dev/input/event*. "
                    "Touchpads are detected but not supported in Add Device yet."
                )
            )
            hint.add_css_class("dim-label")
            hint.add_css_class("caption")
            hint.set_halign(Gtk.Align.START)
            row_box.append(hint)

            row.set_selectable(False)
            row.set_child(row_box)
            self.device_list.append(row)
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

    def _should_show_interface_expander(self, interfaces: list[dict]) -> bool:
        if self._show_raw_evdev_devices:
            return False
        return bool(interfaces)

    def _raw_device_summary(self, interfaces: list[dict]) -> str:
        if not self._show_raw_evdev_devices or not interfaces:
            return ""
        iface = interfaces[0]
        parts = [str(iface.get("path", "") or "")]
        stable_path = str(iface.get("stable_path", "") or "")
        if stable_path and stable_path not in parts:
            parts.append(stable_path)
        phys = str(iface.get("phys", "") or "")
        if phys:
            parts.append(phys)
        return " · ".join(part for part in parts if part)

    def _device_in_use(self, dev_info: dict) -> bool:
        return any(
            bool(iface.get("grabbed_by_keymasq", False))
            or bool(iface.get("configured_hardware_id", False))
            for iface in dev_info.get("interfaces", [])
            if isinstance(iface, dict)
        )

    @staticmethod
    def _device_in_use_summary(dev_info: dict) -> str:
        for iface in dev_info.get("interfaces", []):
            if not isinstance(iface, dict):
                continue
            if not bool(iface.get("grabbed_by_keymasq", False)):
                configured_hardware_id = str(iface.get("configured_hardware_id", "") or "")
                if configured_hardware_id:
                    return f"Configured as {configured_hardware_id}"
                continue
            hardware_id = str(iface.get("source_hardware_id", "") or "")
            interface_id = str(iface.get("source_interface_id", "") or "")
            if hardware_id and interface_id:
                return f"In use by {hardware_id} ({interface_id})"
            if hardware_id:
                return f"In use by {hardware_id}"
            return "In use by Keymasq"
        return ""

    def _interface_detail_lines(self, iface: dict) -> list[str]:
        lines = [f"- {iface.get('name', '') or iface.get('path', '')}"]
        path = str(iface.get("path", "") or "")
        stable_path = str(iface.get("stable_path", "") or "")
        phys = str(iface.get("phys", "") or "")
        if path:
            lines.append(f"  path: {path}")
        if stable_path and stable_path != path:
            lines.append(f"  stable: {stable_path}")
        if phys:
            lines.append(f"  phys: {phys}")
        in_use = self._device_in_use_summary({"interfaces": [iface]})
        if in_use:
            lines.append(f"  {in_use}")
        return lines

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
        if self._show_raw_evdev_devices:
            return f"raw:{stable_path or path}"
        return _logical_hardware_identity_key(
            model_id=model_id,
            device_types=device_types,
            stable_path=stable_path,
            phys=phys,
            path=path,
            config_path=config_path,
        )

    def _detect_devices_locally(
        self,
        lsusb_map: dict[str, dict[str, str]],
        detected_devices: dict[str, dict],
    ) -> None:
        used_hardware_ids = self._configured_hardware_ids()
        configured_identity_hardware_ids = self._configured_identity_hardware_ids()
        pending_identity_hardware_ids: dict[str, str] = {}
        has_config_inventory = callable(getattr(self.hardware_manager, "list_hardware", None))

        def local_sort_key(path: str) -> str:
            try:
                return resolve_stable_path(path)
            except (OSError, RuntimeError, ValueError):
                return path

        for path in sorted(evdev.list_devices(), key=local_sort_key):
            try:
                device = evdev.InputDevice(path)
                try:
                    if self._should_skip_detected_device(device):
                        continue
                    info = device.info
                    vendor_id = f"{info.vendor:04x}"
                    product_id = f"{info.product:04x}"
                    vid_pid = f"{vendor_id}:{product_id}"
                    device_types = detect_input_classes(device)
                    if not self._should_include_detected_interface(device_types):
                        continue
                    stable_path = resolve_stable_path(path)
                    config_path = _config_path_for_detected_interface(
                        vendor_id,
                        product_id,
                        stable_path,
                    )
                    phys = str(getattr(device, "phys", "") or "")
                    identity_key = self._detected_identity_key(
                        model_id=vid_pid,
                        device_types=device_types,
                        stable_path=stable_path,
                        phys=phys,
                        path=path,
                        config_path=config_path,
                    )
                    configured_hardware_id = configured_identity_hardware_ids.get(identity_key, "")
                    if not self._show_raw_evdev_devices and (
                        configured_hardware_id
                        or (not has_config_inventory and self._hardware_config_exists(vid_pid))
                    ):
                        continue
                    if self._show_raw_evdev_devices and configured_hardware_id:
                        hardware_id = configured_hardware_id
                        device_key = _in_use_row_key(hardware_id, path, stable_path)
                        configured_fields = {"configured_hardware_id": hardware_id}
                    elif self._show_raw_evdev_devices:
                        hardware_id = vid_pid
                        device_key = _raw_row_key(path, stable_path)
                        configured_fields = {}
                    else:
                        hardware_id = pending_identity_hardware_ids.get(identity_key)
                        if hardware_id is None:
                            hardware_id = self._allocate_hardware_id(vid_pid, used_hardware_ids)
                            used_hardware_ids.add(hardware_id)
                            pending_identity_hardware_ids[identity_key] = hardware_id
                        device_key = hardware_id
                        configured_fields = {}
                    device_type = primary_input_class(device_types)
                    lsusb_entry = lsusb_map.get(vid_pid)
                    display_name = (
                        lsusb_entry["name"] if lsusb_entry and lsusb_entry["name"] else device.name
                    )
                    human_name = (
                        lsusb_entry["name"] if lsusb_entry and lsusb_entry["name"] else device.name
                    )

                    if device_key not in detected_devices:
                        detected_devices[device_key] = {
                            "name": human_name,
                            "display_name": display_name,
                            "hardware_id": hardware_id,
                            "model_id": vid_pid,
                            "vendor_id": vendor_id,
                            "product_id": product_id,
                            "paths": [path],
                            "interfaces": [
                                {
                                    "path": path,
                                    "stable_path": stable_path,
                                    "name": device.name,
                                    "phys": phys,
                                    "device_type": device_type,
                                    "device_types": device_types,
                                    **configured_fields,
                                }
                            ],
                        }
                    else:
                        detected_devices[device_key]["paths"].append(path)
                        detected_devices[device_key]["interfaces"].append(
                            {
                                "path": path,
                                "stable_path": stable_path,
                                "name": device.name,
                                "phys": phys,
                                "device_type": device_type,
                                "device_types": device_types,
                                **configured_fields,
                            }
                        )
                finally:
                    device.close()

            except Exception:
                log.exception("Skipping local input device %s", path)

    def _detect_devices_via_session(self, detected_devices: dict[str, dict]) -> bool:
        result = session_request(
            {
                "command": "list_devices_for_recording",
                "include_other": self._show_raw_evdev_devices,
            },
            timeout=3.0,
        ) or {}
        if result.get("status") != "ok":
            return False

        used_hardware_ids = self._configured_hardware_ids()
        configured_identity_hardware_ids = self._configured_identity_hardware_ids()
        pending_identity_hardware_ids: dict[str, str] = {}
        has_config_inventory = callable(getattr(self.hardware_manager, "list_hardware", None))

        raw_devices = result.get("devices", [])
        session_devices: list[dict[str, Any]] = [
            cast(dict[str, Any], dev)
            for dev in raw_devices
            if isinstance(dev, dict)
        ]
        session_devices.sort(
            key=lambda dev: (
                str(dev.get("vendor_id", "") or "").lower(),
                str(dev.get("product_id", "") or "").lower(),
                str(dev.get("stable_path", "") or dev.get("path", "") or ""),
            )
        )

        for dev in session_devices:
            if self._should_skip_detected_device_info(dev):
                continue

            vendor_id = str(dev.get("vendor_id", "") or "").lower()
            product_id = str(dev.get("product_id", "") or "").lower()
            if not vendor_id or not product_id:
                continue

            vid_pid = f"{vendor_id}:{product_id}"
            path = str(dev.get("path", "") or "")
            name = str(dev.get("name", "") or path or vid_pid)
            dtype_raw = str(dev.get("device_type", "other") or "other")
            dtype = primary_input_class(dev.get("device_types") or [dtype_raw])
            device_types = normalize_input_classes(dev.get("device_types"), dtype_raw)
            if not self._should_include_detected_interface(device_types):
                continue
            stable_path = str(dev.get("stable_path", "") or path)
            config_path = _config_path_for_detected_interface(
                vendor_id,
                product_id,
                stable_path,
            )
            phys = str(dev.get("phys", "") or "")
            identity_key = self._detected_identity_key(
                model_id=vid_pid,
                device_types=device_types,
                stable_path=stable_path,
                phys=phys,
                path=path,
                config_path=config_path,
            )
            configured_hardware_id = configured_identity_hardware_ids.get(identity_key, "")
            if not self._show_raw_evdev_devices and (
                configured_hardware_id
                or (not has_config_inventory and self._hardware_config_exists(vid_pid))
            ):
                continue
            source_fields = _interface_source_fields(dev)
            is_grabbed = bool(source_fields.get("grabbed_by_keymasq", False))
            source_hardware_id = str(source_fields.get("source_hardware_id", "") or "")
            if self._show_raw_evdev_devices and is_grabbed and source_hardware_id:
                hardware_id = source_hardware_id
                device_key = _in_use_row_key(hardware_id, path, stable_path)
                configured_fields: dict[str, object] = {}
            elif self._show_raw_evdev_devices and configured_hardware_id:
                hardware_id = configured_hardware_id
                device_key = _in_use_row_key(hardware_id, path, stable_path)
                configured_fields = {"configured_hardware_id": hardware_id}
            elif self._show_raw_evdev_devices:
                hardware_id = vid_pid
                device_key = _raw_row_key(path, stable_path)
                configured_fields = {}
            else:
                hardware_id = pending_identity_hardware_ids.get(identity_key)
                if hardware_id is None:
                    hardware_id = self._allocate_hardware_id(vid_pid, used_hardware_ids)
                    used_hardware_ids.add(hardware_id)
                    pending_identity_hardware_ids[identity_key] = hardware_id
                device_key = hardware_id
                configured_fields = {}

            if device_key not in detected_devices:
                detected_devices[device_key] = {
                    "name": name,
                    "display_name": name,
                    "hardware_id": hardware_id,
                    "model_id": vid_pid,
                    "vendor_id": vendor_id,
                    "product_id": product_id,
                    "paths": [path] if path else [],
                    "interfaces": [
                        {
                            "path": path,
                            "stable_path": stable_path,
                            "name": name,
                            "phys": phys,
                            "device_type": dtype,
                            "device_types": device_types,
                            **source_fields,
                            **configured_fields,
                        }
                    ]
                    if path
                    else [],
                }
            else:
                if path:
                    detected_devices[device_key]["paths"].append(path)
                    detected_devices[device_key]["interfaces"].append(
                        {
                            "path": path,
                            "stable_path": stable_path,
                            "name": name,
                            "phys": phys,
                            "device_type": dtype,
                            "device_types": device_types,
                            **source_fields,
                            **configured_fields,
                        }
                    )

        return bool(detected_devices)

    def _should_skip_detected_device(self, device: evdev.InputDevice) -> bool:
        device_info = {
            "name": device.name,
            "phys": getattr(device, "phys", None),
        }
        if self._should_skip_detected_device_info(device_info):
            return True

        phys = str(getattr(device, "phys", "") or "").strip().lower()
        return phys == "py-evdev-uinput" and not self._show_raw_evdev_devices

    def _should_skip_detected_device_info(self, device_info: dict[str, Any]) -> bool:
        name = str(device_info.get("name", "") or "").strip().lower()
        recording_kind = str(device_info.get("recording_kind", "") or "").strip().lower()

        if "keymasq" in name:
            return True
        if recording_kind in {"keymasq_output", "keymasq_passthrough"}:
            return True

        return False

    def _should_include_detected_interface(self, device_types: list[str]) -> bool:
        if self._show_raw_evdev_devices:
            return True
        return "touchpad" not in normalize_input_classes(device_types)

    def _hardware_config_exists(self, hardware_id: str) -> bool:
        getter = getattr(self.hardware_manager, "get_hardware", None)
        if callable(getter):
            return getter(hardware_id) is not None

        list_ids = getattr(self.hardware_manager, "list_hardware_ids", None)
        if callable(list_ids):
            configured_ids = list_ids()
            if isinstance(configured_ids, list):
                return hardware_id in [str(item) for item in configured_ids]

        return False

    def _configured_hardware_ids(self) -> set[str]:
        list_ids = getattr(self.hardware_manager, "list_hardware_ids", None)
        if callable(list_ids):
            try:
                configured_ids = list_ids()
            except Exception:
                log.exception("Unable to list configured hardware IDs")
                configured_ids = []
            if isinstance(configured_ids, list):
                return {str(item) for item in configured_ids}

        list_hardware = getattr(self.hardware_manager, "list_hardware", None)
        if callable(list_hardware):
            try:
                return {
                    str(getattr(config, "hardware_id", "") or "")
                    for config in cast(list[object], list_hardware())
                    if str(getattr(config, "hardware_id", "") or "")
                }
            except Exception:
                log.exception("Unable to list configured hardware")
                return set()

        return set()

    def _configured_identity_keys(self) -> set[str]:
        return set(self._configured_identity_hardware_ids())

    def _configured_identity_hardware_ids(self) -> dict[str, str]:
        list_hardware = getattr(self.hardware_manager, "list_hardware", None)
        if not callable(list_hardware):
            return {}
        try:
            configs = cast(list[object], list_hardware())
        except Exception:
            log.exception("Unable to list configured hardware identity keys")
            return {}

        keys: dict[str, str] = {}
        for config in configs:
            model_id = str(getattr(config, "model_id", "") or "")
            if not model_id:
                continue
            hardware_id = str(getattr(config, "hardware_id", "") or model_id)
            for device in getattr(config, "evdev_devices", []):
                path = str(getattr(device, "path", "") or "")
                if not path:
                    continue
                device_type = getattr(getattr(device, "device_type", None), "value", None)
                device_types = [str(device_type or "other")]
                for key in self._configured_raw_identity_keys(path):
                    keys.setdefault(key, hardware_id)
                keys.setdefault(
                    _logical_hardware_identity_key(
                        model_id=model_id,
                        device_types=device_types,
                        stable_path=self._configured_device_stable_path(path),
                        phys=self._configured_device_phys(device),
                        path=path,
                        config_path=path,
                    ),
                    hardware_id,
                )
        return keys

    def _configured_raw_identity_keys(self, path: str) -> set[str]:
        candidates = {str(path or "")}
        stable_path = self._configured_device_stable_path(path)
        if stable_path:
            candidates.add(stable_path)
        try:
            real_path = os.path.realpath(path)
        except OSError:
            real_path = ""
        if real_path:
            candidates.add(real_path)
        return {f"raw:{candidate}" for candidate in candidates if candidate}

    def _configured_device_stable_path(self, path: str) -> str:
        path = str(path or "")
        if not path:
            return ""
        candidates = [path]
        try:
            real_path = os.path.realpath(path)
        except OSError:
            real_path = ""
        if real_path and real_path != path:
            candidates.append(real_path)

        for candidate in candidates:
            try:
                stable_path = resolve_stable_path(candidate)
            except (OSError, RuntimeError, ValueError) as exc:
                log.debug("Unable to resolve configured device path %s: %s", candidate, exc)
                stable_path = ""
            if stable_path and is_by_id_path(stable_path):
                return stable_path
        return path

    def _configured_device_phys(self, device: object) -> str:
        phys = str(getattr(device, "phys", "") or "")
        if phys:
            return phys

        path = str(getattr(device, "path", "") or "")
        if not path:
            return ""

        try:
            input_device = evdev.InputDevice(path)
        except (OSError, RuntimeError) as exc:
            log.debug("Unable to read configured input device %s: %s", path, exc)
            return ""
        try:
            return str(getattr(input_device, "phys", "") or "")
        finally:
            try:
                input_device.close()
            except (OSError, RuntimeError) as exc:
                log.debug("Failed to close configured input device %s: %s", path, exc)

    def _allocate_hardware_id(self, model_id: str, used_hardware_ids: set[str]) -> str:
        if model_id not in used_hardware_ids:
            return model_id

        index = 2
        while True:
            candidate = f"{model_id}@{index}"
            if candidate not in used_hardware_ids:
                return candidate
            index += 1

    def _selected_config_id(self, selected_device: DetectedDevice) -> str | None:
        model_id = f"{selected_device['vendor_id']}:{selected_device['product_id']}"
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
        for iface in selected_device.get("interfaces", []):
            if not isinstance(iface, dict):
                continue
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

            self._discover_interfaces()
            self._refresh_configure_modes()
            self.next_btn.set_sensitive(not self._device_in_use(self.selected_device))

    def _clear_device_selection(self) -> None:
        self.selected_device = None
        self.next_btn.set_sensitive(False)

    def _interface_device_types(self, iface: dict) -> list[str]:
        return normalize_input_classes(iface.get("device_types"), iface.get("device_type"))

    def _interface_has_role(self, iface: dict, role: str) -> bool:
        return role in self._interface_device_types(iface)

    def _device_type_label(self, device_type: str) -> str:
        return input_class_label(device_type)

    def _refresh_configure_modes(self) -> None:
        if not self.selected_device:
            return

        has_gamepad = False
        has_mouse = False
        has_keyboard = False
        for iface in self.selected_device.get("interfaces", []):
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
        order = {
            DeviceType.MOUSE: 0,
            DeviceType.KEYBOARD: 1,
            DeviceType.GAMEPAD: 2,
            DeviceType.OTHER: 3,
        }
        return order.get(device_type, 99)

    def _group_device_type(self, dev_info: dict) -> DeviceType:
        interfaces = dev_info.get("interfaces", [])
        if not interfaces:
            return DeviceType.OTHER

        best = DeviceType.OTHER
        best_order = 99
        for iface in interfaces:
            iface_type = iface.get("device_type", DeviceType.OTHER)
            order = self._device_type_sort_order(iface_type)
            if order < best_order:
                best = iface_type
                best_order = order
        return best

    def _group_device_types(self, dev_info: dict) -> list[str]:
        interfaces = dev_info.get("interfaces", [])
        type_set: set[str] = set()
        for iface in interfaces:
            type_set.update(self._interface_device_types(iface))

        if not type_set:
            return ["other"]

        return sorted(type_set, key=INPUT_CLASS_ORDER.index)

    def _get_lsusb_name_map(self) -> dict[str, dict[str, str]]:
        try:
            output = subprocess.check_output(["lsusb"], text=True, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("Unable to read USB device names with lsusb: %s", exc)
            return {}

        result: dict[str, dict[str, str]] = {}
        pattern = re.compile(
            r"^Bus\s+\d+\s+Device\s+\d+:\s+ID\s+([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\s*(.*)$"
        )

        for raw_line in output.splitlines():
            line = raw_line.strip()
            match = pattern.match(line)
            if not match:
                continue

            vid = match.group(1).lower()
            pid = match.group(2).lower()
            desc = match.group(3).strip()
            vid_pid = f"{vid}:{pid}"

            if vid_pid not in result:
                result[vid_pid] = {
                    "full": line,
                    "name": desc,
                }

        return result

    def _discover_interfaces(self) -> None:
        if not self.selected_device:
            return

        vid = self.selected_device["vendor_id"]
        pid = self.selected_device["product_id"]
        self._capture_hardware_id = str(
            self.selected_device.get("hardware_id") or f"{vid}:{pid}"
        )

        self.discovered_interfaces = {}

        interfaces = []
        for iface in self.selected_device.get("interfaces", []):
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
        iface_info_by_path = {
            iface.get("path"): {
                "device_type": iface.get("device_type", DeviceType.OTHER),
                "device_types": self._interface_device_types(iface),
            }
            for iface in self.selected_device.get("interfaces", [])
        }
        used_interface_ids: set[str] = set()
        for iface in interfaces:
            merged_iface = {
                **iface,
                "device_types": iface_info_by_path.get(iface["path"], {}).get(
                    "device_types",
                    iface.get("device_types", ["other"]),
                ),
            }
            raw_iface_id = _interface_id_for_config(merged_iface, used_interface_ids)
            iface_key = raw_iface_id
            duplicate_index = 2
            while iface_key in self.discovered_interfaces:
                iface_key = f"{raw_iface_id}_{duplicate_index}"
                duplicate_index += 1

            self.discovered_interfaces[iface_key] = {
                "id": raw_iface_id,
                "stable_path": iface["stable_path"],
                "config_path": str(iface.get("config_path") or iface["stable_path"]),
                "path": iface["path"],
                "name": iface["name"],
                "phys": str(iface.get("phys", "") or ""),
                **_interface_source_fields(iface),
                "device_type": iface_info_by_path.get(iface["path"], {}).get(
                    "device_type",
                    DeviceType.OTHER,
                ),
                "device_types": iface_info_by_path.get(iface["path"], {}).get(
                    "device_types",
                    ["other"],
                ),
                "capabilities": list(iface.get("capabilities", [])),
                "raw_capabilities": cast(
                    dict[int, list[object]],
                    iface.get("raw_capabilities") or {},
                ),
            }

    def _update_total(self, *args) -> None:
        total = self._calculate_total_buttons()
        self.total_label.set_label(f"Total buttons to capture: {total}")

    def _calculate_total_buttons(self) -> int:
        main = int(self.main_buttons_spin.get_value())
        extra = int(self.extra_buttons_spin.get_value())
        return main + extra

    def _build_button_list(self) -> list[dict]:
        buttons = []

        main_count = int(self.main_buttons_spin.get_value())
        main_names = [
            "Left Click",
            "Right Click",
            "Middle Click",
            "Button 4",
            "Button 5",
            "Button 6",
            "Button 7",
            "Button 8",
            "Button 9",
            "Button 10",
        ]
        main_ids = [
            "btn_left",
            "btn_right",
            "btn_middle",
            "btn_4",
            "btn_5",
            "btn_6",
            "btn_7",
            "btn_8",
            "btn_9",
            "btn_10",
        ]

        for i in range(main_count):
            buttons.append(
                {
                    "id": main_ids[i] if i < len(main_ids) else f"btn_{i + 1}",
                    "label": main_names[i] if i < len(main_names) else f"Button {i + 1}",
                    "type": "button",
                }
            )

        extra_count = int(self.extra_buttons_spin.get_value())
        for i in range(extra_count):
            buttons.append(
                {
                    "id": f"extra_{i + 1}",
                    "label": f"Extra Button {i + 1}",
                    "type": "button",
                }
            )

        return buttons

    def _on_next(self, button: Gtk.Button) -> None:
        visible_page = self.stack.get_visible_child_name()

        if visible_page == "select":
            selected_device = self.selected_device
            if selected_device is None:
                return
            if self._select_evdev_only:
                self._emit_selected_evdev_devices()
                return
            self._configure_mode = self._preferred_configure_mode()
            self.mode_combo.set_selected(self._configure_mode_values.index(self._configure_mode))
            self.describe_title.set_label(f"Configure {selected_device['name']}")
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

    def _update_capture_ui(self) -> None:
        if self.current_button_index >= len(self.button_definitions):
            self._finish_capture()
            return

        btn = self.button_definitions[self.current_button_index]
        self.capture_title.set_label(f"Capturing: {btn['label']}")

        if btn["type"] == "wheel":
            self.capture_instruction.set_label("Scroll UP on your device")
        elif btn["type"] == "wheel_h":
            direction = "RIGHT" if btn["evdev_value"] > 0 else "LEFT"
            self.capture_instruction.set_label(f"Scroll {direction} on your device")
        else:
            self.capture_instruction.set_label("Press this button on your device")

        _set_capture_status(
            self.capture_status,
            self.capture_status_dot,
            "Recording button presses..."
            if self._capturing
            else "Click 'Start Capture' then perform the action",
            recording=self._capturing,
        )

        progress = (
            self.current_button_index / len(self.button_definitions)
            if self.button_definitions
            else 0
        )
        self.capture_progress.set_fraction(progress)

    def _clear_captured_list(self) -> None:
        while row := self.captured_list.get_row_at_index(0):
            self.captured_list.remove(row)

    def _add_captured_button(self, btn_def: dict, evdev_code: str) -> None:
        row = Gtk.ListBoxRow()
        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row_box.set_margin_top(8)
        row_box.set_margin_bottom(8)
        row_box.set_margin_start(12)
        row_box.set_margin_end(12)

        label = Gtk.Label(label=btn_def["label"])
        row_box.append(label)

        evdev_label = Gtk.Label(label=f"→ {evdev_code}")
        evdev_label.add_css_class("dim-label")
        row_box.append(evdev_label)

        check = Gtk.Image(icon_name="emblem-ok-symbolic")
        row_box.append(check)

        row.set_child(row_box)
        self.captured_list.append(row)

    def _on_start_capture(self, button: Gtk.Button) -> None:
        if self._capturing:
            return

        if self.current_button_index >= len(self.button_definitions):
            return

        self._capturing = True
        self.capture_btn.set_label("Listening...")
        self.capture_btn.set_sensitive(False)
        _set_capture_status(
            self.capture_status,
            self.capture_status_dot,
            "Recording button presses...",
            recording=True,
        )
        self._capture_remaining_ids = [
            btn["id"] for btn in self.button_definitions[self.current_button_index :]
        ]
        capture_interfaces = [
            iface
            for iface in self.discovered_interfaces.values()
            if str(
                iface.get("config_path")
                or iface.get("stable_path")
                or iface.get("path")
                or ""
            )
        ]
        session_request_async(
            {
                "command": "begin_capture",
                "hardware_id": self._capture_hardware_id,
                "end_on_disconnect": True,
                "evdev_paths": [
                    str(
                        iface.get("config_path")
                        or iface.get("stable_path")
                        or iface.get("path")
                        or ""
                    )
                    for iface in capture_interfaces
                ],
                "evdev_interfaces": [
                    {
                        "id": str(iface.get("id", "") or ""),
                        "path": str(
                            iface.get("config_path")
                            or iface.get("stable_path")
                            or iface.get("path")
                            or ""
                        ),
                        "type": str(
                            primary_input_class(iface.get("device_types")).value
                        ),
                        "phys": str(iface.get("phys", "") or ""),
                        "capabilities": list(iface.get("capabilities", [])),
                    }
                    for iface in capture_interfaces
                ],
            },
            self._on_capture_begin_response,
        )

    def _on_capture_begin_response(self, result: dict | None) -> bool:
        if not self._capturing:
            return False

        if not result or result.get("status") != "ok":
            _set_capture_status(
                self.capture_status,
                self.capture_status_dot,
                (result or {}).get("message", "Capture failed: session unavailable")
            )
            self._stop_capture()
            return False

        warnings = result.get("warnings") or []
        if warnings:
            _set_capture_status(
                self.capture_status,
                self.capture_status_dot,
                f"Capture warnings: {', '.join(str(w) for w in warnings)}"
            )

        self._capture_poll_id = GLib.timeout_add(16, self._poll_capture)
        return False

    def _poll_capture(self) -> bool:
        if not self._capturing:
            return False

        if self._capture_poll_inflight:
            return True

        self._capture_poll_inflight = True
        session_request_async(
            {
                "command": "capture_read",
                "hardware_id": self._capture_hardware_id,
            },
            self._on_capture_poll_response,
        )
        return True

    def _on_capture_poll_response(self, result: dict | None) -> bool:
        self._capture_poll_inflight = False
        if not self._capturing:
            return False

        if not result:
            return False

        if result.get("status") != "ok":
            _set_capture_status(
                self.capture_status,
                self.capture_status_dot,
                result.get("message", "Capture failed"),
            )
            self._stop_capture()
            return False

        captured = result.get("captured")
        if not isinstance(captured, dict):
            return True

        if self.current_button_index >= len(self.button_definitions):
            self._finish_capture()
            return False

        btn_def = self.button_definitions[self.current_button_index]
        btn_def["evdev"] = captured.get("evdev", "unknown")
        btn_def["evdev_code"] = captured.get("code")
        btn_def["evdev_value"] = captured.get("value")
        btn_def["source"] = captured.get("source")
        btn_def["stable_path"] = captured.get("stable_path")

        evdev_display = str(captured.get("evdev", "unknown"))
        if captured.get("direction"):
            evdev_display = f"{evdev_display} ({captured.get('direction')})"
        if captured.get("source"):
            evdev_display = f"{evdev_display} [{captured.get('source')}]"

        self._add_captured_button(btn_def, evdev_display)
        self.current_button_index += 1
        remaining = max(0, len(self.button_definitions) - self.current_button_index)
        _set_capture_status(
            self.capture_status,
            self.capture_status_dot,
            f"Recording button presses... Captured {evdev_display} ({remaining} remaining)",
            recording=True,
        )

        if remaining == 0:
            self._finish_capture()
            return False

        self._update_capture_ui()
        return False

    def _stop_capture(self) -> None:
        self._capturing = False

        if self._capture_hardware_id:
            session_request_async(
                {
                    "command": "end_capture",
                    "hardware_id": self._capture_hardware_id,
                },
                lambda _response: False,
            )
            self._capture_hardware_id = None

        if self._capture_poll_id:
            GLib.source_remove(self._capture_poll_id)
            self._capture_poll_id = None
        self._capture_poll_inflight = False

        self.capture_btn.set_label("Start Capture")
        self.capture_btn.set_sensitive(True)
        self.capture_status_dot.set_visible(False)

    def _on_skip(self, button: Gtk.Button) -> None:
        self._stop_capture()

        if self.current_button_index >= len(self.button_definitions):
            return

        btn_def = self.button_definitions[self.current_button_index]
        btn_def["evdev"] = "unknown"
        btn_def["skipped"] = True
        self._add_captured_button(btn_def, "(skipped)")

        self.current_button_index += 1
        self._update_capture_ui()

    def _finish_capture(self) -> None:
        self._stop_capture()
        self.capture_title.set_label("Setup Complete!")
        self.capture_instruction.set_label("All buttons captured")
        _set_capture_status(
            self.capture_status,
            self.capture_status_dot,
            "Click Save to finish",
        )
        self.capture_progress.set_fraction(1.0)
        self.capture_btn.set_label("Save")
        self.capture_btn.set_sensitive(True)
        self.skip_btn.set_visible(False)

        self.capture_btn.disconnect_by_func(self._on_start_capture)
        self.capture_btn.connect("clicked", self._on_save)

    def _on_save(self, button: Gtk.Button) -> None:
        selected_device = self.selected_device
        if selected_device is None:
            return

        source_to_interface = {}
        for btn_def in self.button_definitions:
            source = btn_def.get("source")
            source_iface = self.discovered_interfaces.get(str(source or ""))
            if isinstance(source_iface, dict):
                config_path = (
                    source_iface.get("config_path")
                    or source_iface.get("stable_path")
                    or source_iface.get("path")
                    or btn_def.get("stable_path")
                )
            else:
                config_path = btn_def.get("stable_path")
            if source and config_path:
                source_to_interface[source] = config_path

        discovered_by_source = {
            str(iface.get("id", "") or ""): iface for iface in self.discovered_interfaces.values()
        }

        evdev_devices = []
        for iface_id, stable_path in source_to_interface.items():
            iface_info = discovered_by_source.get(iface_id, {})
            device_type = primary_input_class(
                iface_info.get("device_types"),
            )

            evdev_devices.append(
                EvdevDevice(
                    path=stable_path,
                    device_type=device_type,
                    id=iface_id,
                    phys=str(iface_info.get("phys", "") or "") or None,
                    capabilities=list(iface_info.get("capabilities", [])),
                )
            )

        buttons = []
        for btn_def in self.button_definitions:
            buttons.append(
                ButtonDefinition(
                    id=btn_def["id"],
                    label=btn_def["label"],
                    evdev=btn_def.get("evdev", "unknown"),
                    evdev_value=btn_def.get("evdev_value"),
                    source=btn_def.get("source"),
                    type=btn_def.get("type"),
                )
            )

        config = HardwareConfig(
            vendor_id=selected_device["vendor_id"],
            product_id=selected_device["product_id"],
            name=selected_device["name"],
            evdev_devices=evdev_devices,
            buttons=buttons,
            id=self._selected_config_id(selected_device),
        )

        self.hardware_manager.save_hardware(config)

        self.emit("device-created", config)
        self.close()

    def _save_custom_config(self) -> None:
        selected_device = self.selected_device
        if selected_device is None:
            return

        interfaces = list(self.discovered_interfaces.values())
        config = HardwareConfig(
            vendor_id=selected_device["vendor_id"],
            product_id=selected_device["product_id"],
            name=selected_device["name"],
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
            vendor_id=selected_device["vendor_id"],
            product_id=selected_device["product_id"],
            name=selected_device["name"],
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

        mouse_interfaces = self._interfaces_for_roles({"mouse", "pointstick"})
        interfaces = self._merge_interface_lists(
            mouse_interfaces,
            list(self.discovered_interfaces.values()),
        )
        primary_mouse_source = ""
        if mouse_interfaces:
            primary_mouse_source = str(mouse_interfaces[0].get("id", "") or "")

        config = HardwareConfig(
            vendor_id=selected_device["vendor_id"],
            product_id=selected_device["product_id"],
            name=selected_device["name"],
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
            vendor_id=selected_device["vendor_id"],
            product_id=selected_device["product_id"],
            name=selected_device["name"],
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

    def _gamepad_interfaces(self) -> list[dict]:
        return self._interfaces_for_roles({"gamepad"})

    def _interfaces_for_roles(self, roles: set[str]) -> list[dict]:
        interfaces = [
            iface
            for iface in self.discovered_interfaces.values()
            if any(self._interface_has_role(iface, role) for role in roles)
        ]
        return interfaces or list(self.discovered_interfaces.values())

    def _merge_interface_lists(self, *interface_lists: list[dict]) -> list[dict]:
        merged: list[dict] = []
        seen_keys: set[tuple[str, str]] = set()
        for interface_list in interface_lists:
            for iface in interface_list:
                iface_id = str(iface.get("id", "") or "")
                config_path = str(
                    iface.get("config_path", "") or iface.get("stable_path", "") or ""
                )
                key = (iface_id, config_path)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                merged.append(iface)
        return merged

    def _interfaces_have_capability(self, interfaces: list[dict], capability: str) -> bool:
        capability_l = capability.strip().lower()
        for iface in interfaces:
            capabilities = {str(name).strip().lower() for name in iface.get("capabilities", [])}
            if capability_l in capabilities:
                return True
            raw_capabilities = iface.get("raw_capabilities") or {}
            if not isinstance(raw_capabilities, dict):
                continue
            for code in raw_capabilities.get(evdev.ecodes.EV_REL, []):
                name = capability_name(evdev.ecodes.EV_REL, code)
                if name == capability_l:
                    return True
        return False

    def _build_evdev_devices(self, interfaces: list[dict]) -> list[EvdevDevice]:
        evdev_devices = []
        for iface in interfaces:
            config_path = str(iface.get("config_path", "") or iface.get("stable_path", "") or "")
            iface_id = str(iface.get("id", "") or "")
            if not config_path or not iface_id:
                continue
            evdev_devices.append(
                EvdevDevice(
                    path=config_path,
                    device_type=primary_input_class(iface.get("device_types")),
                    id=iface_id,
                    phys=str(iface.get("phys", "") or "") or None,
                    capabilities=list(iface.get("capabilities", [])),
                )
            )
        return evdev_devices

    def _build_standard_mouse_buttons(
        self,
        source_id: str,
        *,
        include_horizontal: bool = False,
    ) -> list[ButtonDefinition]:
        buttons = [
            ButtonDefinition(
                id="btn_left",
                label="Left Click",
                evdev="btn_left",
                source=source_id or None,
                type="button",
                zone="left",
            ),
            ButtonDefinition(
                id="btn_right",
                label="Right Click",
                evdev="btn_right",
                source=source_id or None,
                type="button",
                zone="right",
            ),
            ButtonDefinition(
                id="btn_middle",
                label="Middle Click",
                evdev="btn_middle",
                source=source_id or None,
                type="button",
                zone="wheel",
            ),
            ButtonDefinition(
                id="btn_back",
                label="Back",
                evdev="btn_side",
                source=source_id or None,
                type="button",
                zone="thumb",
            ),
            ButtonDefinition(
                id="btn_forward",
                label="Forward",
                evdev="btn_extra",
                source=source_id or None,
                type="button",
                zone="thumb",
            ),
        ]
        buttons.extend(self._standard_wheel_buttons(source_id, include_horizontal))
        return buttons

    def _standard_wheel_buttons(
        self,
        source_id: str,
        include_horizontal: bool,
    ) -> list[ButtonDefinition]:
        specs = [("rel_wheel", 1), ("rel_wheel", -1)]
        if include_horizontal:
            specs.extend([("rel_hwheel", -1), ("rel_hwheel", 1)])

        buttons: list[ButtonDefinition] = []
        for evdev_name, value in specs:
            button_id = wheel_button_id(evdev_name, value)
            label = wheel_label(evdev_name, value)
            if button_id is None or label is None or not is_low_res_wheel_evdev(evdev_name):
                continue
            code = getattr(evdev.ecodes, evdev_name.upper(), None)
            buttons.append(
                ButtonDefinition(
                    id=button_id,
                    label=label,
                    evdev=evdev_name,
                    evdev_code=int(code) if code is not None else None,
                    evdev_value=value,
                    source=source_id or None,
                    type="wheel",
                    zone="wheel",
                )
            )
        return buttons

    def _build_gamepad_buttons(self, interfaces: list[dict]) -> list[ButtonDefinition]:
        button_specs: dict[str, tuple[int, str | None]] = {}

        for iface in interfaces:
            raw_capabilities = iface.get("raw_capabilities") or {}
            if not isinstance(raw_capabilities, dict):
                continue

            source_id = str(iface.get("id", "") or "")
            for code in raw_capabilities.get(evdev.ecodes.EV_KEY, []):
                code_int = int(code[0] if isinstance(code, tuple) else code)
                evdev_name = capability_name(evdev.ecodes.EV_KEY, code_int)
                if not evdev_name:
                    continue
                label = gamepad_button_label(evdev_name)
                canonical = canonical_gamepad_button_name(evdev_name)
                if canonical in button_specs or label is None:
                    continue
                button_specs[canonical] = (code_int, source_id or None)

        buttons: list[ButtonDefinition] = []
        for canonical in ordered_gamepad_button_names(button_specs):
            code_int, source_id = button_specs[canonical]
            label = gamepad_button_label(canonical)
            if label is None:
                continue
            buttons.append(
                ButtonDefinition(
                    id=canonical,
                    label=label,
                    evdev=canonical,
                    evdev_code=code_int,
                    source=source_id,
                    type="gamepad",
                )
            )

        return buttons

    def _build_gamepad_analog_inputs(self, interfaces: list[dict]) -> list[AnalogInputDefinition]:
        axis_specs = {
            "left_stick": (
                "Left Stick",
                "stick",
                ((evdev.ecodes.ABS_X, "x"), (evdev.ecodes.ABS_Y, "y")),
            ),
            "right_stick": (
                "Right Stick",
                "stick",
                ((evdev.ecodes.ABS_RX, "x"), (evdev.ecodes.ABS_RY, "y")),
            ),
            "left_trigger": (
                "Left Trigger",
                "axis",
                ((evdev.ecodes.ABS_Z, "x"),),
            ),
            "right_trigger": (
                "Right Trigger",
                "axis",
                ((evdev.ecodes.ABS_RZ, "x"),),
            ),
        }
        discovered: dict[str, dict[int, tuple[str, str]]] = {}

        for iface in interfaces:
            raw_capabilities = iface.get("raw_capabilities") or {}
            if not isinstance(raw_capabilities, dict):
                continue
            source_id = str(iface.get("id", "") or "")
            abs_codes = {
                int(code[0] if isinstance(code, tuple) else code)
                for code in raw_capabilities.get(evdev.ecodes.EV_ABS, [])
            }
            for analog_id, (_label, _input_type, axes) in axis_specs.items():
                codes = tuple(code for code, _role in axes)
                if all(code in abs_codes for code in codes):
                    discovered[analog_id] = {code: (role, source_id) for code, role in axes}

        analog_inputs: list[AnalogInputDefinition] = []
        for analog_id, (label, input_type, axes) in axis_specs.items():
            axis_data = discovered.get(analog_id)
            if axis_data is None:
                continue
            codes = tuple(code for code, _role in axes)
            source_id = next(
                (axis_data[code][1] for code in codes if axis_data[code][1]),
                None,
            )
            analog_inputs.append(
                AnalogInputDefinition(
                    id=analog_id,
                    label=label,
                    type=input_type,
                    source=source_id,
                    axes=[
                        AnalogAxisDefinition(
                            role=axis_data[code][0],
                            evdev=capability_name(evdev.ecodes.EV_ABS, code) or str(code),
                            evdev_code=code,
                        )
                        for code in codes
                    ],
                )
            )
        return analog_inputs

    def _save_gamepad_config(self) -> None:
        selected_device = self.selected_device
        if selected_device is None:
            return

        gamepad_interfaces = self._gamepad_interfaces()
        interfaces = self._merge_interface_lists(
            gamepad_interfaces,
            list(self.discovered_interfaces.values()),
        )

        config = HardwareConfig(
            vendor_id=selected_device["vendor_id"],
            product_id=selected_device["product_id"],
            name=selected_device["name"],
            evdev_devices=self._build_evdev_devices(interfaces),
            buttons=self._build_gamepad_buttons(gamepad_interfaces),
            analog_inputs=self._build_gamepad_analog_inputs(gamepad_interfaces),
            id=self._selected_config_id(selected_device),
        )

        self.hardware_manager.save_hardware(config)
        self.emit("device-created", config)
        self.close()

    def _build_standard_keyboard_buttons(self, source_id: str) -> list[ButtonDefinition]:
        buttons: list[ButtonDefinition] = []
        standard_keys = [
            "KEY_ESC",
            "KEY_GRAVE",
            "KEY_1",
            "KEY_2",
            "KEY_3",
            "KEY_4",
            "KEY_5",
            "KEY_6",
            "KEY_7",
            "KEY_8",
            "KEY_9",
            "KEY_0",
            "KEY_MINUS",
            "KEY_EQUAL",
            "KEY_BACKSPACE",
            "KEY_TAB",
            "KEY_Q",
            "KEY_W",
            "KEY_E",
            "KEY_R",
            "KEY_T",
            "KEY_Y",
            "KEY_U",
            "KEY_I",
            "KEY_O",
            "KEY_P",
            "KEY_LEFTBRACE",
            "KEY_RIGHTBRACE",
            "KEY_BACKSLASH",
            "KEY_CAPSLOCK",
            "KEY_A",
            "KEY_S",
            "KEY_D",
            "KEY_F",
            "KEY_G",
            "KEY_H",
            "KEY_J",
            "KEY_K",
            "KEY_L",
            "KEY_SEMICOLON",
            "KEY_APOSTROPHE",
            "KEY_ENTER",
            "KEY_LEFTSHIFT",
            "KEY_Z",
            "KEY_X",
            "KEY_C",
            "KEY_V",
            "KEY_B",
            "KEY_N",
            "KEY_M",
            "KEY_COMMA",
            "KEY_DOT",
            "KEY_SLASH",
            "KEY_RIGHTSHIFT",
            "KEY_LEFTCTRL",
            "KEY_LEFTALT",
            "KEY_LEFTMETA",
            "KEY_SPACE",
            "KEY_RIGHTALT",
            "KEY_RIGHTCTRL",
            "KEY_RIGHTMETA",
            "KEY_SYSRQ",
            "KEY_SCROLLLOCK",
            "KEY_PAUSE",
            "KEY_INSERT",
            "KEY_HOME",
            "KEY_PAGEUP",
            "KEY_DELETE",
            "KEY_END",
            "KEY_PAGEDOWN",
            "KEY_UP",
            "KEY_LEFT",
            "KEY_DOWN",
            "KEY_RIGHT",
            "KEY_F1",
            "KEY_F2",
            "KEY_F3",
            "KEY_F4",
            "KEY_F5",
            "KEY_F6",
            "KEY_F7",
            "KEY_F8",
            "KEY_F9",
            "KEY_F10",
            "KEY_F11",
            "KEY_F12",
            "KEY_NUMLOCK",
            "KEY_KPSLASH",
            "KEY_KPASTERISK",
            "KEY_KPMINUS",
            "KEY_KP7",
            "KEY_KP8",
            "KEY_KP9",
            "KEY_KPPLUS",
            "KEY_KP4",
            "KEY_KP5",
            "KEY_KP6",
            "KEY_KP1",
            "KEY_KP2",
            "KEY_KP3",
            "KEY_KPENTER",
            "KEY_KP0",
            "KEY_KPDOT",
        ]

        for name in standard_keys:
            if name not in evdev.ecodes.ecodes:
                continue
            evdev_name = name.lower()
            buttons.append(
                ButtonDefinition(
                    id=evdev_name,
                    label=self._keyboard_label_from_evdev(name),
                    evdev=evdev_name,
                    source=source_id or None,
                    type="key",
                )
            )

        return buttons

    def _keyboard_label_from_evdev(self, key_name: str) -> str:
        token = key_name[4:] if key_name.startswith("KEY_") else key_name
        token = token.replace("LEFT", "Left ").replace("RIGHT", "Right ")
        token = token.replace("CTRL", "Ctrl").replace("ALT", "Alt")
        token = token.replace("META", "Meta").replace("SHIFT", "Shift")
        token = token.replace("PAGEUP", "Page Up").replace("PAGEDOWN", "Page Down")
        token = token.replace("NUMLOCK", "Num Lock")
        return token.replace("_", " ").strip().title()

    def do_close_request(self) -> bool:
        return False
