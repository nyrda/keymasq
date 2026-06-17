import logging
import re
from collections.abc import Callable, Sequence
from typing import Literal, cast

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq import __version__
from keymasq.common.devices import is_keymasq_device_path
from keymasq.common.models import DeviceType, EvdevDevice, HardwareConfig
from keymasq.gui.widgets.docs_links import docs_page_url
from keymasq.session.hardware import HardwareManager

log = logging.getLogger("keymasq.gui.widgets.device_tab.hardware_settings_dialog")

DetectionMethod = Literal["stable", "product"]
EvdevDevicesAddedCallback = Callable[[list[EvdevDevice]], int]
EvdevDeviceDeleteCallback = Callable[[EvdevDevice, bool], bool]
EvdevDetectionMethodCallback = Callable[[EvdevDevice, DetectionMethod], tuple[bool, str]]
EvdevStableDetectionStatusCallback = Callable[[EvdevDevice], tuple[bool, str]]
RenameDeviceCallback = Callable[[Callable[[], None]], None]


def _hardware_docs_url() -> str:
    return docs_page_url("HARDWARE", version=__version__)


def append_unique_evdev_devices(
    hardware_config: HardwareConfig,
    evdev_devices: Sequence[EvdevDevice],
) -> int:
    used_ids = {
        _normalize_interface_id(str(device.id or ""))
        for device in hardware_config.evdev_devices
        if str(device.id or "").strip()
    }
    signatures = {_evdev_device_signature(device) for device in hardware_config.evdev_devices}

    added = 0
    for device in evdev_devices:
        path = str(device.path or "").strip()
        if not path:
            continue
        signature = _evdev_device_signature(device)
        if signature in signatures:
            continue
        source_id = _dedupe_interface_id(str(device.id or ""), used_ids)
        hardware_config.evdev_devices.append(
            EvdevDevice(
                path=path,
                device_type=_evdev_device_type(device),
                id=source_id,
                phys=str(device.phys or "") or None,
                capabilities=[str(item) for item in device.capabilities],
            )
        )
        signatures.add(signature)
        added += 1
    return added


