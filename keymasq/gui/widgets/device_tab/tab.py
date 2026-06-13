import logging
import sys
from collections.abc import Callable
from typing import cast

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gtk, Pango  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.common.models import (
    ActionType,
    AnalogInputDefinition,
    ButtonDefinition,
    DeviceType,
    EvdevDevice,
    HardwareConfig,
    MappingAction,
)
from keymasq.gui.icons import device_icon_names, image_from_icon_names, resolve_icon_name
from keymasq.gui.session_client import (
    JsonDict,
)
from keymasq.gui.session_client import (
    session_request_async as _default_session_request_async,
)
from keymasq.gui.widgets.device_control_layout import device_layout_kind
from keymasq.gui.widgets.device_tab import mapping_display, rename_dialogs
from keymasq.gui.widgets.device_tab.add_inputs_flow import (
    AddInputsFlow,
    AddInputsResult,
)
from keymasq.gui.widgets.device_tab.grid import (
    DeviceGridBuilder,
    DeviceGridCallbacks,
    mapping_action_summary_chars,
    supports_analog_learning,
)
from keymasq.gui.widgets.device_tab.input_helpers import label_from_evdev
from keymasq.gui.widgets.device_tab.learn_analog_flow import (
    LearnAnalogFlow,
    LearnAnalogResult,
)
from keymasq.gui.widgets.key_selector_dialog import KeySelectorDialog
from keymasq.gui.widgets.profile_managed_tab import ProfileManagedTab
from keymasq.session.hardware import HardwareManager
from keymasq.session.profiles import ProfileInfo, ProfileManager

log = logging.getLogger(__name__)
session_request_async = _default_session_request_async
SessionCallback = Callable[[JsonDict | None], bool]


def _session_request_async(payload: JsonDict, callback: SessionCallback) -> object:
    package = sys.modules.get("keymasq.gui.widgets.device_tab")
    request = getattr(package, "session_request_async", session_request_async)
    typed_request = cast(Callable[[JsonDict, SessionCallback], object], request)
    return typed_request(payload, callback)


