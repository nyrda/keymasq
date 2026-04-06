import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import json
import threading
from collections.abc import Callable

from gi.repository import Adw, GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keyforge.common.devices import input_class_label, normalize_input_classes
from keyforge.common.paths import CONFIG_DIR, ensure_config_dirs
from keyforge.gui.session_client import session_request


class RecordMacroDialog(Adw.Dialog):
    _SETTINGS_PATH = CONFIG_DIR / "recording_settings.json"

    def __init__(self, parent: Gtk.Window, on_saved: Callable | None = None):
        super().__init__(title="Macro Recording Settings", content_width=480)
        self._parent = parent
        self._on_saved = on_saved
        self._devices: list[dict] = []
        self._device_checks: dict[str, Gtk.CheckButton] = {}
        self._record_mouse_movement = False
        self._record_mouse_clicks = False
        self._record_start_position = False
        self._record_keyboard = True
        self._record_mouse = False
        self._record_gamepad = True
        self._device_overrides: dict[str, bool] = {}
        self._recording_unlocked = False
        self._recording_unlock_required = True
        self._recording_refresh_owner = False
        self._load_settings()
        self._build_ui()
        self._load_initial_state_async()

    def _build_ui(self) -> None:
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main_box.set_margin_top(12)
        main_box.set_margin_bottom(12)
        main_box.set_margin_start(12)
        main_box.set_margin_end(12)

        frame = Gtk.Frame()
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        title_label = Gtk.Label(label="Macro Recording Settings")
        title_label.add_css_class("title-3")
        title_label.set_halign(Gtk.Align.CENTER)
        title_label.set_margin_top(12)
        title_label.set_margin_bottom(12)
        inner.append(title_label)
        inner.append(Gtk.Separator())

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(16)
        content.set_margin_end(16)

        intro_label = Gtk.Label(
            label=(
                "Record from Keyforge output devices to capture remapped events. "
                "Managed physical devices are shown below for reference."
            )
        )
        intro_label.set_wrap(True)
        intro_label.set_halign(Gtk.Align.START)
        intro_label.add_css_class("dim-label")
        content.append(intro_label)

        cat_label = Gtk.Label(label="Event classes to include:")
        cat_label.add_css_class("heading")
        cat_label.set_halign(Gtk.Align.START)
        content.append(cat_label)

        cat_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        cat_row.set_halign(Gtk.Align.START)

        self._keyboard_check = Gtk.CheckButton(label="Keyboards")
        self._keyboard_check.set_active(self._record_keyboard)
        self._keyboard_check.connect("toggled", self._on_category_toggled, "keyboard")
        cat_row.append(self._keyboard_check)

        self._mouse_check = Gtk.CheckButton(label="Mice")
        self._mouse_check.set_active(self._record_mouse)
        self._mouse_check.connect("toggled", self._on_category_toggled, "mouse")
        cat_row.append(self._mouse_check)

        self._gamepad_check = Gtk.CheckButton(label="Gamepads")
        self._gamepad_check.set_active(self._record_gamepad)
        self._gamepad_check.connect("toggled", self._on_category_toggled, "gamepad")
        cat_row.append(self._gamepad_check)

        content.append(cat_row)

        options_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        options_row.set_halign(Gtk.Align.START)

        self._record_movement_check = Gtk.CheckButton(label="Record mouse movement")
        self._record_movement_check.set_active(self._record_mouse_movement)
        self._record_movement_check.connect("toggled", self._on_record_options_changed)
        options_row.append(self._record_movement_check)

        self._record_clicks_check = Gtk.CheckButton(label="Record mouse clicks")
        self._record_clicks_check.set_active(self._record_mouse_clicks)
        self._record_clicks_check.connect("toggled", self._on_record_options_changed)
        options_row.append(self._record_clicks_check)

        content.append(options_row)

        start_pos_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        start_pos_row.set_halign(Gtk.Align.START)

        self._record_start_pos_check = Gtk.CheckButton(
            label="Record initial mouse position (for 'move to start' playback)"
        )
        self._record_start_pos_check.set_active(self._record_start_position)
        self._record_start_pos_check.connect("toggled", self._on_record_options_changed)
        start_pos_row.append(self._record_start_pos_check)

        content.append(start_pos_row)

        devices_label = Gtk.Label(label="Recording sources:")
        devices_label.add_css_class("heading")
        devices_label.set_halign(Gtk.Align.START)
        content.append(devices_label)

        sources_help = Gtk.Label(
            label=(
                "Recommended sources are Keyforge virtual outputs. "
                "Direct sources capture raw input. "
                "Managed devices are already routed through Keyforge."
            )
        )
        sources_help.set_wrap(True)
        sources_help.set_halign(Gtk.Align.START)
        sources_help.add_css_class("dim-label")
        content.append(sources_help)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_height(280)
        scrolled.set_max_content_height(420)

        self._device_listbox = Gtk.ListBox()
        self._device_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._device_listbox.add_css_class("boxed-list")
        scrolled.set_child(self._device_listbox)
        content.append(scrolled)

        self._loading_label = Gtk.Label(label="Loading devices...")
        self._loading_label.add_css_class("dim-label")
        content.append(self._loading_label)

        inner.append(content)
        inner.append(Gtk.Separator())

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_margin_top(8)
        footer.set_margin_bottom(8)
        footer.set_margin_start(12)
        footer.set_margin_end(12)

        self._unlock_btn = Gtk.Button(label="Unlock")
        self._unlock_btn.connect("clicked", self._on_unlock_clicked)
        footer.append(self._unlock_btn)

        self._unlock_status = Gtk.Label(label="Unlock required")
        self._unlock_status.add_css_class("dim-label")
        self._unlock_status.set_hexpand(True)
        self._unlock_status.set_halign(Gtk.Align.START)
        footer.append(self._unlock_status)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.close())
        footer.append(cancel_btn)

        self._save_btn = Gtk.Button(label="Save Settings")
        self._save_btn.add_css_class("suggested-action")
        self._save_btn.connect("clicked", self._on_save_settings)
        footer.append(self._save_btn)

        inner.append(footer)
        frame.set_child(inner)
        main_box.append(frame)
        self.set_child(main_box)

    def _load_initial_state_async(self) -> None:
        self._loading_label.set_visible(True)

        def worker() -> None:
            self._sync_settings_to_session()
            devices_result = session_request({"command": "list_devices_for_recording"})
            settings_result = session_request({"command": "get_recording_settings"})
            GLib.idle_add(self._apply_initial_state, devices_result, settings_result)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_initial_state(
        self,
        devices_result: dict | None,
        settings_result: dict | None,
    ) -> bool:
        self._devices = (devices_result or {}).get("devices", [])
        self._loading_label.set_visible(False)
        self._populate_device_list()
        self._apply_unlock_state(settings_result)
        return False

    def _populate_device_list(self) -> None:
        while self._device_listbox.get_first_child():
            self._device_listbox.remove(self._device_listbox.get_first_child())
        self._device_checks.clear()

        selected_types = self._get_selected_types()

        if not self._devices:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            lbl = Gtk.Label(label="No devices found")
            lbl.add_css_class("dim-label")
            lbl.set_margin_top(8)
            lbl.set_margin_bottom(8)
            row.set_child(lbl)
            self._device_listbox.append(row)
            return

        recommended = [
            device for device in self._devices if self._is_keyforge_output_device(device)
        ]
        direct = [
            device
            for device in self._devices
            if not self._is_keyforge_output_device(device)
            and not device.get("grabbed_by_keyforge", False)
        ]
        managed = [
            device
            for device in self._devices
            if device.get("grabbed_by_keyforge", False)
            and not self._is_keyforge_output_device(device)
        ]

        self._append_device_section(
            "Recommended: Remapped Output",
            "Use these to record what Keyforge emits after remapping.",
            recommended,
            selected_types,
            selectable=True,
        )
        self._append_device_section(
            "Direct Input Sources",
            "Use these to capture raw events before remapping.",
            direct,
            selected_types,
            selectable=True,
        )
        self._append_device_section(
            "Managed by Keyforge",
            (
                "These physical devices are grabbed by Keyforge. "
                "Record their matching Keyforge output above."
            ),
            managed,
            selected_types,
            selectable=False,
        )

        self._update_unlock_ui()

    def _append_device_section(
        self,
        title: str,
        description: str,
        devices: list[dict],
        selected_types: set[str],
        *,
        selectable: bool,
    ) -> None:
        if not devices:
            return

        header_row = Gtk.ListBoxRow()
        header_row.set_selectable(False)
        header_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        header_box.set_margin_top(10)
        header_box.set_margin_bottom(4)
        header_box.set_margin_start(12)
        header_box.set_margin_end(12)

        title_label = Gtk.Label(label=title)
        title_label.set_halign(Gtk.Align.START)
        title_label.add_css_class("heading")
        header_box.append(title_label)

        desc_label = Gtk.Label(label=description)
        desc_label.set_wrap(True)
        desc_label.set_halign(Gtk.Align.START)
        desc_label.add_css_class("dim-label")
        desc_label.add_css_class("caption")
        header_box.append(desc_label)

        header_row.set_child(header_box)
        self._device_listbox.append(header_row)

        for device in devices:
            self._device_listbox.append(
                self._build_device_row(device, selected_types, selectable=selectable)
            )

    def _build_device_row(
        self,
        device: dict,
        selected_types: set[str],
        *,
        selectable: bool,
    ) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_selectable(False)

        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row_box.set_margin_top(6)
        row_box.set_margin_bottom(6)
        row_box.set_margin_start(12)
        row_box.set_margin_end(12)

        path = str(device.get("path", ""))
        if selectable:
            check = Gtk.CheckButton()
            if path in self._device_overrides:
                check.set_active(bool(self._device_overrides[path]))
            else:
                check.set_active(self._device_matches_selected_types(device, selected_types))
            check.connect("toggled", self._on_device_check_toggled)
            self._device_checks[path] = check
            row_box.append(check)
        else:
            status_icon = Gtk.Image.new_from_icon_name("dialog-information-symbolic")
            status_icon.add_css_class("dim-label")
            row_box.append(status_icon)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_box.set_hexpand(True)

        name_label = Gtk.Label(label=device.get("name", path))
        name_label.set_halign(Gtk.Align.START)
        if not selectable:
            name_label.add_css_class("dim-label")
        info_box.append(name_label)

        detail_parts = [self._device_type_text(device)]
        if self._is_keyforge_output_device(device):
            detail_parts.append("Keyforge output")
        elif device.get("grabbed_by_keyforge", False):
            detail_parts.append("Managed physical device")
        else:
            detail_parts.append("Direct physical device")
        detail_parts.append(path)

        details_label = Gtk.Label(label=" · ".join(detail_parts))
        details_label.set_wrap(True)
        details_label.set_halign(Gtk.Align.START)
        details_label.add_css_class("caption")
        details_label.add_css_class("dim-label")
        info_box.append(details_label)
        row_box.append(info_box)

        badge_label = Gtk.Label(label=self._device_badge_text(device, selectable))
        badge_label.add_css_class("caption")
        badge_label.add_css_class("dim-label")
        row_box.append(badge_label)

        tooltip = self._device_tooltip_text(device)
        row.set_tooltip_text(tooltip)
        row_box.set_tooltip_text(tooltip)
        info_box.set_tooltip_text(tooltip)
        name_label.set_tooltip_text(tooltip)
        details_label.set_tooltip_text(tooltip)
        badge_label.set_tooltip_text(tooltip)

        row.set_child(row_box)
        return row

    def _is_keyforge_output_device(self, device: dict) -> bool:
        name = str(device.get("name", "") or "").lower()
        return name.startswith("keyforge-")

    def _device_tooltip_text(self, device: dict) -> str:
        name = str(device.get("name", "") or "")
        lowered = name.lower()
        if lowered in {"keyforge-keyboard", "keyforge-mouse", "keyforge-gamepad"}:
            return "Synthetic Keyforge events."
        if lowered.startswith("keyforge-"):
            source_name = self._find_passthrough_source_name(device)
            if source_name:
                return f"Passthrough from {source_name}."
            return "Passthrough from a managed device."
        if device.get("grabbed_by_keyforge", False):
            return (
                "Managed physical device. "
                "Record its matching Keyforge output to capture remapped events."
            )
        return "Direct physical input source."

    def _find_passthrough_source_name(self, output_device: dict) -> str | None:
        vendor_id = str(output_device.get("vendor_id", "") or "")
        product_id = str(output_device.get("product_id", "") or "")
        device_type = str(output_device.get("device_type", "") or "")
        for device in self._devices:
            if not device.get("grabbed_by_keyforge", False):
                continue
            if str(device.get("vendor_id", "") or "") != vendor_id:
                continue
            if str(device.get("product_id", "") or "") != product_id:
                continue
            if device_type not in self._device_types(device):
                continue
            return str(device.get("name", "") or "")
        return None

    def _device_badge_text(self, device: dict, selectable: bool) -> str:
        name = str(device.get("name", "") or "").lower()
        if name in {"keyforge-keyboard", "keyforge-mouse", "keyforge-gamepad"}:
            return "Recommended"
        if self._is_keyforge_output_device(device):
            return "Passthrough"
        if device.get("grabbed_by_keyforge", False):
            return "Managed"
        if selectable:
            return "Raw"
        return ""

    def _get_selected_types(self) -> set[str]:
        types = set()
        if self._keyboard_check.get_active():
            types.add("keyboard")
        if self._mouse_check.get_active():
            types.add("mouse")
        if self._gamepad_check.get_active():
            types.add("gamepad")
        return types

    def _on_category_toggled(self, check: Gtk.CheckButton, device_type: str) -> None:
        active = check.get_active()

        if device_type == "keyboard":
            self._record_keyboard = active
        elif device_type == "mouse":
            self._record_mouse = active
        elif device_type == "gamepad":
            self._record_gamepad = active

        for device in self._devices:
            if (
                device_type in self._device_types(device)
                and not device.get("grabbed_by_keyforge", False)
            ):
                dev_check = self._device_checks.get(device["path"])
                if dev_check:
                    dev_check.handler_block_by_func(self._on_device_check_toggled)
                    dev_check.set_active(active)
                    dev_check.handler_unblock_by_func(self._on_device_check_toggled)
                    self._device_overrides[device["path"]] = active

        self._save_settings_async()

    def _on_record_options_changed(self, check: Gtk.CheckButton) -> None:
        self._record_mouse_movement = self._record_movement_check.get_active()
        self._record_mouse_clicks = self._record_clicks_check.get_active()
        self._record_start_position = self._record_start_pos_check.get_active()
        self._save_settings_async()
        threading.Thread(target=self._sync_settings_to_session, daemon=True).start()

    def _refresh_unlock_state(self) -> None:
        result = session_request({"command": "get_recording_settings"})
        self._apply_unlock_state(result)

    def _apply_unlock_state(self, result: dict | None) -> None:
        result = result or {}
        self._recording_unlock_required = bool(result.get("recording_unlock_required", True))
        self._recording_unlocked = bool(
            result.get("recording_unlocked", False)
        ) or not self._recording_unlock_required
        self._recording_refresh_owner = bool(result.get("recording_refresh_owner", False))
        if not self._recording_unlock_required:
            self._unlock_status.set_label("Unlock not required")
            self._unlock_status.remove_css_class("success")
            self._unlock_status.remove_css_class("error")
        elif self._recording_unlocked and self._recording_refresh_owner:
            self._unlock_status.set_label("Unlock active")
            self._unlock_status.remove_css_class("error")
            self._unlock_status.add_css_class("success")
        elif self._recording_unlocked:
            self._unlock_status.set_label("Unlock active in another session")
            self._unlock_status.remove_css_class("success")
            self._unlock_status.add_css_class("error")
        else:
            self._unlock_status.set_label("Unlock required")
            self._unlock_status.remove_css_class("success")
            self._unlock_status.remove_css_class("error")

        self._update_unlock_ui()

    def _on_unlock_clicked(self, _btn: Gtk.Button) -> None:
        present_unlock = getattr(self._parent, "present_unlock_dialog", None)
        if callable(present_unlock):
            present_unlock(on_success=self._refresh_unlock_state_async)
            return
        self._show_error("Unlock is only available from the main window")

    def _refresh_unlock_state_async(self) -> None:
        threading.Thread(target=self._refresh_unlock_state, daemon=True).start()

    def _on_device_check_toggled(self, check: Gtk.CheckButton) -> None:
        for path, dev_check in self._device_checks.items():
            if dev_check is check:
                self._device_overrides[path] = check.get_active()
                break
        self._save_settings_async()

    def _update_unlock_ui(self) -> None:
        if not self._recording_unlock_required:
            self._unlock_btn.set_visible(False)
            return

        has_active_unlock = self._recording_unlocked and self._recording_refresh_owner
        self._unlock_btn.set_visible(not has_active_unlock)
        self._unlock_btn.set_label("Claim Unlock" if self._recording_unlocked else "Unlock")

    def _device_types(self, device: dict) -> list[str]:
        return normalize_input_classes(
            device.get("device_types"),
            device.get("device_type", "other"),
        )

    def _device_matches_selected_types(self, device: dict, selected_types: set[str]) -> bool:
        return bool(selected_types.intersection(self._device_types(device)))

    def _device_type_text(self, device: dict) -> str:
        return " / ".join(input_class_label(dtype) for dtype in self._device_types(device))

    def _get_selected_device_types(self) -> list[str]:
        return list(self._get_selected_types())

    def _on_save_settings(self, btn: Gtk.Button) -> None:
        self._save_btn.set_sensitive(False)

        def worker() -> None:
            self._save_settings()
            result = session_request(
                {
                    "command": "set_recording_settings",
                    "include_mouse_movement": self._record_mouse_movement,
                    "include_mouse_clicks": self._record_mouse_clicks,
                    "record_start_position": self._record_start_position,
                    "record_keyboard": self._record_keyboard,
                    "record_mouse": self._record_mouse,
                    "record_gamepad": self._record_gamepad,
                    "device_overrides": self._device_overrides,
                },
                timeout=2.0,
            )
            GLib.idle_add(self._on_save_settings_done, result)

        threading.Thread(target=worker, daemon=True).start()

    def _on_save_settings_done(self, result: dict | None) -> bool:
        self._save_btn.set_sensitive(True)
        if result and result.get("status") == "ok":
            if self._on_saved:
                self._on_saved()
            self.close()
            return False

        msg = (result or {}).get("message", "Failed to save recording settings")
        self._show_error(msg)
        return False

    def _sync_settings_to_session(self) -> None:
        session_request(
            {
                "command": "set_recording_settings",
                "include_mouse_movement": self._record_mouse_movement,
                "include_mouse_clicks": self._record_mouse_clicks,
                "record_start_position": self._record_start_position,
                "record_keyboard": self._record_keyboard,
                "record_mouse": self._record_mouse,
                "record_gamepad": self._record_gamepad,
                "device_overrides": self._device_overrides,
            },
            timeout=0.5,
        )

    def _load_settings(self) -> None:
        try:
            if self._SETTINGS_PATH.exists():
                data = json.loads(self._SETTINGS_PATH.read_text())
                self._record_mouse_movement = bool(data.get("include_mouse_movement", False))
                self._record_mouse_clicks = bool(data.get("include_mouse_clicks", False))
                self._record_start_position = bool(data.get("record_start_position", False))
                self._record_keyboard = bool(data.get("record_keyboard", True))
                self._record_mouse = bool(data.get("record_mouse", False))
                self._record_gamepad = bool(data.get("record_gamepad", True))
                overrides = data.get("device_overrides", {})
                if isinstance(overrides, dict):
                    self._device_overrides = {str(k): bool(v) for k, v in overrides.items()}
        except Exception:
            pass

    def _save_settings(self) -> None:
        try:
            ensure_config_dirs()
            self._SETTINGS_PATH.write_text(
                json.dumps(
                    {
                        "include_mouse_movement": self._record_mouse_movement,
                        "include_mouse_clicks": self._record_mouse_clicks,
                        "record_start_position": self._record_start_position,
                        "record_keyboard": self._record_keyboard,
                        "record_mouse": self._record_mouse,
                        "record_gamepad": self._record_gamepad,
                        "device_overrides": self._device_overrides,
                    }
                )
            )
        except Exception:
            pass

    def _save_settings_async(self) -> None:
        threading.Thread(target=self._save_settings, daemon=True).start()

    def _show_error(self, message: str) -> None:
        dialog = Adw.AlertDialog()
        dialog.set_heading("Error")
        dialog.set_body(message)
        dialog.add_response("ok", "OK")
        dialog.present(self._parent)
