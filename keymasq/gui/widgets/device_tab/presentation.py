from collections.abc import Callable
from typing import Any, cast

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gtk, Pango  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.model.core import ActionType
from keymasq.common.model.hardware import HardwareConfig
from keymasq.gui.icons import device_icon_names, image_from_icon_names, resolve_icon_name
from keymasq.gui.session_client import JsonDict
from keymasq.session.profile.types import ProfileInfo


class ProfilePresentationMixin:
    def _selected_layer(self: Any, create: bool = False):
        if not self._selected_profile:
            return None
        if create:
            return self._selected_profile.config.ensure_layer(self.device.hardware_id)
        return self._selected_profile.config.get_layer(self.device.hardware_id)

    def _device_layer_for_profile(self: Any, profile: ProfileInfo | None, create: bool = False):
        if profile is None:
            return None
        if create:
            return profile.config.ensure_layer(self.device.hardware_id)
        return profile.config.get_layer(self.device.hardware_id)

    def _resolve_mapping_target_profile(
        self: Any,
        target_profile: ProfileInfo | None,
    ) -> ProfileInfo | None:
        target_profile = target_profile or self._selected_profile
        if target_profile is None:
            return None
        if self.profile_manager is None or self.demo_mode:
            return target_profile
        return self.profile_manager.get_profile(target_profile.config.name)

    def _profile_is_selected(self: Any, profile: ProfileInfo | None) -> bool:
        return (
            profile is not None
            and self._selected_profile is not None
            and self._selected_profile.config.name == profile.config.name
        )

    def _append_profile_settings_groups(self: Any, container: Gtk.Box) -> None:
        self.always_grab_checks: dict[str, Adw.SwitchRow] = {}

        grab_group = Adw.PreferencesGroup()
        self.always_grab_group = grab_group
        self._sync_always_grab_device_list()

        if not hasattr(self, "always_grab_check"):
            self.always_grab_check = Adw.SwitchRow(title=self._device_grab_label_text())

        container.append(grab_group)

    def _update_extra_profile_settings(self: Any) -> None:
        self._sync_always_grab_device_list()
        for hardware_id, switch_row in self.always_grab_checks.items():
            layer = self._profile_layer_for_hardware(hardware_id)
            switch_row.handler_block_by_func(self._on_always_grab_toggled)
            switch_row.set_active(layer.always_grab_all if layer else False)
            switch_row.handler_unblock_by_func(self._on_always_grab_toggled)

    def _active_profile_names_from_response(self: Any, data: dict) -> list[str]:
        devices = data.get("devices", {})
        if not isinstance(devices, dict):
            return []
        return list(devices.get(self.device.hardware_id, {}).get("profiles", []))

    def _active_profiles_summary_title(self: Any) -> str:
        return "Applied profiles:"

    def _active_profiles_summary_tooltip(self: Any) -> str:
        return (
            "Profiles currently applied to this device. "
            "Enabled profiles without mappings are not listed."
        )

    def _active_profiles_empty_tooltip(self: Any) -> str:
        return "No profiles are currently applied to this device."

    def _active_profiles_layer_tooltip(self: Any) -> str:
        return "Applied profiles. Layer order: " + " -> ".join(self._active_profile_names)

    def _after_profile_selection_applied(self: Any) -> None:
        for button_id in self._button_widgets:
            self._update_button_display(button_id)
        self._update_header_caption()

    def _after_active_profiles_changed(self: Any) -> None:
        for button_id in self._button_widgets:
            self._update_button_display(button_id)
        self._update_header_caption()

    def _count_mapped_buttons(self: Any) -> int:
        layer = self._selected_layer()
        if not layer:
            return 0
        return sum(
            1
            for mapping in layer.mappings.values()
            if mapping.action_type != ActionType.PASSTHROUGH
        )

    def _count_label(self: Any, count: int, singular: str, plural: str | None = None) -> str:
        label = singular if count == 1 else plural or f"{singular}s"
        return f"{count} {label}"

    def _update_header_caption(self: Any) -> None:
        mapped = self._count_mapped_buttons()
        base = self._header_caption_text()
        status_note = self._device_status_caption_note()
        if status_note:
            base = f"{base} - {status_note}"
        if mapped > 0:
            caption = f"{base} · {self._count_label(mapped, 'mapping')}"
        else:
            caption = base
        self._header_caption_label.set_text(caption)
        self._header_caption_label.set_tooltip_text(self._header_hardware_tooltip())

    def _profile_layer_for_hardware(self: Any, hardware_id: str, create: bool = False):
        if not self._selected_profile:
            return None
        if create:
            return self._selected_profile.config.ensure_layer(hardware_id)
        return self._selected_profile.config.get_layer(hardware_id)

    def _profile_settings_devices(self: Any) -> list[HardwareConfig]:
        devices: list[HardwareConfig] = []
        seen: set[str] = set()

        root = self.main_window or self.get_root()
        list_device_tab_configs = getattr(root, "list_device_tab_configs", None)
        if callable(list_device_tab_configs):
            get_devices = cast(Callable[[], list[HardwareConfig]], list_device_tab_configs)
            for device in get_devices():
                hardware_id = getattr(device, "hardware_id", None)
                if isinstance(hardware_id, str) and hardware_id not in seen:
                    devices.append(device)
                    seen.add(hardware_id)

        stack = getattr(root, "stack", None)
        if stack is not None:
            child = stack.get_first_child()
            while child is not None:
                device = getattr(child, "device", None)
                hardware_id = getattr(device, "hardware_id", None)
                if (
                    isinstance(device, HardwareConfig)
                    and isinstance(hardware_id, str)
                    and hardware_id not in seen
                ):
                    devices.append(device)
                    seen.add(hardware_id)
                child = child.get_next_sibling()

        if self.device.hardware_id not in seen:
            devices.append(self.device)

        return devices

    def _sync_always_grab_device_list(self: Any) -> None:
        if not hasattr(self, "always_grab_checks"):
            return

        devices = self._profile_settings_devices()
        current_ids = {device.hardware_id for device in devices}
        for hardware_id, switch_row in list(self.always_grab_checks.items()):
            if hardware_id in current_ids:
                continue
            self.always_grab_group.remove(switch_row)
            del self.always_grab_checks[hardware_id]

        for device in self._profile_settings_devices():
            switch_row = self.always_grab_checks.get(device.hardware_id)
            if switch_row is None:
                switch_row = Adw.SwitchRow()
                switch_row.set_tooltip_text(
                    "Grab all device interfaces even if not all are used. "
                    "Prevents lag when switching between profiles that need different interfaces."
                )
                switch_row.connect(
                    "notify::active", self._on_always_grab_toggled, device.hardware_id
                )
                self.always_grab_group.add(switch_row)
                self.always_grab_checks[device.hardware_id] = switch_row
            switch_row.set_title(self._device_grab_label_text(device))
            if device.hardware_id == self.device.hardware_id:
                self.always_grab_check = switch_row

    def _on_always_grab_toggled(
        self: Any,
        switch_row: Adw.SwitchRow,
        _param,
        hardware_id: str | None = None,
    ) -> None:
        layer = self._profile_layer_for_hardware(
            hardware_id or self.device.hardware_id,
            create=True,
        )
        if layer:
            layer.always_grab_all = switch_row.get_active()
            self._save_profile()

    def _setup_header(self: Any) -> None:
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        device_icon = image_from_icon_names(
            *device_icon_names(device_kind=self.device_layout_kind()), pixel_size=32
        )
        device_icon.set_valign(Gtk.Align.CENTER)
        header_box.append(device_icon)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        name_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self.device_name_label = Gtk.Label(label=self.device.name)
        self.device_name_label.add_css_class("title-2")
        self.device_name_label.set_halign(Gtk.Align.START)
        self.device_name_label.set_ellipsize(Pango.EllipsizeMode.END)
        if not self.demo_mode and self.hardware_manager is not None:
            self.device_name_label.set_tooltip_text("Right-click to rename device")
            name_right_click = Gtk.GestureClick()
            name_right_click.set_button(Gdk.BUTTON_SECONDARY)
            name_right_click.connect("pressed", self._on_device_name_right_clicked)
            self.device_name_label.add_controller(name_right_click)
        name_row.append(self.device_name_label)

        self._device_status_label = Gtk.Label()
        self._device_status_label.add_css_class("status-pill")
        self._device_status_label.set_valign(Gtk.Align.CENTER)
        name_row.append(self._device_status_label)

        if not self.demo_mode:
            inspect_btn = Gtk.Button(
                icon_name=resolve_icon_name(
                    "view-reveal-symbolic",
                    "edit-find-symbolic",
                    "system-search-symbolic",
                    "zoom-in-symbolic",
                    "dialog-information-symbolic",
                )
            )
            inspect_btn.set_tooltip_text("Inspect device")
            inspect_btn.add_css_class("flat")
            inspect_btn.set_valign(Gtk.Align.CENTER)
            inspect_btn.connect("clicked", self._on_inspect_device_clicked)
            name_row.append(inspect_btn)

            settings_btn = Gtk.Button(icon_name="emblem-system-symbolic")
            settings_btn.set_tooltip_text("Hardware settings")
            settings_btn.add_css_class("flat")
            settings_btn.set_valign(Gtk.Align.CENTER)
            settings_btn.set_sensitive(self.hardware_manager is not None)
            settings_btn.connect("clicked", self._on_hardware_settings_clicked)
            name_row.append(settings_btn)

        info_box.append(name_row)

        caption = self._header_caption_text()
        self._header_caption_label = Gtk.Label(label=caption)
        self._header_caption_label.add_css_class("dim-label")
        self._header_caption_label.add_css_class("caption")
        self._header_caption_label.set_halign(Gtk.Align.START)
        self._header_caption_label.set_tooltip_text(self._header_hardware_tooltip())
        info_box.append(self._header_caption_label)

        header_box.append(info_box)
        header_box.set_hexpand(True)

        self.append(header_box)

        self.set_focusable(True)
        self._update_device_status_pill()

    def _header_hardware_tooltip(self: Any) -> str:
        lines = [f"Hardware ID: {self.device.hardware_id}"]
        interfaces = self._device_runtime_interfaces()
        if interfaces:
            lines.append("Interfaces:")
            for interface in interfaces:
                lines.append(f"  {self._interface_tooltip_line(interface)}")
        else:
            paths = [str(device.path or "") for device in self.device.evdev_devices if device.path]
            if paths:
                lines.append("Interfaces:")
                lines.extend(paths)
        return "\n".join(lines)

    def _header_caption_text(self: Any) -> str:
        parts = [f"{len(self.device.buttons)} buttons"]
        analog_count = len(self.device.analog_inputs)
        if analog_count:
            parts.append(f"{analog_count} analog inputs")
        if not self.demo_mode and self._device_runtime_ready():
            configured_count = self._device_status_count(
                "configured_count",
                len(self.device.evdev_devices),
            )
            connected_count = self._device_status_count("connected_count", 0)
            grabbed_count = self._device_status_count("grabbed_count", 0)
            status_parts = [
                self._count_label(configured_count, "interface"),
                f"{connected_count} connected",
                f"{grabbed_count} grabbed",
            ]
            return f"{self.device.model_id} | {' · '.join(status_parts)}"
        return (
            f"{self.device.model_id} | {len(self.device.evdev_devices)} evdev, {', '.join(parts)}"
        )

    def _device_runtime_status_from_response(self: Any, data: dict) -> JsonDict:
        devices = data.get("devices", {})
        if not isinstance(devices, dict):
            return {}
        raw_device = devices.get(self.device.hardware_id, {})
        if not isinstance(raw_device, dict):
            return {}
        raw_status = raw_device.get("device_status", {})
        return dict(raw_status) if isinstance(raw_status, dict) else {}

    def _update_device_status_pill(self: Any) -> None:
        if not hasattr(self, "_device_status_label"):
            return
        label = self._device_status_label
        for css_class in ("status-active", "status-waiting", "status-inactive", "status-standby"):
            label.remove_css_class(css_class)
        text, css_class = self._device_status_pill()
        label.set_text(text)
        label.add_css_class(css_class)
        label.set_tooltip_text(self._device_status_tooltip_text())

    def _device_status_pill(self: Any) -> tuple[str, str]:
        if self.demo_mode:
            return "Demo", "status-standby"
        state = str(self._device_runtime_status.get("state", "unknown") or "unknown")
        if state == "grabbed":
            return "Grabbed", "status-active"
        if state in {"waiting", "partial"}:
            return "Waiting", "status-waiting"
        if state == "connected":
            return "Connected", "status-waiting"
        if state == "not_connected":
            return "Not connected", "status-inactive"
        if state == "inspector":
            return "Inspector", "status-waiting"
        return "Unknown", "status-standby"

    def _device_status_tooltip_text(self: Any) -> str:
        if self.demo_mode:
            return "Live device status is not available in demo mode."
        state = str(self._device_runtime_status.get("state", "unknown") or "unknown")
        if state == "grabbed":
            return "Connected and grabbed by keymasqd."
        if state == "partial":
            return "Connected, but not every requested interface is grabbed."
        if state == "waiting":
            return "Connected, but keymasqd is waiting to grab this device."
        if state == "connected":
            return "Connected. No interface is currently grabbed."
        if state == "not_connected":
            return "Configured device is not currently connected."
        if state == "inspector":
            return "Device inspector is active for this device."
        return "Session or daemon runtime status is not available."

    def _device_runtime_ready(self: Any) -> bool:
        return bool(self._device_runtime_status.get("runtime_ready", False))

    def _device_status_count(self: Any, key: str, default: int) -> int:
        value = self._device_runtime_status.get(key, default)
        try:
            return max(0, int(cast(int | float | str, value)))
        except (TypeError, ValueError):
            return default

    def _device_runtime_interfaces(self: Any) -> list[JsonDict]:
        raw_interfaces = self._device_runtime_status.get("interfaces", [])
        if not isinstance(raw_interfaces, list):
            return []
        return [dict(item) for item in raw_interfaces if isinstance(item, dict)]

    def _device_status_caption_note(self: Any) -> str:
        if self.demo_mode or not self._device_runtime_status:
            return ""
        state = str(self._device_runtime_status.get("state", "unknown") or "unknown")
        if state == "waiting":
            waiting_path = self._waiting_status_path()
            if waiting_path:
                return f"waiting on {waiting_path}"
        if state == "inspector":
            return "device inspector active"
        return ""

    def _waiting_status_path(self: Any) -> str:
        grab_status = self._device_runtime_status.get("grab_status", {})
        if isinstance(grab_status, dict):
            path = str(grab_status.get("path", "") or "").strip()
            if path:
                return path
        for interface in self._device_runtime_interfaces():
            if bool(interface.get("requested", False)) and not bool(
                interface.get("grabbed", False)
            ):
                path = str(
                    interface.get("current_path") or interface.get("configured_path") or ""
                ).strip()
                if path:
                    return path
        return ""

    def _interface_tooltip_line(self: Any, interface: JsonDict) -> str:
        configured_path = str(interface.get("configured_path", "") or "").strip()
        label = configured_path or str(interface.get("id", "") or "").strip() or "interface"
        states: list[str] = []
        if bool(interface.get("connected", False)):
            states.append("connected")
        else:
            states.append("not connected")
        if bool(interface.get("grabbed", False)):
            states.append("grabbed")
        elif bool(interface.get("requested", False)):
            states.append("waiting for grab")
        current_path = str(interface.get("current_path", "") or "").strip()
        if current_path and current_path != configured_path:
            return f"{label} - {', '.join(states)} ({current_path})"
        return f"{label} - {', '.join(states)}"

    def _device_grab_label_text(self: Any, device: HardwareConfig | None = None) -> str:
        resolved_device = device if device is not None else cast(HardwareConfig, self.device)
        iface_count = len(resolved_device.evdev_devices)
        if iface_count > 1:
            return f"Always grab all {iface_count} interfaces of {resolved_device.name}"
        return f"Always grab {resolved_device.name}"

    def _on_inspect_device_clicked(self: Any, _button: Gtk.Button) -> None:
        root = self.main_window or self.get_root()
        opener = getattr(root, "open_device_inspector", None)
        if callable(opener):
            opener(self.device)