class HardwareSettingsDialog(Adw.Dialog):
    def __init__(
        self,
        parent: Gtk.Window | None,
        hardware_config: HardwareConfig,
        hardware_manager: HardwareManager,
        on_add_devices: EvdevDevicesAddedCallback,
        on_delete_device: Callable[[], None],
        on_delete_evdev_device: EvdevDeviceDeleteCallback,
        on_set_detection_method: EvdevDetectionMethodCallback,
        on_stable_detection_status: EvdevStableDetectionStatusCallback,
        on_rename_device: RenameDeviceCallback,
        *,
        can_delete_profile_mappings: bool,
    ) -> None:
        super().__init__(
            title="Hardware Settings",
            content_width=620,
            content_height=520,
        )
        if hasattr(self, "set_modal"):
            self.set_modal(True)

        self._parent = parent
        self._hardware_config = hardware_config
        self._hardware_manager = hardware_manager
        self._on_add_devices = on_add_devices
        self._on_delete_device = on_delete_device
        self._on_delete_evdev_device = on_delete_evdev_device
        self._on_set_detection_method = on_set_detection_method
        self._on_stable_detection_status = on_stable_detection_status
        self._on_rename_device = on_rename_device
        self._can_delete_profile_mappings = can_delete_profile_mappings
        self._interface_rows: list[Adw.ActionRow] = []
        self._identity_row: Adw.ActionRow | None = None
        self._updating_detection_method = False

        self._setup_ui()

    def _setup_ui(self) -> None:
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_propagate_natural_height(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        identity_group = Adw.PreferencesGroup(title="Hardware")
        identity_row = Adw.ActionRow(
            title=self._hardware_config.name,
            subtitle=self._hardware_config.hardware_id,
        )
        identity_row.set_tooltip_text("Rename hardware")
        identity_row.set_activatable(True)
        identity_row.connect("activated", self._on_identity_row_activated)
        identity_right_click = Gtk.GestureClick()
        identity_right_click.set_button(3)
        identity_right_click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        identity_right_click.connect("pressed", self._on_identity_row_right_clicked)
        identity_row.add_controller(identity_right_click)
        self._identity_row = identity_row
        identity_group.add(identity_row)
        box.append(identity_group)

        self._interfaces_group = Adw.PreferencesGroup(title="Attached Event Devices")
        box.append(self._interfaces_group)
        self._refresh_interface_rows()

        self._status_label = Gtk.Label()
        self._status_label.add_css_class("dim-label")
        self._status_label.set_halign(Gtk.Align.START)
        self._status_label.set_wrap(True)
        box.append(self._status_label)

        scrolled.set_child(box)
        content.append(scrolled)
        content.append(Gtk.Separator())

        footer = Gtk.CenterBox(orientation=Gtk.Orientation.HORIZONTAL)
        footer.set_margin_top(6)
        footer.set_margin_bottom(6)
        footer.set_margin_start(12)
        footer.set_margin_end(12)

        start_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        docs_btn = Gtk.Button(label="?")
        docs_btn.add_css_class("flat")
        docs_btn.add_css_class("actions-docs-button")
        docs_btn.set_tooltip_text("Open Hardware documentation")
        docs_btn.connect("clicked", self._on_hardware_docs_clicked)
        start_box.append(docs_btn)

        delete_btn = Gtk.Button(label="Delete Hardware")
        delete_btn.add_css_class("destructive-action")
        delete_btn.connect("clicked", self._on_delete_hardware_clicked)
        start_box.append(delete_btn)

        rename_btn = Gtk.Button(label="Rename")
        rename_btn.connect("clicked", self._on_rename_clicked)
        start_box.append(rename_btn)
        footer.set_start_widget(start_box)

        end_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        add_btn = Gtk.Button()
        add_btn.set_child(
            Adw.ButtonContent(icon_name="list-add-symbolic", label="Add Event Device")
        )
        add_btn.set_tooltip_text("Attach another raw evdev event device to this hardware ID")
        add_btn.connect("clicked", self._on_add_event_device_clicked)
        end_box.append(add_btn)

        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", self._on_close_clicked)
        end_box.append(close_btn)
        footer.set_end_widget(end_box)
        content.append(footer)

        self.set_child(content)

    def _refresh_interface_rows(self) -> None:
        for row in self._interface_rows:
            self._interfaces_group.remove(row)
        self._interface_rows = []

        if not self._hardware_config.evdev_devices:
            row = Adw.ActionRow(
                title="No event devices attached",
                subtitle="Add an event device to make this hardware ID match live input.",
            )
            row.set_sensitive(False)
            self._interfaces_group.add(row)
            self._interface_rows.append(row)
            return

        for device in self._hardware_config.evdev_devices:
            row = Adw.ActionRow(
                title=_interface_row_title(device),
                subtitle=str(device.path or ""),
            )
            if device.phys:
                row.add_prefix(Gtk.Label(label="phys"))
                row.set_subtitle(f"{device.path}\n{device.phys}")
            type_label = Gtk.Label(label=_evdev_device_type(device).value)
            type_label.add_css_class("dim-label")
            type_label.add_css_class("caption")
            row.add_suffix(type_label)
            row.add_suffix(self._detection_method_toggle_box(device))
            delete_btn = Gtk.Button(icon_name="user-trash-symbolic")
            delete_btn.add_css_class("flat")
            delete_btn.add_css_class("destructive-action")
            delete_btn.set_tooltip_text("Remove event device")
            delete_btn.connect("clicked", self._on_delete_evdev_clicked, device)
            row.add_suffix(delete_btn)
            self._interfaces_group.add(row)
            self._interface_rows.append(row)

    def _detection_method_toggle_box(self, device: EvdevDevice) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        box.add_css_class("linked")
        box.set_valign(Gtk.Align.CENTER)
        box.set_tooltip_text("Choose how Keymasq matches this event device.")

        detection_method = _detection_method_for_device(device)
        stable_available, stable_tooltip = self._on_stable_detection_status(device)
        stable_btn = Gtk.ToggleButton(label="Stable")
        stable_btn.add_css_class("flat")
        stable_btn.set_valign(Gtk.Align.CENTER)
        stable_btn.set_tooltip_text(stable_tooltip)

        product_btn = Gtk.ToggleButton(label="Product")
        product_btn.add_css_class("flat")
        product_btn.set_valign(Gtk.Align.CENTER)
        product_btn.set_tooltip_text(
            "Match this event device by vendor/product ID and capabilities."
        )
        product_btn.set_group(stable_btn)

        if detection_method == "product":
            product_btn.set_active(True)
            if not stable_available:
                stable_btn.set_sensitive(False)
                box.set_tooltip_text(stable_tooltip)
        else:
            stable_btn.set_active(True)

        stable_btn.connect(
            "toggled",
            self._on_detection_method_toggled,
            device,
            "stable",
            product_btn,
        )
        product_btn.connect(
            "toggled",
            self._on_detection_method_toggled,
            device,
            "product",
            stable_btn,
        )

        box.append(stable_btn)
        box.append(product_btn)
        return box

    def _on_add_event_device_clicked(self, _button: Gtk.Button) -> None:
        from keymasq.gui.wizards.hardware_setup import HardwareSetupDialog

        parent = self._parent_window()
        picker = HardwareSetupDialog(
            parent,
            self._hardware_manager,
            raw_evdev_only=True,
            select_evdev_only=True,
        )
        picker.connect("evdev-devices-selected", self._on_evdev_devices_selected)
        picker.present(parent)

    def _on_evdev_devices_selected(
        self,
        _dialog: object,
        raw_devices: object,
    ) -> None:
        if not isinstance(raw_devices, list):
            return
        devices = [
            device
            for device in cast(list[object], raw_devices)
            if isinstance(device, EvdevDevice)
        ]
        added = self._on_add_devices(devices)
        self._refresh_interface_rows()
        if added:
            self._set_status(
                f"Added {_count_label(added, 'event device')} to this hardware ID."
            )
        else:
            self._set_status("That event device is already attached.")

    def _on_detection_method_toggled(
        self,
        button: Gtk.ToggleButton,
        device: EvdevDevice,
        method: DetectionMethod,
        fallback_button: Gtk.ToggleButton,
    ) -> None:
        if self._updating_detection_method or not button.get_active():
            return
        if method == _detection_method_for_device(device):
            return

        ok, message = self._on_set_detection_method(device, method)
        if ok:
            self._refresh_interface_rows()
            self._set_status(message)
            return

        self._updating_detection_method = True
        try:
            button.set_active(False)
            fallback_button.set_active(True)
        finally:
            self._updating_detection_method = False
        self._set_status(message, error=True)

    def _on_delete_hardware_clicked(self, _button: Gtk.Button) -> None:
        self._on_delete_device()

    def _on_hardware_docs_clicked(self, _button: Gtk.Button) -> None:
        url = _hardware_docs_url()
        try:
            launcher = Gtk.UriLauncher.new(url)
            launcher.launch(None, None, None)
        except Exception:
            log.exception("Could not open Hardware documentation %s", url)

    def _on_rename_clicked(self, _button: Gtk.Button) -> None:
        self._open_rename_dialog()

    def _on_identity_row_activated(self, _row: Adw.ActionRow) -> None:
        self._open_rename_dialog()

    def _on_identity_row_right_clicked(
        self,
        _click: Gtk.GestureClick,
        n_press: int,
        _x: float,
        _y: float,
    ) -> None:
        if n_press != 1:
            return
        _click.set_state(Gtk.EventSequenceState.CLAIMED)
        self._open_rename_dialog()

    def _open_rename_dialog(self) -> None:
        self._on_rename_device(self._refresh_identity)

    def _refresh_identity(self) -> None:
        if self._identity_row is None:
            return
        self._identity_row.set_title(self._hardware_config.name)
        self._identity_row.set_subtitle(self._hardware_config.hardware_id)

    def _on_delete_evdev_clicked(
        self,
        _button: Gtk.Button,
        device: EvdevDevice,
    ) -> None:
        self._present_delete_evdev_dialog(device)

    def _present_delete_evdev_dialog(self, device: EvdevDevice) -> None:
        dialog = Adw.Dialog(title="Remove Event Device", content_width=420, content_height=-1)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_margin_top(16)
        body.set_margin_bottom(16)
        body.set_margin_start(16)
        body.set_margin_end(16)

        message = Gtk.Label(
            label=(
                f"Remove '{_interface_row_title(device)}' from "
                f"{self._hardware_config.name}?"
            )
        )
        message.set_halign(Gtk.Align.START)
        message.set_wrap(True)
        body.append(message)

        attached_control_ids = self._attached_control_ids(device)
        if attached_control_ids:
            detail = Gtk.Label(
                label=(
                    f"This also removes {_count_label(len(attached_control_ids), 'control')} "
                    "from the hardware layout."
                )
            )
            detail.add_css_class("dim-label")
            detail.set_halign(Gtk.Align.START)
            detail.set_wrap(True)
            body.append(detail)

        delete_profiles_check = Gtk.CheckButton(
            label="Remove attached profile mappings"
        )
        delete_profiles_check.set_active(
            bool(attached_control_ids) and self._can_delete_profile_mappings
        )
        delete_profiles_check.set_sensitive(
            bool(attached_control_ids) and self._can_delete_profile_mappings
        )
        body.append(delete_profiles_check)

        error_label = Gtk.Label()
        error_label.add_css_class("error")
        error_label.set_halign(Gtk.Align.START)
        error_label.set_wrap(True)
        error_label.set_visible(False)
        body.append(error_label)

        content.append(body)
        content.append(Gtk.Separator())

        footer = Gtk.CenterBox(orientation=Gtk.Orientation.HORIZONTAL)
        footer.set_margin_top(6)
        footer.set_margin_bottom(6)
        footer.set_margin_start(12)
        footer.set_margin_end(12)

        end_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_close_delete_evdev_clicked, dialog)
        end_box.append(cancel_btn)

        remove_btn = Gtk.Button(label="Remove")
        remove_btn.add_css_class("destructive-action")
        remove_btn.connect(
            "clicked",
            self._on_confirm_delete_evdev_clicked,
            dialog,
            device,
            delete_profiles_check,
            error_label,
        )
        end_box.append(remove_btn)
        footer.set_end_widget(end_box)
        content.append(footer)

        dialog.set_child(content)
        dialog.present(self._parent_window())

    def _on_close_delete_evdev_clicked(
        self,
        _button: Gtk.Button,
        dialog: Adw.Dialog,
    ) -> None:
        dialog.close()

    def _on_confirm_delete_evdev_clicked(
        self,
        button: Gtk.Button,
        dialog: Adw.Dialog,
        device: EvdevDevice,
        delete_profiles_check: Gtk.CheckButton,
        error_label: Gtk.Label,
    ) -> None:
        button.set_sensitive(False)
        if self._on_delete_evdev_device(device, delete_profiles_check.get_active()):
            dialog.close()
            self._refresh_interface_rows()
            self._set_status("Removed event device.")
            return
        button.set_sensitive(True)
        error_label.set_label("Event device could not be removed.")
        error_label.set_visible(True)

    def _attached_control_ids(self, device: EvdevDevice) -> list[str]:
        source = str(device.id or "").strip()
        if not source:
            return []
        ids = [button.id for button in self._hardware_config.buttons if button.source == source]
        ids.extend(
            analog.id for analog in self._hardware_config.analog_inputs if analog.source == source
        )
        return ids

    def _set_status(self, message: str, *, error: bool = False) -> None:
        self._status_label.set_label(message)
        if error:
            self._status_label.add_css_class("error")
            self._status_label.remove_css_class("dim-label")
            return
        self._status_label.remove_css_class("error")
        self._status_label.add_css_class("dim-label")

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _parent_window(self) -> Gtk.Window:
        if isinstance(self._parent, Gtk.Window):
            return self._parent
        return Gtk.Window()


