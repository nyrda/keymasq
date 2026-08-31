import logging
from typing import Any, cast

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.devices import (
    is_by_id_path,
    is_keymasq_device_path,
    make_keymasq_device_path,
)
from keymasq.common.model.hardware import EvdevDevice
from keymasq.gui.session_client import JsonDict
from keymasq.gui.widgets.device_tab.hardware_settings_dialog import (
    DetectionMethod,
    EvdevDevicesAddResult,
    HardwareSettingsDialog,
    append_unique_evdev_devices,
)

log = logging.getLogger(__name__)


class HardwareSettingsMixin:
    def _on_hardware_settings_clicked(self: Any, _button: Gtk.Button) -> None:
        if self.hardware_manager is None:
            return
        if self._hardware_settings_dialog is not None:
            parent = self._hardware_settings_parent()
            self._present_hardware_settings_dialog(self._hardware_settings_dialog, parent)
            return

        parent = self._hardware_settings_parent()
        dialog = HardwareSettingsDialog(
            parent,
            self.device,
            self.hardware_manager,
            self._add_hardware_evdev_devices,
            self._delete_hardware_from_settings,
            self._delete_hardware_evdev_device,
            self._set_hardware_evdev_detection_method,
            self._stable_detection_status_for_evdev_device,
            self._show_device_rename_dialog,
            can_delete_profile_mappings=self.profile_manager is not None,
        )
        dialog.connect("closed", self._on_hardware_settings_dialog_closed)
        self._hardware_settings_dialog = dialog
        self._present_hardware_settings_dialog(dialog, parent)

    def _hardware_settings_parent(self: Any) -> Gtk.Window | None:
        root = self.main_window or self.get_root()
        return root if isinstance(root, Gtk.Window) else None

    def _present_hardware_settings_dialog(
        self: Any,
        dialog: HardwareSettingsDialog,
        parent: Gtk.Window | None,
    ) -> None:
        if parent is not None:
            dialog.present(parent)
            return
        dialog.present()

    def _on_hardware_settings_dialog_closed(self: Any, dialog: Adw.Dialog) -> None:
        if dialog is self._hardware_settings_dialog:
            self._hardware_settings_dialog = None

    def _refresh_hardware_settings_runtime_metadata(self: Any) -> None:
        settings_dialog = self._hardware_settings_dialog
        if settings_dialog is not None:
            settings_dialog.refresh_runtime_metadata()

    def _add_hardware_evdev_devices(
        self: Any,
        evdev_devices: list[EvdevDevice],
    ) -> EvdevDevicesAddResult:
        if self.hardware_manager is None:
            log.warning(
                "Cannot add event devices for %s without a hardware manager",
                self.device.hardware_id,
            )
            return 0, "Action unavailable: missing hardware manager.", True

        conflict = self._product_detection_conflict_for_evdev_devices(evdev_devices)
        if conflict:
            return 0, f"Product ID detection is already used by {conflict}.", True

        added = append_unique_evdev_devices(self.device, evdev_devices)
        if added <= 0:
            return 0, "That event device is already attached.", False

        self.hardware_manager.save_hardware(self.device)
        self._request_session_async({"command": "reload"}, self._ignore_session_response)
        self._sync_always_grab_device_list()
        self._update_header_caption()
        return (
            added,
            f"Added {self._count_label(added, 'event device')} to this hardware ID.",
            False,
        )

    def _product_detection_conflict_for_evdev_devices(
        self: Any,
        evdev_devices: list[EvdevDevice],
    ) -> str:
        for evdev_device in evdev_devices:
            product_path = str(evdev_device.path or "").strip()
            if not is_keymasq_device_path(product_path):
                continue
            conflict = self._hardware_using_product_path(product_path)
            if conflict:
                return conflict
        return ""

    def _set_hardware_evdev_detection_method(
        self: Any,
        evdev_device: EvdevDevice,
        method: DetectionMethod,
    ) -> tuple[bool, str]:
        if method == "product":
            return self._set_hardware_evdev_product_detection(evdev_device)
        return self._set_hardware_evdev_stable_detection(evdev_device)

    def _set_hardware_evdev_product_detection(
        self: Any,
        evdev_device: EvdevDevice,
    ) -> tuple[bool, str]:
        product_path = make_keymasq_device_path(
            self.device.vendor_id,
            self.device.product_id,
        )
        if is_keymasq_device_path(str(evdev_device.path or "")):
            return True, "Event device already uses Product ID detection."
        conflict = self._hardware_using_product_path(product_path)
        if conflict:
            return (
                False,
                f"Product ID detection is already used by {conflict}.",
            )
        return self._update_hardware_evdev_path(
            evdev_device,
            product_path,
            "Switched event device to Product ID detection.",
            preserve_runtime_selectors=True,
        )

    def _set_hardware_evdev_stable_detection(
        self: Any,
        evdev_device: EvdevDevice,
    ) -> tuple[bool, str]:
        stable_available, stable_message = self._stable_detection_status_for_evdev_device(
            evdev_device
        )
        if not stable_available:
            return False, stable_message

        current_path = str(evdev_device.path or "").strip()
        if current_path and not is_keymasq_device_path(current_path):
            if is_by_id_path(current_path):
                return True, "Event device already uses Stable Path detection."

        stable_path = self._runtime_stable_path_for_evdev_device(evdev_device)
        if not stable_path:
            return False, stable_message
        return self._update_hardware_evdev_path(
            evdev_device,
            stable_path,
            "Switched event device to Stable Path detection.",
        )

    def _stable_detection_status_for_evdev_device(
        self: Any,
        evdev_device: EvdevDevice,
    ) -> tuple[bool, str]:
        current_path = str(evdev_device.path or "").strip()
        if current_path and not is_keymasq_device_path(current_path):
            if is_by_id_path(current_path):
                return (
                    True,
                    "Match this event device by its /dev/input/by-id path.",
                )

        interface = self._runtime_interface_for_evdev_device(evdev_device)
        if interface is None:
            return (
                False,
                "Stable Path is unavailable until this event device is connected.",
            )

        stable_path = str(interface.get("stable_path", "") or "").strip()
        if is_by_id_path(stable_path):
            return (
                True,
                "Switch this event device to its /dev/input/by-id path.",
            )
        return (
            False,
            "Stable Path is unavailable because this event device has no /dev/input/by-id path.",
        )

    def _update_hardware_evdev_path(
        self: Any,
        evdev_device: EvdevDevice,
        path: str,
        message: str,
        *,
        preserve_runtime_selectors: bool = False,
    ) -> tuple[bool, str]:
        if self.hardware_manager is None:
            log.warning(
                "Cannot update event device detection for %s without a hardware manager",
                self.device.hardware_id,
            )
            return False, "Action unavailable: missing hardware manager."
        if not path:
            return False, "Event device path could not be determined."

        for configured in self.device.evdev_devices:
            if self._same_evdev_device(configured, evdev_device):
                if preserve_runtime_selectors:
                    self._preserve_runtime_evdev_selectors(configured, evdev_device)
                configured.path = path
                evdev_device.path = path
                self.hardware_manager.save_hardware(self.device)
                self._request_session_async({"command": "reload"}, self._ignore_session_response)
                self._sync_always_grab_device_list()
                self._update_header_caption()
                return True, message
        return False, "Event device could not be found."

    def _preserve_runtime_evdev_selectors(
        self: Any,
        configured: EvdevDevice,
        evdev_device: EvdevDevice,
    ) -> None:
        interface = self._runtime_interface_for_evdev_device(evdev_device)
        if interface is None:
            return

        phys = str(interface.get("phys", "") or "").strip()
        if phys:
            if not str(configured.phys or "").strip():
                configured.phys = phys
            if not str(evdev_device.phys or "").strip():
                evdev_device.phys = phys

        raw_capabilities = interface.get("capabilities", [])
        if not isinstance(raw_capabilities, list | tuple | set):
            return
        capabilities = [str(item) for item in raw_capabilities if str(item)]
        if not capabilities:
            return
        if not configured.capabilities:
            configured.capabilities = list(capabilities)
        if not evdev_device.capabilities:
            evdev_device.capabilities = list(capabilities)

    def _hardware_using_product_path(self: Any, product_path: str) -> str:
        hardware_manager = self.hardware_manager
        if hardware_manager is None:
            return ""
        list_hardware = getattr(hardware_manager, "list_hardware", None)
        if not callable(list_hardware):
            return ""
        for config in cast(list[object], list_hardware()):
            hardware_id = str(getattr(config, "hardware_id", "") or "")
            if hardware_id == self.device.hardware_id:
                continue
            for device in getattr(config, "evdev_devices", []):
                if str(getattr(device, "path", "") or "").strip() == product_path:
                    return hardware_id
        return ""

    def _runtime_interface_for_evdev_device(
        self: Any,
        evdev_device: EvdevDevice,
    ) -> JsonDict | None:
        device_id = str(evdev_device.id or "").strip().lower()
        configured_path = str(evdev_device.path or "").strip()
        for interface in self._device_runtime_interfaces():
            runtime_id = str(interface.get("id", "") or "").strip().lower()
            if device_id and runtime_id and device_id == runtime_id:
                return interface
            if (
                configured_path
                and configured_path == str(interface.get("configured_path", "") or "").strip()
            ):
                return interface
        return None

    def _runtime_stable_path_for_evdev_device(self: Any, evdev_device: EvdevDevice) -> str:
        interface = self._runtime_interface_for_evdev_device(evdev_device)
        stable_path = str((interface or {}).get("stable_path", "") or "").strip()
        return stable_path if is_by_id_path(stable_path) else ""

    def _delete_hardware_from_settings(self: Any) -> None:
        self.present_delete_device_dialog()

    def _delete_hardware_evdev_device(
        self: Any,
        evdev_device: EvdevDevice,
        delete_profile_mappings: bool,
    ) -> bool:
        if self.hardware_manager is None:
            log.warning(
                "Cannot delete event device for %s without a hardware manager",
                self.device.hardware_id,
            )
            return False
        if delete_profile_mappings and self.profile_manager is None:
            log.warning(
                "Cannot delete profile mappings for %s without a profile manager",
                self.device.hardware_id,
            )
            return False

        original_count = len(self.device.evdev_devices)
        self.device.evdev_devices = [
            device
            for device in self.device.evdev_devices
            if not self._same_evdev_device(device, evdev_device)
        ]
        if len(self.device.evdev_devices) == original_count:
            return False

        removed_control_ids = self._remove_controls_for_evdev_device(evdev_device)
        if delete_profile_mappings and self.profile_manager is not None:
            for control_id in removed_control_ids:
                self.profile_manager.remove_device_button_mappings(
                    self.device.hardware_id,
                    control_id,
                )

        self.hardware_manager.save_hardware(self.device)
        self._request_session_async({"command": "reload"}, self._ignore_session_response)
        self._sync_always_grab_device_list()
        if self.profile_manager is not None:
            self._reload_ui()
        else:
            self._update_header_caption()
        return True

    def _remove_controls_for_evdev_device(self: Any, evdev_device: EvdevDevice) -> list[str]:
        source = str(evdev_device.id or "").strip()
        if not source:
            return []

        removed_button_ids = [
            button.id for button in self.device.buttons if button.source == source
        ]
        removed_analog_ids = [
            analog.id for analog in self.device.analog_inputs if analog.source == source
        ]
        removed_motion_ids = [
            sensor.id for sensor in self.device.motion_sensors if sensor.source == source
        ]
        if removed_button_ids:
            self.device.buttons = [
                button for button in self.device.buttons if button.source != source
            ]
        if removed_analog_ids:
            self.device.analog_inputs = [
                analog for analog in self.device.analog_inputs if analog.source != source
            ]
        if removed_motion_ids:
            self.device.motion_sensors = [
                sensor for sensor in self.device.motion_sensors if sensor.source != source
            ]
        return [*removed_button_ids, *removed_analog_ids, *removed_motion_ids]

    @staticmethod
    def _same_evdev_device(left: EvdevDevice, right: EvdevDevice) -> bool:
        return left is right or (
            str(left.path or "").strip() == str(right.path or "").strip()
            and str(left.id or "").strip() == str(right.id or "").strip()
            and str(left.phys or "").strip() == str(right.phys or "").strip()
            and getattr(left.device_type, "value", left.device_type)
            == getattr(right.device_type, "value", right.device_type)
        )
