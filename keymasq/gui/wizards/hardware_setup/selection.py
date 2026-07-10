from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.devices import input_classes_include_gamepad, make_keymasq_device_path

from . import discovery, inventory, rows, templates
from .identity import config_path_for_detected_interface
from .types import DetectedDevice

MODE_LABELS = {
    "gamepad": "Gamepad",
    "mouse_keyboard": "Mouse + Keyboard",
    "mouse": "Mouse",
    "keyboard": "Keyboard",
    "custom": "Custom",
}


class SelectionMixin:
    def _on_raw_evdev_toggled(self: Any, check: Gtk.CheckButton) -> None:
        if self._raw_evdev_only:
            if not check.get_active():
                check.set_active(True)
            self._discovery_state.show_raw = True
            return
        self._discovery_state.show_raw = check.get_active()
        self._discovery_state.selected_device = None
        self.next_btn.set_sensitive(False)
        self._detect_devices()

    def _should_show_interface_expander(
        self: Any,
        interfaces: Sequence[Mapping[str, Any]],
    ) -> bool:
        return rows.should_show_interface_expander(self._discovery_state.show_raw, interfaces)

    def _device_in_use(self: Any, dev_info: Mapping[str, Any]) -> bool:
        return rows.device_in_use(dev_info)

    @staticmethod
    def _device_in_use_summary(dev_info: Mapping[str, Any]) -> str:
        return rows.device_in_use_summary(dev_info)

    def _configured_hardware_ids(self: Any) -> set[str]:
        return inventory.configured_hardware_ids(self.hardware_manager)

    def _allocate_hardware_id(
        self: Any,
        model_id: str,
        used_hardware_ids: set[str],
    ) -> str:
        return discovery.allocate_hardware_id(model_id, used_hardware_ids)

    @staticmethod
    def _selected_device_fields(selected_device: DetectedDevice) -> tuple[str, str, str]:
        return (
            str(selected_device.get("vendor_id", "") or ""),
            str(selected_device.get("product_id", "") or ""),
            str(selected_device.get("name", "") or "Device"),
        )

    def _selected_config_id(self: Any, selected_device: DetectedDevice) -> str | None:
        vendor_id, product_id, _name = self._selected_device_fields(selected_device)
        model_id = f"{vendor_id}:{product_id}"
        hardware_id = str(selected_device.get("hardware_id") or model_id)
        if (
            self._discovery_state.show_raw or self._selected_uses_model_path(selected_device)
        ) and not self._device_in_use(selected_device):
            hardware_id = self._allocate_hardware_id(
                model_id,
                self._configured_hardware_ids(),
            )
        return hardware_id if hardware_id != model_id else None

    def _selected_uses_model_path(self: Any, selected_device: DetectedDevice) -> bool:
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
                config_path = config_path_for_detected_interface(
                    vendor_id,
                    product_id,
                    stable_path,
                )
            if config_path == make_keymasq_device_path(vendor_id, product_id):
                return True
        return False

    def _on_device_selected(self: Any, _list_box: Gtk.ListBox, row: Any) -> None:
        if row is None:
            self._clear_device_selection()
            return
        hardware_id = str(row.hardware_id)
        self._discovery_state.selected_device = self._discovery_state.detected_devices[hardware_id]
        expander = getattr(row, "_expander", None)
        if expander is not None:
            expander.set_expanded(True)

        index = 0
        while True:
            other = self.device_list.get_row_at_index(index)
            if other is None:
                break
            other_expander = getattr(other, "_expander", None)
            if other is not row and other_expander is not None:
                other_expander.set_expanded(False)
            index += 1
        self._start_discover_interfaces()

    def _clear_device_selection(self: Any) -> None:
        self._discovery_state.clear_selection()
        self.next_btn.set_sensitive(False)

    def _interface_device_types(self: Any, iface: Mapping[str, Any]) -> list[str]:
        return templates.interface_device_types(iface)

    def _refresh_configure_modes(self: Any) -> None:
        if not self._discovery_state.selected_device:
            return
        interfaces: Sequence[Mapping[str, Any]] = (
            list(self._discovery_state.discovered_interfaces.values())
            if self._discovery_state.discovered_interfaces
            else self._discovery_state.selected_device.get("interfaces", []) or []
        )
        roles = {role for iface in interfaces for role in self._interface_device_types(iface)}
        values = self._template_state.refresh(
            roles,
            show_raw=self._discovery_state.show_raw,
        )
        self.mode_combo_model.splice(0, self.mode_combo_model.get_n_items(), [])
        for value in values:
            self.mode_combo_model.append(MODE_LABELS[value])
        self.mode_combo.set_selected(values.index(self._template_state.current))
        self.mode_row.set_visible(len(values) > 1)
        self._update_describe_mode_ui()

    def _preferred_configure_mode(self: Any) -> str:
        self._template_state.current = self._template_state.current
        self._template_state.values = self._template_state.values
        return self._template_state.preferred()

    def _on_mode_changed(self: Any, combo: Gtk.DropDown, _param: object) -> None:
        if not self._template_state.select(combo.get_selected()):
            return
        self._update_describe_mode_ui()

    def _update_describe_mode_ui(self: Any) -> None:
        mode = self._template_state.current
        self.keyboard_mode_info.set_visible(mode == "keyboard")
        self.mouse_mode_info.set_visible(mode == "mouse")
        self.mouse_keyboard_mode_info.set_visible(mode == "mouse_keyboard")
        self.gamepad_mode_info.set_visible(mode == "gamepad")
        self.custom_mode_info.set_visible(mode == "custom")
        subtitle = {
            "gamepad": "Review the detected controller controls",
            "custom": "Create an empty profile for this raw device",
            "mouse_keyboard": "Create a standard keyboard and mouse profile",
            "keyboard": "Create a standard keyboard profile",
        }.get(mode, "Create a standard mouse profile")
        self.describe_subtitle.set_label(subtitle)
        if self.stack.get_visible_child_name() == "describe":
            self.next_btn.set_label("Save")

    def _selected_device_request_key(self: Any, selected_device: DetectedDevice) -> str:
        vendor_id = str(selected_device.get("vendor_id", "") or "")
        product_id = str(selected_device.get("product_id", "") or "")
        return str(
            selected_device.get("hardware_id")
            or selected_device.get("model_id")
            or f"{vendor_id}:{product_id}"
        )