def _interface_row_title(device: EvdevDevice) -> str:
    source_id = str(device.id or "").strip()
    device_type = _evdev_device_type(device).value
    return f"{source_id or 'input'} ({device_type})"


def _detection_method_for_device(device: EvdevDevice) -> DetectionMethod:
    if is_keymasq_device_path(str(device.path or "")):
        return "product"
    return "stable"


def _evdev_device_type(device: EvdevDevice) -> DeviceType:
    raw_value = getattr(device.device_type, "value", device.device_type)
    try:
        return DeviceType(str(raw_value or "other"))
    except ValueError:
        return DeviceType.OTHER


def _evdev_device_signature(device: EvdevDevice) -> tuple[str, str, str, tuple[str, ...]]:
    path = str(device.path or "").strip()
    if _is_real_evdev_path(path):
        return (path, "", "", ())
    return (
        path,
        _evdev_device_type(device).value,
        str(device.phys or "").strip(),
        tuple(sorted(str(item).strip().lower() for item in device.capabilities if str(item))),
    )


def _is_real_evdev_path(path: str) -> bool:
    return path.startswith("/dev/input/") and not is_keymasq_device_path(path)


def _normalize_interface_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower()).strip("_")


def _dedupe_interface_id(base_id: str, used_ids: set[str]) -> str:
    candidate = _normalize_interface_id(base_id) or "input"
    if candidate not in used_ids:
        used_ids.add(candidate)
        return candidate

    index = 2
    while f"{candidate}_{index}" in used_ids:
        index += 1
    deduped = f"{candidate}_{index}"
    used_ids.add(deduped)
    return deduped


def _count_label(count: int, singular: str) -> str:
    return f"{count} {singular if count == 1 else f'{singular}s'}"