class DeviceTab(ProfileManagedTab):
    def __init__(
        self,
        device: HardwareConfig,
        profile_manager: ProfileManager | None,
        hardware_manager: "HardwareManager | None" = None,
        main_window=None,
        demo_mode: bool = False,
        compositor_capabilities: list[str] | None = None,
    ) -> None:
        self.device = device
        self.hardware_manager = hardware_manager
        self._device_runtime_status: JsonDict = {}
        super().__init__(
            profile_manager=profile_manager,
            main_window=main_window,
            demo_mode=demo_mode,
            compositor_capabilities=compositor_capabilities,
        )
        self._button_widgets: dict[str, Gtk.Button] = {}
        self._user_interacting = False
        self._keyboard_layout_mode = False
        self._highlight_timeout_ids: list[int] = []
        self._setup_header()
        self._setup_profile_selector()
        self._setup_button_grid()
        self.refresh_profiles()

    def apply_active_profile_response(self, data: dict | None) -> None:
        self._device_runtime_status = self._device_runtime_status_from_response(data or {})
        super().apply_active_profile_response(data)
        self._update_device_status_pill()

    def _selected_layer(self, create: bool = False):
        if not self._selected_profile:
            return None
        if create:
            return self._selected_profile.config.ensure_layer(self.device.hardware_id)
        return self._selected_profile.config.get_layer(self.device.hardware_id)

    def _append_profile_settings_groups(self, container: Gtk.Box) -> None:
        self.always_grab_checks: dict[str, Adw.SwitchRow] = {}

        grab_group = Adw.PreferencesGroup()
        self.always_grab_group = grab_group
        self._sync_always_grab_device_list()

        if not hasattr(self, "always_grab_check"):
            self.always_grab_check = Adw.SwitchRow(title=self._device_grab_label_text())

        container.append(grab_group)

    def _update_extra_profile_settings(self) -> None:
        self._sync_always_grab_device_list()
        for hardware_id, switch_row in self.always_grab_checks.items():
            layer = self._profile_layer_for_hardware(hardware_id)
            switch_row.handler_block_by_func(self._on_always_grab_toggled)
            switch_row.set_active(layer.always_grab_all if layer else False)
            switch_row.handler_unblock_by_func(self._on_always_grab_toggled)

    def _active_profile_names_from_response(self, data: dict) -> list[str]:
        devices = data.get("devices", {})
        if not isinstance(devices, dict):
            return []
        return list(devices.get(self.device.hardware_id, {}).get("profiles", []))

    def _active_profiles_summary_title(self) -> str:
        return "Applied profiles:"

    def _active_profiles_summary_tooltip(self) -> str:
        return (
            "Profiles currently applied to this device. "
            "Enabled profiles without mappings are not listed."
        )

    def _active_profiles_empty_tooltip(self) -> str:
        return "No profiles are currently applied to this device."

    def _active_profiles_layer_tooltip(self) -> str:
        return "Applied profiles. Layer order: " + " -> ".join(self._active_profile_names)

    def _after_profile_selection_applied(self) -> None:
        for button_id in self._button_widgets:
            self._update_button_display(button_id)
        self._update_header_caption()

    def _after_active_profiles_changed(self) -> None:
        for button_id in self._button_widgets:
            self._update_button_display(button_id)
        self._update_header_caption()

    def _count_mapped_buttons(self) -> int:
        layer = self._selected_layer()
        if not layer:
            return 0
        return sum(
            1
            for mapping in layer.mappings.values()
            if mapping.action_type != ActionType.PASSTHROUGH
        )

    def _count_label(self, count: int, singular: str, plural: str | None = None) -> str:
        label = singular if count == 1 else plural or f"{singular}s"
        return f"{count} {label}"

    def _update_header_caption(self) -> None:
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

    def _profile_layer_for_hardware(self, hardware_id: str, create: bool = False):
        if not self._selected_profile:
            return None
        if create:
            return self._selected_profile.config.ensure_layer(hardware_id)
        return self._selected_profile.config.get_layer(hardware_id)

    def _profile_settings_devices(self) -> list[HardwareConfig]:
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

    def _sync_always_grab_device_list(self) -> None:
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
        self,
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

    def _setup_header(self) -> None:
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

            delete_btn = Gtk.Button(icon_name="user-trash-symbolic")
            delete_btn.set_tooltip_text("Delete device")
            delete_btn.add_css_class("destructive-action")
            delete_btn.add_css_class("flat")
            delete_btn.set_valign(Gtk.Align.CENTER)
            delete_btn.set_sensitive(self.hardware_manager is not None)
            delete_btn.connect("clicked", self._on_delete_device)
            name_row.append(delete_btn)

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

    def _header_hardware_tooltip(self) -> str:
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

    def _header_caption_text(self) -> str:
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

    def _device_runtime_status_from_response(self, data: dict) -> JsonDict:
        devices = data.get("devices", {})
        if not isinstance(devices, dict):
            return {}
        raw_device = devices.get(self.device.hardware_id, {})
        if not isinstance(raw_device, dict):
            return {}
        raw_status = raw_device.get("device_status", {})
        return dict(raw_status) if isinstance(raw_status, dict) else {}

    def _update_device_status_pill(self) -> None:
        if not hasattr(self, "_device_status_label"):
            return
        label = self._device_status_label
        for css_class in ("status-active", "status-waiting", "status-inactive", "status-standby"):
            label.remove_css_class(css_class)
        text, css_class = self._device_status_pill()
        label.set_text(text)
        label.add_css_class(css_class)
        label.set_tooltip_text(self._device_status_tooltip_text())

    def _device_status_pill(self) -> tuple[str, str]:
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

    def _device_status_tooltip_text(self) -> str:
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

    def _device_runtime_ready(self) -> bool:
        return bool(self._device_runtime_status.get("runtime_ready", False))

    def _device_status_count(self, key: str, default: int) -> int:
        value = self._device_runtime_status.get(key, default)
        try:
            return max(0, int(cast(int | float | str, value)))
        except (TypeError, ValueError):
            return default

    def _device_runtime_interfaces(self) -> list[JsonDict]:
        raw_interfaces = self._device_runtime_status.get("interfaces", [])
        if not isinstance(raw_interfaces, list):
            return []
        return [dict(item) for item in raw_interfaces if isinstance(item, dict)]

    def _device_status_caption_note(self) -> str:
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

    def _waiting_status_path(self) -> str:
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

    def _interface_tooltip_line(self, interface: JsonDict) -> str:
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

    def _device_grab_label_text(self, device: HardwareConfig | None = None) -> str:
        device = device or self.device
        iface_count = len(device.evdev_devices)
        if iface_count > 1:
            return f"Always grab all {iface_count} interfaces of {device.name}"
        return f"Always grab {device.name}"

    def _on_inspect_device_clicked(self, _button: Gtk.Button) -> None:
        root = self.main_window or self.get_root()
        opener = getattr(root, "open_device_inspector", None)
        if callable(opener):
            opener(self.device)

    def _on_device_name_right_clicked(self, click, n_press, x, y) -> None:
        if n_press != 1 or self.demo_mode or self.hardware_manager is None:
            return
        self._show_device_rename_dialog()

    def _show_device_rename_dialog(self) -> None:
        if self.hardware_manager is None:
            return
        rename_dialogs.present_device_rename_dialog(
            parent=self.get_root(),
            current_name=self.device.name,
            on_save=self._rename_device,
            on_close_clicked=self._on_close_dialog_clicked,
        )

    def _rename_device(self, new_name: str) -> bool:
        new_name = new_name.strip()
        if not new_name:
            return False
        if new_name == self.device.name:
            return True

        if self.hardware_manager is None:
            log.warning(
                "Cannot rename device %s without a hardware manager",
                self.device.hardware_id,
            )
            return False

        self.device.name = new_name
        self.hardware_manager.save_hardware(self.device)
        _session_request_async({"command": "reload"}, lambda _result: False)
        self._update_device_name_display()
        self._notify_device_renamed()
        return True

    def _update_device_name_display(self) -> None:
        self.device_name_label.set_text(self.device.name)
        if hasattr(self, "always_grab_check"):
            self.always_grab_check.set_title(self._device_grab_label_text())
        self._sync_always_grab_device_list()

    def _notify_device_renamed(self) -> None:
        target = self.main_window or self.get_root()
        updater = getattr(target, "update_device_display_name", None)
        if callable(updater):
            updater(self.device.hardware_id, self.device.name)

    def _on_delete_device(self, _button: Gtk.Button) -> None:
        if self.hardware_manager is None:
            return
        self.present_delete_device_dialog()

    def present_delete_device_dialog(self) -> None:
        rename_dialogs.present_delete_device_dialog(
            parent=self.get_root(),
            device_name=self.device.name,
            can_delete=self.hardware_manager is not None,
            can_delete_profiles=self.profile_manager is not None,
            on_confirm_clicked=self._on_confirm_delete_device,
            on_close_clicked=self._on_close_dialog_clicked,
        )

    def _on_confirm_delete_device(
        self,
        button: Gtk.Button,
        dialog: Adw.Dialog,
        delete_profiles_check: Gtk.CheckButton,
        error_label: Gtk.Label | None = None,
    ) -> None:
        delete_profiles = delete_profiles_check.get_active()
        hardware_id = self.device.hardware_id

        if self.hardware_manager is None or (delete_profiles and self.profile_manager is None):
            if error_label is not None:
                error_label.set_label("Action unavailable: missing manager.")
                error_label.set_visible(True)
            return

        button.set_sensitive(False)
        if error_label is not None:
            error_label.set_visible(False)
        _session_request_async(
            {
                "command": "release_device",
                "hardware_id": hardware_id,
                "immediate": True,
            },
            lambda result: self._on_delete_device_release_response(
                result,
                button,
                hardware_id,
                delete_profiles,
                dialog,
                error_label,
            ),
        )

    def _on_delete_device_release_response(
        self,
        result: JsonDict | None,
        button: Gtk.Button,
        hardware_id: str,
        delete_profiles: bool,
        dialog: Adw.Dialog,
        error_label: Gtk.Label | None,
    ) -> bool:
        if isinstance(result, dict) and result.get("status") == "ok":
            return self._delete_device_after_release(
                hardware_id,
                delete_profiles,
                dialog,
            )

        message = "No response from keymasq-session"
        if isinstance(result, dict):
            message = str(result.get("error") or result.get("message") or "release failed")
        log.warning("Failed to release device %s before delete: %s", hardware_id, message)
        button.set_sensitive(True)
        if error_label is not None:
            error_label.set_label(f"Failed to release device: {message}")
            error_label.set_visible(True)
        return False

    def _delete_device_after_release(
        self,
        hardware_id: str,
        delete_profiles: bool,
        dialog: Adw.Dialog,
    ) -> bool:
        hardware_manager = self.hardware_manager
        profile_manager = self.profile_manager
        if hardware_manager is None or (delete_profiles and profile_manager is None):
            log.warning("Cannot delete device %s without required managers", hardware_id)
            return False
        if delete_profiles and profile_manager is not None:
            profile_manager.remove_device_layers(hardware_id)

        hardware_manager.delete_hardware(hardware_id)
        _session_request_async({"command": "reload"}, lambda _result: False)

        dialog.close()

        root = self.main_window or self.get_root()
        remove_device_tab = getattr(root, "remove_device_tab", None)
        if callable(remove_device_tab):
            remove_device_tab(hardware_id)
        elif root and hasattr(root, "stack"):
            root.stack.remove(self)
            root._check_empty_state()
        return False

    def _grid_callbacks(self) -> DeviceGridCallbacks:
        return DeviceGridCallbacks(
            on_add_inputs_clicked=self._on_add_keys_clicked,
            on_learn_analog_clicked=self._on_learn_analog_clicked,
            on_mapping_button_clicked=self._on_mapping_button_clicked,
            on_analog_mapping_clicked=self._on_analog_mapping_clicked,
            on_name_label_right_clicked=self._on_name_label_right_clicked,
            on_action_label_right_clicked=self._on_action_label_right_clicked,
            on_analog_name_right_clicked=self._on_analog_name_right_clicked,
        )

    def _grid_builder(self) -> DeviceGridBuilder:
        return DeviceGridBuilder(
            device=self.device,
            demo_mode=self.demo_mode,
            callbacks=self._grid_callbacks(),
            describe_passthrough_output=self._describe_passthrough_output,
        )

    def _setup_button_grid(self) -> None:
        result = self._grid_builder().build()
        self._keyboard_layout_mode = result.keyboard_layout_mode
        self._button_widgets.update(result.button_widgets)
        self.append(result.widget)

    def _create_learn_tile(self) -> Gtk.Button:
        return self._grid_builder().create_learn_tile()

    def _supports_analog_learning(self) -> bool:
        return supports_analog_learning(self.device)

    def device_layout_kind(self) -> str:
        return device_layout_kind(self.device)

    def _mapping_action_summary_chars(self) -> int:
        return mapping_action_summary_chars(self.device_layout_kind())

    def _on_analog_mapping_clicked(
        self,
        _button_widget: Gtk.Button,
        analog: AnalogInputDefinition,
    ) -> None:
        self._activate_analog_mapping(analog)

    def _on_analog_name_right_clicked(
        self,
        click,
        n_press,
        x,
        y,
        analog: AnalogInputDefinition,
    ) -> None:
        if n_press != 1 or self.demo_mode:
            return
        self._show_analog_relabel_dialog(analog)

    def _on_mapping_button_clicked(
        self,
        _button_widget: Gtk.Button,
        button: ButtonDefinition,
        protected: bool,
    ) -> None:
        self._activate_mapping_button(button, protected)

    def _on_button_clicked(
        self,
        click,
        n_press,
        x,
        y,
        button: ButtonDefinition,
        protected: bool,
    ) -> None:
        if click.get_current_button() != Gdk.BUTTON_PRIMARY:
            return

        self._activate_mapping_button(button, protected)

    def _activate_mapping_button(self, button: ButtonDefinition, protected: bool) -> None:
        if self._selected_profile is None:
            self._show_no_profile_dialog()
            return

        if protected:
            self._show_protected_remap_warning_dialog(button)
            return

        self._show_function_editor(button)

    def _activate_analog_mapping(self, analog: AnalogInputDefinition) -> None:
        if self._selected_profile is None:
            self._show_no_profile_dialog()
            return
        self._show_analog_editor(analog)

    def _on_action_label_right_clicked(
        self, click, n_press, x, y, button: ButtonDefinition
    ) -> None:
        if n_press != 1:
            return
        layer = self._selected_layer()
        if self._selected_profile is None:
            return

        mapping = layer.mappings.get(button.id) if layer else None
        if not mapping or mapping.action_type != ActionType.MACRO or not mapping.macro_name:
            return
        macro_name = mapping.macro_name

        def on_macro_loaded(result: JsonDict | None) -> bool:
            return self._on_macro_lookup(result, macro_name, button)

        _session_request_async(
            {"command": "get_macro", "name": macro_name},
            on_macro_loaded,
        )

    def _on_macro_lookup(
        self,
        result: JsonDict | None,
        macro_name: str,
        button: ButtonDefinition,
    ) -> bool:
        macro = (result or {}).get("macro")
        if (result or {}).get("status") != "ok" or not isinstance(macro, dict):
            self._show_function_editor(button)
            return False

        from keymasq.gui.widgets.macro_editor_dialog import MacroEditorDialog

        dialog = MacroEditorDialog(self.get_root(), macro_name)
        dialog.present(self.get_root())
        return False

    def _on_name_label_right_clicked(self, click, n_press, x, y, button: ButtonDefinition) -> None:
        if n_press != 1 or self.demo_mode:
            return
        self._show_relabel_dialog(button)

    def _show_relabel_dialog(self, button: ButtonDefinition) -> None:
        if self.hardware_manager is None:
            return
        rename_dialogs.present_button_relabel_dialog(
            parent=self.get_root(),
            button=button,
            on_delete_clicked=self._on_delete_button_clicked,
            on_save=self._rename_button_label,
            on_close_clicked=self._on_close_dialog_clicked,
        )

    def _rename_button_label(self, button: ButtonDefinition, new_label: str) -> bool:
        new_label = new_label.strip()
        if not new_label:
            return False
        if self.hardware_manager is None:
            log.warning("Cannot rename button %s without a hardware manager", button.id)
            return False
        for item in self.device.buttons:
            if item.id == button.id:
                item.label = new_label
                break
        self.hardware_manager.save_hardware(self.device)
        _session_request_async({"command": "reload"}, lambda _result: False)
        widget = self._button_widgets.get(button.id)
        if widget:
            widget._name_label.set_text(new_label)
        return True

    def _show_analog_relabel_dialog(self, analog: AnalogInputDefinition) -> None:
        if self.hardware_manager is None:
            return
        rename_dialogs.present_analog_relabel_dialog(
            parent=self.get_root(),
            analog=analog,
            on_delete_clicked=self._on_delete_analog_clicked,
            on_save=self._rename_analog_label,
            on_close_clicked=self._on_close_dialog_clicked,
        )

    def _rename_analog_label(self, analog: AnalogInputDefinition, new_label: str) -> bool:
        new_label = new_label.strip()
        if not new_label:
            return False
        if self.hardware_manager is None:
            log.warning("Cannot rename analog input %s without a hardware manager", analog.id)
            return False
        for item in self.device.analog_inputs:
            if item.id == analog.id:
                item.label = new_label
                break
        self.hardware_manager.save_hardware(self.device)
        _session_request_async({"command": "reload"}, lambda _result: False)
        widget = self._button_widgets.get(analog.id)
        if widget:
            widget._name_label.set_text(new_label)
        return True

    def _on_delete_button_clicked(
        self,
        _button: Gtk.Button,
        dialog: Adw.Dialog,
        button: ButtonDefinition,
    ) -> None:
        self._delete_button(button, dialog)

    def _delete_button(self, button: ButtonDefinition, dialog: Adw.Dialog) -> None:
        if self.hardware_manager is None:
            log.warning("Cannot delete button %s without a hardware manager", button.id)
            dialog.close()
            return
        original_count = len(self.device.buttons)
        self.device.buttons = [
            existing for existing in self.device.buttons if existing.id != button.id
        ]
        if len(self.device.buttons) == original_count:
            dialog.close()
            return

        if self.profile_manager is not None:
            self.profile_manager.remove_device_button_mappings(self.device.hardware_id, button.id)

        self.hardware_manager.save_hardware(self.device)
        _session_request_async({"command": "reload"}, lambda _result: False)
        dialog.close()
        if self.profile_manager is not None:
            self._reload_ui()

    def _on_delete_analog_clicked(
        self,
        _button: Gtk.Button,
        dialog: Adw.Dialog,
        analog: AnalogInputDefinition,
    ) -> None:
        self._delete_analog(analog, dialog)

    def _delete_analog(self, analog: AnalogInputDefinition, dialog: Adw.Dialog) -> None:
        if self.hardware_manager is None:
            log.warning("Cannot delete analog input %s without a hardware manager", analog.id)
            dialog.close()
            return
        original_count = len(self.device.analog_inputs)
        self.device.analog_inputs = [
            existing for existing in self.device.analog_inputs if existing.id != analog.id
        ]
        if len(self.device.analog_inputs) == original_count:
            dialog.close()
            return

        if self.profile_manager is not None:
            self.profile_manager.remove_device_button_mappings(self.device.hardware_id, analog.id)

        self.hardware_manager.save_hardware(self.device)
        _session_request_async({"command": "reload"}, lambda _result: False)
        dialog.close()
        if self.profile_manager is not None:
            self._reload_ui()

    def _show_protected_remap_warning_dialog(self, button: ButtonDefinition) -> None:
        dialog = Adw.AlertDialog(
            heading="Remap Critical Mouse Button?",
            body=(
                f"{button.label} is a critical pointer button. Remapping it can remove "
                "your normal left or right click <b>everywhere</b>.\n\n"
                "Continue only if you have a reliable recovery path, such as another "
                "mouse, keyboard navigation, or direct access to the profile files."
            ),
        )
        dialog.set_body_use_markup(True)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("continue", "Continue")
        dialog.set_response_appearance("continue", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.connect("response", self._on_protected_remap_response, button)
        dialog.present(self.get_root())

    def _on_protected_remap_response(
        self,
        _dialog: Adw.AlertDialog,
        response: str,
        button: ButtonDefinition,
    ) -> None:
        if response == "continue":
            self._show_function_editor(button)

    def _show_no_profile_dialog(self) -> None:
        dialog = Adw.AlertDialog(
            heading="No Profile Selected",
            body="Select or create a profile first to edit button mappings.",
        )
        dialog.add_response("ok", "OK")
        dialog.present(self.get_root())

    def _show_profile_error_dialog(self, message: str) -> None:
        dialog = Adw.AlertDialog(
            heading="Invalid Profile Configuration",
            body=message,
        )
        dialog.add_response("ok", "OK")
        dialog.present(self.get_root())

    def _show_function_editor(self, button: ButtonDefinition) -> None:
        current_action = None
        layer = self._selected_layer()
        if layer:
            current_action = layer.mappings.get(button.id)

        def on_key_selected(dialog, action):
            layer = self._selected_layer(create=True)
            if layer is None:
                return
            if action is None:
                if button.id in layer.mappings:
                    del layer.mappings[button.id]
            else:
                layer.mappings[button.id] = action
            self._save_profile()
            self._update_button_display(button.id)
            self._update_header_caption()

        dialog = KeySelectorDialog(self, button.label, current_action)
        dialog.connect("key-selected", on_key_selected)
        dialog.present(self.get_root())

    def _show_analog_editor(self, analog: AnalogInputDefinition) -> None:
        current_action = None
        layer = self._selected_layer()
        if layer:
            current_action = layer.mappings.get(analog.id)

        def on_key_selected(dialog, action):
            layer = self._selected_layer(create=True)
            if layer is None:
                return
            if action is None:
                layer.mappings.pop(analog.id, None)
            else:
                layer.mappings[analog.id] = action
            self._save_profile()
            self._update_button_display(analog.id)
            self._update_header_caption()

        dialog = KeySelectorDialog(
            self,
            analog.label,
            current_action,
            allow_rapidfire=False,
            allow_tap=False,
            allow_macro_options=False,
            source_type="analog",
            analog_input_type=analog.type,
        )
        dialog.connect("key-selected", on_key_selected)
        dialog.present(self.get_root())

    def _profile_info_by_name(self, profile_name: str) -> ProfileInfo | None:
        return mapping_display.profile_info_by_name(
            self.profile_manager,
            self.profiles,
            profile_name,
        )

    def _get_effective_mapping_for_button(
        self, button_id: str
    ) -> tuple[str | None, MappingAction | None]:
        return mapping_display.get_effective_mapping_for_button(
            active_profile_names=self._active_profile_names,
            profile_lookup=self._profile_info_by_name,
            hardware_id=self.device.hardware_id,
            button_id=button_id,
        )

    def _describe_mapping(
        self,
        mapping: MappingAction,
        button: ButtonDefinition | None = None,
    ) -> str:
        return mapping_display.describe_mapping(
            mapping,
            describe_passthrough=self._describe_passthrough_output,
            button=button,
        )

    def _describe_passthrough_output(self, button: ButtonDefinition) -> str:
        return mapping_display.describe_passthrough_output(
            button,
            label_from_evdev=self._label_from_evdev,
        )

    def _set_action_label_text(self, label: Gtk.Label, text: str) -> None:
        mapping_display.set_action_label_text(
            label,
            text,
            max_chars=self._mapping_action_summary_chars(),
        )

    def _update_button_display(self, button_id: str) -> None:
        mapping_display.update_button_display(
            button_widgets=self._button_widgets,
            button_id=button_id,
            device=self.device,
            selected_layer=self._selected_layer(),
            selected_profile=self._selected_profile,
            effective_mapping=self._get_effective_mapping_for_button(button_id),
            describe_mapping_for_button=self._describe_mapping,
            describe_passthrough=self._describe_passthrough_output,
            action_summary_chars=self._mapping_action_summary_chars(),
        )

    def _on_add_keys_clicked(self, _button: Gtk.Button | None) -> None:
        AddInputsFlow(
            self.main_window or self.get_root(),
            _session_request_async,
            self.device,
            self._on_add_inputs_complete,
        ).present()

    def _on_add_inputs_complete(self, result: AddInputsResult) -> None:
        if self.hardware_manager is None:
            log.warning(
                "Cannot save added inputs for device %s without a hardware manager",
                self.device.hardware_id,
            )
            return
        self.device.buttons.extend(result.buttons)
        self.device.evdev_devices.extend(result.evdev_devices)
        self.hardware_manager.save_hardware(self.device)
        _session_request_async({"command": "reload"}, self._ignore_session_response)
        self._reload_ui()

    def _on_learn_analog_clicked(self, _button: Gtk.Button | None) -> None:
        LearnAnalogFlow(
            self.main_window or self.get_root(),
            _session_request_async,
            self.device,
            self._on_learn_analog_complete,
        ).present()

    def _on_learn_analog_complete(self, result: LearnAnalogResult) -> None:
        if self.hardware_manager is None:
            log.warning(
                "Cannot save learned analog input for device %s without a hardware manager",
                self.device.hardware_id,
            )
            return
        self.device.analog_inputs.append(result.analog)
        self._ensure_analog_evdev_interface(result.source, result.stable_path)
        self.hardware_manager.save_hardware(self.device)
        _session_request_async({"command": "reload"}, self._ignore_session_response)
        self._reload_ui()

    def _on_close_dialog_clicked(self, _button: Gtk.Button, dialog: Adw.Dialog) -> None:
        dialog.close()

    def _ignore_session_response(self, _response: JsonDict | None) -> bool:
        return False

    def _ensure_analog_evdev_interface(
        self,
        source: str | None,
        stable_path: str | None,
    ) -> None:
        if not source or not stable_path:
            return
        for dev in self.device.evdev_devices:
            if dev.id == source:
                return
            if dev.path == stable_path:
                if not dev.id:
                    dev.id = source
                return
        self.device.evdev_devices.append(
            EvdevDevice(path=stable_path, device_type=DeviceType.GAMEPAD, id=source)
        )

    def _label_from_evdev(self, evdev_name: str) -> str:
        return label_from_evdev(evdev_name)

    def _reload_ui(self) -> None:
        selected_name = self._selected_profile.config.name if self._selected_profile else None
        selected_name = self._window_selected_profile_name() or selected_name
        assert self.profile_manager is not None
        self.profiles = self.profile_manager.list_profiles()
        while child := self.get_first_child():
            self.remove(child)
        self._button_widgets = {}
        self._setup_header()
        self._setup_profile_selector()
        self._setup_button_grid()
        if selected_name:
            for i, name in enumerate(self._profile_names):
                if name == selected_name:
                    self.profile_dropdown.set_selected(i)
                    break
        self._apply_profile_selection()
