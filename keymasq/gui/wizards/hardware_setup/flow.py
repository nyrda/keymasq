from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any, cast

import evdev
import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.devices import capability_names_from_capabilities
from keymasq.common.model.core import DeviceType
from keymasq.gui.session_client import GuiTaskResult

from . import discovery, rows
from .identity import (
    config_path_for_detected_interface,
    interface_id_for_config,
    interface_source_fields,
)
from .types import DetectedDevice, DetectedInterface

log = logging.getLogger("keymasq.gui.hardware_setup")


class DiscoveryMixin:
    def _detect_devices(self: Any) -> None:
        if not self._discovery_state.begin_detection():
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
        self._run_gui_task(
            self._collect_detected_devices,
            self._on_detected_devices_ready,
            on_done=self._on_detected_devices_done,
        )

    def _collect_detected_devices(self: Any) -> dict[str, DetectedDevice]:
        detected_devices: dict[str, DetectedDevice] = {}
        self._detect_devices_via_session(detected_devices)
        return detected_devices

    def _on_detected_devices_done(self: Any) -> None:
        self._discovery_state.finish_detection()
        self.raw_evdev_check.set_sensitive(not self._raw_evdev_only)

    def _on_detected_devices_ready(
        self: Any,
        result: GuiTaskResult[dict[str, DetectedDevice]],
    ) -> bool:
        detected_devices = result.value if result.ok and result.value is not None else {}
        while row := self.device_list.get_row_at_index(0):
            self.device_list.remove(row)
        self._discovery_state.detected_devices = detected_devices

        sorted_devices = sorted(
            self._discovery_state.detected_devices.items(),
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
                    show_raw_evdev_devices=self._discovery_state.show_raw,
                )
            )
        if not detected_devices:
            self.device_list.append(rows.build_no_devices_row())
        return False

    def _detect_devices_via_session(
        self: Any,
        detected_devices: dict[str, DetectedDevice],
    ) -> bool:
        return discovery.detect_devices_via_session(
            detected_devices,
            hardware_manager=self.hardware_manager,
            show_raw_evdev_devices=self._discovery_state.show_raw,
        )

    def _start_discover_interfaces(self: Any) -> None:
        selected_device = self._discovery_state.selected_device
        self.next_btn.set_sensitive(False)
        if not selected_device:
            self._discovery_state.clear_selection()
            return

        request_id = self._discovery_state.select(selected_device)
        selected_snapshot = deepcopy(selected_device)
        selected_key = self._selected_device_request_key(selected_device)

        def handle_result(
            result: GuiTaskResult[dict[str, DetectedInterface]],
        ) -> bool:
            return self._on_discovered_interfaces_ready(
                request_id,
                selected_key,
                result,
            )

        self._run_gui_task(
            lambda: self._discover_interfaces(selected_snapshot),
            handle_result,
            on_done=lambda: self._on_discovered_interfaces_done(request_id),
        )

    def _on_discovered_interfaces_ready(
        self: Any,
        request_id: int,
        selected_key: str,
        result: GuiTaskResult[dict[str, DetectedInterface]],
    ) -> bool:
        selected_device = self._discovery_state.selected_device
        current_key = self._selected_device_request_key(selected_device) if selected_device else ""
        if not self._discovery_state.accepts(request_id, selected_key, current_key):
            return False

        self._discovery_state.discovered_interfaces = (
            result.value if result.ok and result.value is not None else {}
        )
        self._refresh_configure_modes()
        self.next_btn.set_sensitive(
            bool(self._discovery_state.discovered_interfaces)
            and not self._device_in_use(selected_device)
        )
        return False

    def _on_discovered_interfaces_done(self: Any, request_id: int) -> None:
        self._discovery_state.finish_discovery(request_id)

    def _discover_interfaces(
        self: Any,
        selected_device: DetectedDevice,
    ) -> dict[str, DetectedInterface]:
        if not selected_device:
            return {}

        vendor_id = str(selected_device.get("vendor_id", "") or "")
        product_id = str(selected_device.get("product_id", "") or "")
        discovered_interfaces: dict[str, DetectedInterface] = {}
        selected_interfaces = list(selected_device.get("interfaces", []) or [])
        interfaces: list[dict[str, Any]] = []
        for iface in selected_interfaces:
            raw_path = str(iface.get("path", "") or "")
            if not raw_path:
                continue
            stable_path = str(iface.get("stable_path", "") or self._resolve_stable_path(raw_path))
            default_config_path = config_path_for_detected_interface(
                vendor_id,
                product_id,
                stable_path,
            )
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
                    **interface_source_fields(iface),
                }
            )

        if not interfaces:
            interfaces = self._find_all_interfaces(vendor_id, product_id)
            for iface in interfaces:
                raw_path = str(iface.get("path", "") or "")
                stable_path = str(iface.get("stable_path", "") or raw_path)
                iface["config_path"] = config_path_for_detected_interface(
                    vendor_id,
                    product_id,
                    stable_path,
                )
        used_interface_ids: set[str] = set()
        for iface in interfaces:
            merged_iface = {**iface, "device_types": self._interface_device_types(iface)}
            raw_iface_id = interface_id_for_config(merged_iface, used_interface_ids)
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
                    **interface_source_fields(iface),
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

    def _read_interface_capabilities(
        self: Any,
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
