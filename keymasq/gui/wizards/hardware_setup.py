import os
import re
import subprocess
from typing import Any, cast

import evdev
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, GObject, Gtk  # pyright: ignore[reportAttributeAccessIssue]

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
    is_low_res_wheel_evdev,
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
from keymasq.session.hardware import HardwareManager

DetectedDevice = dict[str, Any]
DetectedInterface = dict[str, Any]
DetectedButton = dict[str, Any]


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


def _looks_like_by_id_path(stable_path: str) -> bool:
    return "/by-id/" in str(stable_path or "")


def _logical_hardware_identity_key(
    *,
    model_id: str,
    device_types: list[str],
    stable_path: str,
    phys: str = "",
) -> str:
    normalized_types = normalize_input_classes(device_types)
    if "gamepad" in normalized_types and _looks_like_by_id_path(stable_path):
        return f"path:{stable_path}"
    if _looks_like_by_id_path(stable_path):
        return f"by-id:{_by_id_device_stem(stable_path)}"
    phys_base = _strip_input_suffix(phys)
    if phys_base and not _is_usb_phys(phys_base):
        return f"phys:{phys_base}"
    return f"model:{model_id}"


class HardwareSetupDialog(Adw.Window):
    __gsignals__ = {
        "device-created": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, parent, hardware_manager: HardwareManager) -> None:
        super().__init__(
            title="Add New Device",
            transient_for=parent,
            modal=True,
            default_width=500,
            default_height=450,
        )

        self.hardware_manager = hardware_manager
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

        self._setup_ui()
        self._detect_devices()

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

        self.next_btn = Gtk.Button(label="Next")
        self.next_btn.connect("clicked", self._on_next)
        self.next_btn.add_css_class("suggested-action")
        self.next_btn.set_sensitive(False)
        header.pack_end(self.next_btn)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.stack)
        self.set_content(toolbar)

    def _setup_page_select(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)

        title = Gtk.Label(label="Select Your Device")
        title.add_css_class("title-1")
        box.append(title)

        subtitle = Gtk.Label(label="Choose the device you want to configure")
        subtitle.add_css_class("dim-label")
        box.append(subtitle)

        self.device_list = Gtk.ListBox()
        self.device_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.device_list.connect("row-selected", self._on_device_selected)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_child(self.device_list)
        box.append(scrolled)

        refresh_btn = Gtk.Button(label="Refresh")
        refresh_btn.connect("clicked", self._on_refresh_clicked)
        box.append(refresh_btn)

        self.stack.add_titled(box, "select", "Select Device")

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

        self.keyboard_mode_info = Gtk.Label(
            label="Keyboard template creates a full standard keyboard hardware profile."
        )
        self.keyboard_mode_info.add_css_class("dim-label")
        self.keyboard_mode_info.set_wrap(True)
        self.keyboard_mode_info.set_halign(Gtk.Align.START)
        box.append(self.keyboard_mode_info)

        self.mouse_mode_info = Gtk.Label(
            label=(
                "Mouse template creates standard mouse buttons and scroll wheel directions."
            )
        )
        self.mouse_mode_info.add_css_class("dim-label")
        self.mouse_mode_info.set_wrap(True)
        self.mouse_mode_info.set_halign(Gtk.Align.START)
        box.append(self.mouse_mode_info)

        self.mouse_keyboard_mode_info = Gtk.Label(
            label=(
                "Mouse + Keyboard template creates a full standard keyboard profile plus a "
                "standard mouse with scroll wheel directions."
            )
        )
        self.mouse_keyboard_mode_info.add_css_class("dim-label")
        self.mouse_keyboard_mode_info.set_wrap(True)
        self.mouse_keyboard_mode_info.set_halign(Gtk.Align.START)
        box.append(self.mouse_keyboard_mode_info)

        self.gamepad_mode_info = Gtk.Label(
            label=(
                "Gamepad profile uses detected capabilities and saves the standard digital "
                "buttons it reports. Analog axes still passthrough and are not remappable yet."
            )
        )
        self.gamepad_mode_info.add_css_class("dim-label")
        self.gamepad_mode_info.set_wrap(True)
        self.gamepad_mode_info.set_halign(Gtk.Align.START)
        box.append(self.gamepad_mode_info)

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
        self.capture_status.set_margin_top(12)
        box.append(self.capture_status)

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

            expander = Gtk.Expander(label="Evdev devices")
            expander.set_expanded(False)

            iface_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            iface_box.set_margin_top(4)
            iface_box.set_margin_start(12)

            for iface in interfaces:
                iface_name = Gtk.Label(label=f"- {iface['name']}")
                iface_name.add_css_class("caption")
                iface_name.set_halign(Gtk.Align.START)
                iface_box.append(iface_name)

            expander.set_child(iface_box)
            row_box.append(expander)

            row.set_child(row_box)
            row.hardware_id = hardware_id
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

    def _detect_devices_locally(
        self,
        lsusb_map: dict[str, dict[str, str]],
        detected_devices: dict[str, dict],
    ) -> None:
        used_hardware_ids = self._configured_hardware_ids()
        configured_identity_keys = self._configured_identity_keys()
        pending_identity_hardware_ids: dict[str, str] = {}
        has_config_inventory = callable(getattr(self.hardware_manager, "list_hardware", None))

        def local_sort_key(path: str) -> str:
            try:
                return resolve_stable_path(path)
            except Exception:
                return path

        for path in sorted(evdev.list_devices(), key=local_sort_key):
            try:
                device = evdev.InputDevice(path)
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
                phys = str(getattr(device, "phys", "") or "")
                identity_key = _logical_hardware_identity_key(
                    model_id=vid_pid,
                    device_types=device_types,
                    stable_path=stable_path,
                    phys=phys,
                )
                if identity_key in configured_identity_keys or (
                    not has_config_inventory and self._hardware_config_exists(vid_pid)
                ):
                    continue
                hardware_id = pending_identity_hardware_ids.get(identity_key)
                if hardware_id is None:
                    hardware_id = self._allocate_hardware_id(vid_pid, used_hardware_ids)
                    used_hardware_ids.add(hardware_id)
                    pending_identity_hardware_ids[identity_key] = hardware_id
                device_type = primary_input_class(device_types)
                lsusb_entry = lsusb_map.get(vid_pid)
                display_name = (
                    lsusb_entry["name"] if lsusb_entry and lsusb_entry["name"] else device.name
                )
                human_name = (
                    lsusb_entry["name"] if lsusb_entry and lsusb_entry["name"] else device.name
                )

                if hardware_id not in detected_devices:
                    detected_devices[hardware_id] = {
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
                            }
                        ],
                    }
                else:
                    detected_devices[hardware_id]["paths"].append(path)
                    detected_devices[hardware_id]["interfaces"].append(
                        {
                            "path": path,
                            "stable_path": stable_path,
                            "name": device.name,
                            "phys": phys,
                            "device_type": device_type,
                            "device_types": device_types,
                        }
                    )

            except Exception:
                pass

    def _detect_devices_via_session(self, detected_devices: dict[str, dict]) -> bool:
        result = session_request({"command": "list_devices_for_recording"}, timeout=3.0) or {}
        if result.get("status") != "ok":
            return False

        used_hardware_ids = self._configured_hardware_ids()
        configured_identity_keys = self._configured_identity_keys()
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
            phys = str(dev.get("phys", "") or "")
            identity_key = _logical_hardware_identity_key(
                model_id=vid_pid,
                device_types=device_types,
                stable_path=stable_path,
                phys=phys,
            )
            if identity_key in configured_identity_keys or (
                not has_config_inventory and self._hardware_config_exists(vid_pid)
            ):
                continue
            hardware_id = pending_identity_hardware_ids.get(identity_key)
            if hardware_id is None:
                hardware_id = self._allocate_hardware_id(vid_pid, used_hardware_ids)
                used_hardware_ids.add(hardware_id)
                pending_identity_hardware_ids[identity_key] = hardware_id

            if hardware_id not in detected_devices:
                detected_devices[hardware_id] = {
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
                        }
                    ]
                    if path
                    else [],
                }
            else:
                if path:
                    detected_devices[hardware_id]["paths"].append(path)
                    detected_devices[hardware_id]["interfaces"].append(
                        {
                            "path": path,
                            "stable_path": stable_path,
                            "name": name,
                            "phys": phys,
                            "device_type": dtype,
                            "device_types": device_types,
                        }
                    )

        return bool(detected_devices)

    def _should_skip_detected_device(self, device: evdev.InputDevice) -> bool:
        return self._should_skip_detected_device_info(
            {
                "name": device.name,
                "phys": getattr(device, "phys", None),
            }
        )

    def _should_skip_detected_device_info(self, device_info: dict[str, Any]) -> bool:
        name = str(device_info.get("name", "") or "").strip().lower()
        phys = str(device_info.get("phys", "") or "").strip().lower()

        if "keymasq" in name:
            return True

        if phys == "py-evdev-uinput":
            return True

        return False

    def _should_include_detected_interface(self, device_types: list[str]) -> bool:
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
                return set()

        return set()

    def _configured_identity_keys(self) -> set[str]:
        list_hardware = getattr(self.hardware_manager, "list_hardware", None)
        if not callable(list_hardware):
            return set()
        try:
            configs = cast(list[object], list_hardware())
        except Exception:
            return set()

        keys: set[str] = set()
        for config in configs:
            model_id = str(getattr(config, "model_id", "") or "")
            if not model_id:
                continue
            for device in getattr(config, "evdev_devices", []):
                path = str(getattr(device, "path", "") or "")
                if not path:
                    continue
                device_type = getattr(getattr(device, "device_type", None), "value", None)
                device_types = [str(device_type or "other")]
                keys.add(
                    _logical_hardware_identity_key(
                        model_id=model_id,
                        device_types=device_types,
                        stable_path=self._configured_device_stable_path(path),
                        phys=self._configured_device_phys(device),
                    )
                )
        return keys

    def _configured_device_stable_path(self, path: str) -> str:
        path = str(path or "")
        if not path:
            return ""
        candidates = [path]
        try:
            real_path = os.path.realpath(path)
        except Exception:
            real_path = ""
        if real_path and real_path != path:
            candidates.append(real_path)

        for candidate in candidates:
            try:
                stable_path = resolve_stable_path(candidate)
            except Exception:
                continue
            if _looks_like_by_id_path(stable_path):
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
        except Exception:
            return ""
        try:
            return str(getattr(input_device, "phys", "") or "")
        finally:
            try:
                input_device.close()
            except Exception:
                pass

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
        return hardware_id if hardware_id != model_id else None

    def _on_device_selected(self, list_box, row) -> None:
        if row:
            self.selected_device = self.detected_devices[row.hardware_id]
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
            self.next_btn.set_sensitive(True)

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

        if not self._configure_mode_values:
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
        self.keyboard_mode_info.set_visible(is_keyboard)
        self.mouse_mode_info.set_visible(is_mouse)
        self.mouse_keyboard_mode_info.set_visible(is_mouse_keyboard)
        self.gamepad_mode_info.set_visible(is_gamepad)
        if is_gamepad:
            self.describe_subtitle.set_label("Review the detected controller controls")
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
        except Exception:
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
            capability_names, raw_capabilities = self._read_interface_capabilities(raw_path)
            interfaces.append(
                {
                    "path": raw_path,
                    "stable_path": stable_path,
                    "id": get_interface_id(stable_path),
                    "name": str(iface.get("name", "") or raw_path),
                    "phys": str(iface.get("phys", "") or ""),
                    "capabilities": capability_names,
                    "raw_capabilities": raw_capabilities,
                }
            )

        if not interfaces:
            interfaces = find_all_interfaces(vid, pid)
        iface_info_by_path = {
            iface.get("path"): {
                "device_type": iface.get("device_type", DeviceType.OTHER),
                "device_types": self._interface_device_types(iface),
            }
            for iface in self.selected_device.get("interfaces", [])
        }
        for iface in interfaces:
            raw_iface_id = str(iface.get("id", "") or "default")
            iface_key = raw_iface_id
            duplicate_index = 2
            while iface_key in self.discovered_interfaces:
                iface_key = f"{raw_iface_id}_{duplicate_index}"
                duplicate_index += 1

            self.discovered_interfaces[iface_key] = {
                "id": raw_iface_id,
                "stable_path": iface["stable_path"],
                "path": iface["path"],
                "name": iface["name"],
                "phys": str(iface.get("phys", "") or ""),
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
            self._save_mouse_config()

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

        self.capture_status.set_label("Click 'Start Capture' then perform the action")

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
        self.capture_status.set_label("Waiting for input...")
        self._capture_remaining_ids = [
            btn["id"] for btn in self.button_definitions[self.current_button_index :]
        ]
        session_request_async(
            {
                "command": "begin_capture",
                "hardware_id": self._capture_hardware_id,
                "evdev_paths": [
                    str(iface.get("stable_path") or iface.get("path") or "")
                    for iface in self.discovered_interfaces.values()
                    if str(iface.get("stable_path") or iface.get("path") or "")
                ],
            },
            self._on_capture_begin_response,
        )

    def _on_capture_begin_response(self, result: dict | None) -> bool:
        if not self._capturing:
            return False

        if not result or result.get("status") != "ok":
            self.capture_status.set_label(
                (result or {}).get("message", "Capture failed: session unavailable")
            )
            self._stop_capture()
            return False

        warnings = result.get("warnings") or []
        if warnings:
            self.capture_status.set_label(
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
            self.capture_status.set_label(result.get("message", "Capture failed"))
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
        self.capture_status.set_label(f"Captured {evdev_display} ({remaining} remaining)")

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
        self.capture_status.set_label("Click Save to finish")
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
            stable_path = btn_def.get("stable_path")
            if source and stable_path:
                source_to_interface[source] = stable_path

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

    def _save_keyboard_config(self) -> None:
        selected_device = self.selected_device
        if selected_device is None:
            return

        keyboard_interfaces = self._interfaces_for_roles({"keyboard"})

        primary_keyboard_source = ""
        if keyboard_interfaces:
            primary_keyboard_source = str(keyboard_interfaces[0].get("id", "") or "")

        evdev_devices = self._build_evdev_devices(keyboard_interfaces)

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
        primary_mouse_source = ""
        if mouse_interfaces:
            primary_mouse_source = str(mouse_interfaces[0].get("id", "") or "")

        config = HardwareConfig(
            vendor_id=selected_device["vendor_id"],
            product_id=selected_device["product_id"],
            name=selected_device["name"],
            evdev_devices=self._build_evdev_devices(mouse_interfaces),
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

        interfaces = self._merge_interface_lists(mouse_interfaces, keyboard_interfaces)
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
        except Exception:
            return ([], {})

        try:
            raw_capabilities = cast(dict[int, list[object]], device.capabilities())
        except Exception:
            raw_capabilities = {}
        finally:
            try:
                device.close()
            except Exception:
                pass

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
                stable_path = str(iface.get("stable_path", "") or "")
                key = (iface_id, stable_path)
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
            stable_path = str(iface.get("stable_path", "") or "")
            iface_id = str(iface.get("id", "") or "")
            if not stable_path or not iface_id:
                continue
            evdev_devices.append(
                EvdevDevice(
                    path=stable_path,
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

        evdev_devices = []
        for iface in gamepad_interfaces:
            stable_path = str(iface.get("stable_path", "") or "")
            iface_id = str(iface.get("id", "") or "")
            if not stable_path or not iface_id:
                continue
            dev_type = primary_input_class(iface.get("device_types"))
            evdev_devices.append(
                EvdevDevice(
                    path=stable_path,
                    device_type=dev_type,
                    id=iface_id,
                    phys=str(iface.get("phys", "") or "") or None,
                    capabilities=list(iface.get("capabilities", [])),
                )
            )

        config = HardwareConfig(
            vendor_id=selected_device["vendor_id"],
            product_id=selected_device["product_id"],
            name=selected_device["name"],
            evdev_devices=evdev_devices,
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
