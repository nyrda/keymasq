import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import logging
import threading
from collections.abc import Callable

from gi.repository import Adw, GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq import __version__
from keymasq.common.devices import input_class_label, normalize_input_classes
from keymasq.gui.session_client import session_request
from keymasq.gui.widgets.docs_links import docs_page_url

log = logging.getLogger("keymasq.gui.widgets.record_macro_dialog")


def _macro_recording_docs_url() -> str:
    return docs_page_url("MACROS", anchor="live-recording", version=__version__)


class RecordMacroDialog(Adw.Dialog):
    def __init__(
        self,
        parent: Gtk.Window,
        on_saved: Callable | None = None,
        reason: str = "settings",
    ):
        super().__init__(title="Macro Recording Settings", content_width=480)
        self._parent = parent
        self._on_saved = on_saved
        self._reason = reason
        self._devices: list[dict] = []
        self._device_checks: dict[str, Gtk.CheckButton] = {}
        self._record_mouse_movement = False
        self._record_mouse_clicks = False
        self._record_start_position = False
        self._device_overrides: dict[str, bool] = {}
        self._recording_unlocked = False
        self._recording_unlock_required = True
        self._recording_refresh_owner = False
        self._macro_recording_enabled = False
        self._macro_recording_source = "none"
        self._macro_recording_expires_at = 0
        self._settings_loaded = False
        self._applying_settings = False
        self._settings_sync_lock = threading.Lock()
        self._settings_sync_generation = 0
        self._settings_sync_worker_running = False
        self._closed = False
        self._build_ui()
        self.set_presentation_reason(reason)
        self._register_parent_events()
        self.connect("closed", self._on_dialog_closed)
        self._load_initial_state_async()

    def _build_ui(self) -> None:
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main_box.set_margin_top(12)
        main_box.set_margin_bottom(12)
        main_box.set_margin_start(12)
        main_box.set_margin_end(12)

        frame = Gtk.Frame()
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self._title_label = Gtk.Label(label="Macro Recording Settings")
        self._title_label.add_css_class("title-3")
        self._title_label.set_halign(Gtk.Align.CENTER)
        self._title_label.set_margin_top(12)
        self._title_label.set_margin_bottom(12)
        inner.append(self._title_label)
        inner.append(Gtk.Separator())

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(16)
        content.set_margin_end(16)

        self._locked_notice = self._build_locked_notice()
        content.append(self._locked_notice)

        access_frame = Gtk.ListBox()
        access_frame.set_selection_mode(Gtk.SelectionMode.NONE)
        access_frame.add_css_class("boxed-list")

        self._macro_recording_row = Adw.ActionRow(title="Macro recording")
        self._macro_recording_row.set_subtitle("Disabled")
        self._macro_recording_toggle_btn = Gtk.Button()
        self._macro_recording_toggle_btn.set_valign(Gtk.Align.CENTER)
        self._macro_recording_toggle_btn.connect(
            "clicked",
            self._on_macro_recording_toggle_clicked,
        )
        self._macro_recording_row.add_suffix(self._macro_recording_toggle_btn)
        access_frame.append(self._macro_recording_row)

        content.append(access_frame)

        # Recording options in a compact boxed list
        options_frame = Gtk.ListBox()
        options_frame.set_selection_mode(Gtk.SelectionMode.NONE)
        options_frame.add_css_class("boxed-list")

        self._record_movement_check = Gtk.CheckButton()
        self._record_movement_check.set_active(self._record_mouse_movement)
        self._record_movement_check.connect("toggled", self._on_record_options_changed)
        movement_row = Adw.ActionRow(title="Record mouse movement")
        movement_row.add_prefix(self._record_movement_check)
        movement_row.set_activatable_widget(self._record_movement_check)
        options_frame.append(movement_row)

        self._record_clicks_check = Gtk.CheckButton()
        self._record_clicks_check.set_active(self._record_mouse_clicks)
        self._record_clicks_check.connect("toggled", self._on_record_options_changed)
        clicks_row = Adw.ActionRow(title="Record mouse clicks")
        clicks_row.add_prefix(self._record_clicks_check)
        clicks_row.set_activatable_widget(self._record_clicks_check)
        options_frame.append(clicks_row)

        self._record_start_pos_check = Gtk.CheckButton()
        self._record_start_pos_check.set_active(self._record_start_position)
        self._record_start_pos_check.connect("toggled", self._on_record_options_changed)
        start_pos_row = Adw.ActionRow(
            title="Record initial mouse position",
            subtitle="Adds an editable first cursor move event",
        )
        start_pos_row.add_prefix(self._record_start_pos_check)
        start_pos_row.set_activatable_widget(self._record_start_pos_check)
        options_frame.append(start_pos_row)

        content.append(options_frame)

        # Quick Selection as a collapsed expander with compact inline controls
        quick_expander = Gtk.Expander(label="Quick Selection")
        quick_expander.set_expanded(False)

        quick_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        quick_box.set_margin_top(8)

        quick_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        quick_row.set_halign(Gtk.Align.START)

        for label, device_type in (
            ("Keyboards", "keyboard"),
            ("Mice", "mouse"),
            ("Gamepads", "gamepad"),
        ):
            type_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)

            type_label = Gtk.Label(label=label)
            type_box.append(type_label)

            select_btn = Gtk.Button()
            select_btn.set_icon_name("object-select-symbolic")
            select_btn.set_tooltip_text(f"Select all {label.lower()}")
            select_btn.add_css_class("flat")
            select_btn.connect("clicked", self._on_select_type_clicked, device_type, True)
            type_box.append(select_btn)

            clear_btn = Gtk.Button()
            clear_btn.set_icon_name("edit-clear-symbolic")
            clear_btn.set_tooltip_text(f"Clear all {label.lower()}")
            clear_btn.add_css_class("flat")
            clear_btn.connect("clicked", self._on_select_type_clicked, device_type, False)
            type_box.append(clear_btn)

            quick_row.append(type_box)

        quick_box.append(quick_row)
        quick_expander.set_child(quick_box)
        content.append(quick_expander)

        sources_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sources_header.set_margin_top(4)

        devices_label = Gtk.Label(label="Recording sources")
        devices_label.add_css_class("heading")
        devices_label.set_halign(Gtk.Align.START)
        sources_header.append(devices_label)

        self._selection_summary = Gtk.Label(label="0 selected")
        self._selection_summary.set_halign(Gtk.Align.START)
        self._selection_summary.add_css_class("caption")
        self._selection_summary.add_css_class("dim-label")
        sources_header.append(self._selection_summary)

        reset_btn = Gtk.Button(label="Reset")
        reset_btn.set_tooltip_text("Reset to recommended sources")
        reset_btn.add_css_class("flat")
        reset_btn.set_hexpand(True)
        reset_btn.set_halign(Gtk.Align.END)
        reset_btn.connect("clicked", self._on_reset_to_recommended_clicked)
        sources_header.append(reset_btn)

        content.append(sources_header)

        self._selection_warning = Gtk.Label(label="")
        self._selection_warning.set_wrap(True)
        self._selection_warning.set_halign(Gtk.Align.START)
        self._selection_warning.add_css_class("caption")
        self._selection_warning.add_css_class("error")
        self._selection_warning.set_visible(False)
        content.append(self._selection_warning)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_min_content_height(200)
        scrolled.set_max_content_height(500)

        self._device_listbox = Gtk.ListBox()
        self._device_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._device_listbox.add_css_class("boxed-list")
        self._device_listbox.connect("row-activated", self._on_device_row_activated)
        scrolled.set_child(self._device_listbox)
        content.append(scrolled)

        self._loading_label = Gtk.Label(label="Loading devices...")
        self._loading_label.add_css_class("dim-label")
        content.append(self._loading_label)

        inner.append(content)
        inner.append(Gtk.Separator())

        footer = Gtk.CenterBox(orientation=Gtk.Orientation.HORIZONTAL)
        footer.set_margin_top(8)
        footer.set_margin_bottom(8)
        footer.set_margin_start(12)
        footer.set_margin_end(12)

        self.recording_docs_btn = Gtk.Button(label="?")
        self.recording_docs_btn.add_css_class("flat")
        self.recording_docs_btn.add_css_class("actions-docs-button")
        self.recording_docs_btn.set_tooltip_text("Open macro recording documentation")
        self.recording_docs_btn.connect("clicked", self._on_recording_docs_clicked)
        footer.set_start_widget(self.recording_docs_btn)

        self._unlock_status = Gtk.Label(label="Save access locked")
        self._unlock_status.add_css_class("dim-label")
        self._unlock_status.set_halign(Gtk.Align.CENTER)
        footer.set_center_widget(self._unlock_status)

        footer_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer_actions.set_halign(Gtk.Align.END)

        self._unlock_btn = Gtk.Button()
        self._unlock_btn.set_child(self._make_unlock_button_content("Unlock"))
        self._unlock_btn.set_tooltip_text(
            "Authorize saving temporary recording slots and protected macro body access."
        )
        self._unlock_btn.connect("clicked", self._on_unlock_clicked)
        footer_actions.append(self._unlock_btn)

        self._save_btn = Gtk.Button(label="Done")
        self._save_btn.add_css_class("suggested-action")
        self._save_btn.set_sensitive(False)
        self._save_btn.set_tooltip_text("Loading recording settings")
        self._save_btn.connect("clicked", self._on_save_settings)
        footer_actions.append(self._save_btn)
        footer.set_end_widget(footer_actions)

        inner.append(footer)
        frame.set_child(inner)
        main_box.append(frame)
        self.set_child(main_box)
        self._update_macro_recording_ui()

    def _build_locked_notice(self) -> Gtk.Box:
        notice = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        notice.add_css_class("recording-locked-notice")
        notice.set_margin_bottom(2)
        notice.set_visible(False)

        icon = Gtk.Image.new_from_icon_name("channel-insecure-symbolic")
        icon.set_valign(Gtk.Align.START)
        notice.append(icon)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True)

        self._locked_notice_title = Gtk.Label(label="Saving needs unlock")
        self._locked_notice_title.add_css_class("heading")
        self._locked_notice_title.set_halign(Gtk.Align.START)
        text_box.append(self._locked_notice_title)

        body = Gtk.Label(
            label=(
                "Temporary slots can be recorded after macro recording is enabled. "
                "Saving a slot as a persistent macro still requires unlock."
            )
        )
        body.set_wrap(True)
        body.set_halign(Gtk.Align.START)
        text_box.append(body)

        notice.append(text_box)
        return notice

    def set_presentation_reason(self, reason: str = "settings") -> None:
        self._reason = reason
        locked = reason == "recording_locked"
        title = "Macro Recording Settings"
        self.set_title(title)
        self._title_label.set_label(title)
        self._locked_notice.set_visible(locked)

    def _on_recording_docs_clicked(self, _button: Gtk.Button) -> None:
        url = _macro_recording_docs_url()
        try:
            launcher = Gtk.UriLauncher.new(url)
            launcher.launch(None, None, None)
        except Exception:
            log.exception("Could not open macro recording documentation %s", url)

    def _register_parent_events(self) -> None:
        register_event_handler = getattr(self._parent, "register_event_handler", None)
        if callable(register_event_handler):
            register_event_handler("recording_started", self._on_recording_started)

    def _on_dialog_closed(self, _dialog: Adw.Dialog) -> None:
        self._closed = True
        unregister_event_handler = getattr(self._parent, "unregister_event_handler", None)
        if callable(unregister_event_handler):
            unregister_event_handler("recording_started", self._on_recording_started)

    def _on_recording_started(self, _event: dict) -> None:
        self.close()

    def _load_initial_state_async(self) -> None:
        self._loading_label.set_visible(True)

        def worker() -> None:
            devices_result = session_request({"command": "list_devices_for_recording"})
            settings_result = session_request({"command": "get_recording_settings"})
            if self._closed:
                return
            GLib.idle_add(self._apply_initial_state, devices_result, settings_result)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_initial_state(
        self,
        devices_result: dict | None,
        settings_result: dict | None,
    ) -> bool:
        if self._closed:
            return False
        settings_loaded = (
            isinstance(settings_result, dict) and settings_result.get("status") == "ok"
        )
        self._apply_recording_settings(settings_result)
        self._devices = (devices_result or {}).get("devices", [])
        self._populate_device_list()
        self._apply_unlock_state(settings_result)
        self._settings_loaded = settings_loaded
        if settings_loaded:
            self._loading_label.set_visible(False)
            self._save_btn.set_sensitive(True)
            self._save_btn.set_tooltip_text(None)
        else:
            self._loading_label.set_label("Failed to load recording settings")
            self._loading_label.set_visible(True)
        return False

    def _populate_device_list(self) -> None:
        while self._device_listbox.get_first_child():
            self._device_listbox.remove(self._device_listbox.get_first_child())
        self._device_checks.clear()

        if not self._devices:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            lbl = Gtk.Label(label="No devices found")
            lbl.add_css_class("dim-label")
            lbl.set_margin_top(8)
            lbl.set_margin_bottom(8)
            row.set_child(lbl)
            self._device_listbox.append(row)
            self._update_selection_ui()
            return

        recommended = [device for device in self._devices if self._is_recommended_device(device)]
        direct = [device for device in self._devices if self._device_kind(device) == "physical"]
        managed = [
            device for device in self._devices if self._device_kind(device) == "other_virtual"
        ]

        self._append_device_section(
            "Recommended: Remapped Output",
            "Use these to record what Keymasq emits after remapping.",
            recommended,
            selectable=True,
        )
        self._append_device_section(
            "Direct Input Sources",
            (
                "Use these to capture raw events before remapping. "
                "If a matching Keymasq passthrough is also selected, Keymasq records that instead."
            ),
            direct,
            selectable=True,
        )
        self._append_device_section(
            "Other Virtual Sources",
            "Use these only when you explicitly want events from another virtual input source.",
            managed,
            selectable=True,
        )

        self._update_selection_ui()

    def _append_device_section(
        self,
        title: str,
        description: str,
        devices: list[dict],
        *,
        selectable: bool,
    ) -> None:
        if not devices:
            return

        header_row = Gtk.ListBoxRow()
        header_row.set_selectable(False)
        header_row.set_tooltip_text(description)

        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header_box.set_margin_top(6)
        header_box.set_margin_bottom(4)
        header_box.set_margin_start(12)
        header_box.set_margin_end(12)

        title_label = Gtk.Label(label=title)
        title_label.set_halign(Gtk.Align.START)
        title_label.add_css_class("caption")
        title_label.add_css_class("dim-label")
        header_box.append(title_label)

        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        separator.set_hexpand(True)
        separator.set_valign(Gtk.Align.CENTER)
        header_box.append(separator)

        header_row.set_child(header_box)
        self._device_listbox.append(header_row)

        for device in devices:
            self._device_listbox.append(self._build_device_row(device, selectable=selectable))

    def _build_device_row(
        self,
        device: dict,
        *,
        selectable: bool,
    ) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(selectable)

        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row_box.set_margin_top(6)
        row_box.set_margin_bottom(6)
        row_box.set_margin_start(12)
        row_box.set_margin_end(12)

        path = str(device.get("path", ""))
        recording_id = self._device_recording_id(device)
        if selectable:
            check = Gtk.CheckButton()
            check.set_active(self._is_selected_device(device))
            check.connect("toggled", self._on_device_check_toggled)
            self._device_checks[recording_id] = check
            row._recording_id = recording_id  # type: ignore[attr-defined]
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
        kind = self._device_kind(device)
        if kind in {"keymasq_output", "keymasq_passthrough"}:
            detail_parts.append("Keymasq output")
        elif device.get("grabbed_by_keymasq", False):
            detail_parts.append("Managed physical device")
        elif kind == "other_virtual":
            detail_parts.append("Virtual device")
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

    def _on_device_row_activated(
        self,
        _listbox: Gtk.ListBox,
        row: Gtk.ListBoxRow,
    ) -> None:
        recording_id = str(getattr(row, "_recording_id", "") or "")
        if not recording_id:
            return

        check = self._device_checks.get(recording_id)
        if check is None:
            return
        check.set_active(not check.get_active())

    def _device_recording_id(self, device: dict) -> str:
        recording_id = str(device.get("recording_id", "") or "")
        if recording_id:
            return recording_id
        stable_path = str(device.get("stable_path", "") or "")
        if stable_path:
            return f"physical:{stable_path}"
        path = str(device.get("path", "") or "")
        return f"physical:{path}" if path else ""

    def _device_kind(self, device: dict) -> str:
        return str(device.get("recording_kind", "physical") or "physical")

    def _is_recommended_device(self, device: dict) -> bool:
        return self._device_kind(device) in {"keymasq_output", "keymasq_passthrough"}

    def _device_tooltip_text(self, device: dict) -> str:
        kind = self._device_kind(device)
        if kind == "keymasq_output":
            return "Synthetic Keymasq events."
        if kind == "keymasq_passthrough":
            source_name = str(device.get("source_hardware_id", "") or "")
            source_interface = str(device.get("source_interface_id", "") or "")
            if source_name and source_interface:
                return f"Passthrough from {source_name} {source_interface}."
            if source_name:
                return f"Passthrough from {source_name}."
            return "Passthrough from a managed physical device."
        if device.get("grabbed_by_keymasq", False):
            return (
                "Managed physical device. "
                "If its matching Keymasq passthrough is selected, Keymasq records that instead."
            )
        if kind == "other_virtual":
            return "Virtual input source outside Keymasq."
        return "Direct physical input source."

    def _device_badge_text(self, device: dict, selectable: bool) -> str:
        kind = self._device_kind(device)
        if kind in {"keymasq_output", "keymasq_passthrough"}:
            return "Recommended"
        if device.get("grabbed_by_keymasq", False):
            return "Managed"
        if kind == "other_virtual":
            return "Virtual"
        if selectable:
            return "Raw"
        return ""

    def _is_selected_device(self, device: dict) -> bool:
        recording_id = self._device_recording_id(device)
        if recording_id in self._device_overrides:
            return bool(self._device_overrides[recording_id])
        return self._is_recommended_device(device)

    def _find_device_by_recording_id(self, recording_id: str) -> dict | None:
        for device in self._devices:
            if self._device_recording_id(device) == recording_id:
                return device
        return None

    def _store_device_selection(self, device: dict, active: bool) -> None:
        recording_id = self._device_recording_id(device)
        if not recording_id:
            return
        if active == self._is_recommended_device(device):
            self._device_overrides.pop(recording_id, None)
            return
        self._device_overrides[recording_id] = active

    def _set_device_type_selection(self, device_type: str, active: bool) -> None:
        for device in self._devices:
            if device_type not in self._device_types(device):
                continue
            self._store_device_selection(device, active)
            recording_id = self._device_recording_id(device)
            dev_check = self._device_checks.get(recording_id)
            if dev_check:
                dev_check.handler_block_by_func(self._on_device_check_toggled)
                dev_check.set_active(active)
                dev_check.handler_unblock_by_func(self._on_device_check_toggled)

    def _on_select_type_clicked(
        self,
        _btn: Gtk.Button,
        device_type: str,
        active: bool,
    ) -> None:
        if not self._settings_loaded:
            return
        self._set_device_type_selection(device_type, active)
        self._update_selection_ui()
        self._sync_settings_async()

    def _on_reset_to_recommended_clicked(self, _btn: Gtk.Button) -> None:
        if not self._settings_loaded:
            return
        self._device_overrides.clear()
        for device in self._devices:
            recording_id = self._device_recording_id(device)
            dev_check = self._device_checks.get(recording_id)
            if dev_check:
                dev_check.handler_block_by_func(self._on_device_check_toggled)
                dev_check.set_active(self._is_selected_device(device))
                dev_check.handler_unblock_by_func(self._on_device_check_toggled)
        self._update_selection_ui()
        self._sync_settings_async()

    def _on_record_options_changed(self, check: Gtk.CheckButton) -> None:
        if self._applying_settings or not self._settings_loaded:
            return

        self._record_mouse_movement = self._record_movement_check.get_active()
        self._record_mouse_clicks = self._record_clicks_check.get_active()
        self._record_start_position = self._record_start_pos_check.get_active()
        self._update_selection_ui()
        self._sync_settings_async()

    def _refresh_unlock_state(self) -> None:
        result = session_request({"command": "get_recording_settings"})
        if self._closed:
            return
        GLib.idle_add(self._apply_security_state_from_session, result)

    def _apply_security_state_from_session(self, result: dict | None) -> bool:
        if self._closed:
            return False
        self._apply_unlock_state(result)
        return False

    def _apply_unlock_state(self, result: dict | None) -> None:
        result = result or {}
        self._apply_macro_recording_state(result)
        self._recording_unlock_required = bool(result.get("recording_unlock_required", True))
        self._recording_unlocked = (
            bool(result.get("recording_unlocked", False)) or not self._recording_unlock_required
        )
        self._recording_refresh_owner = bool(result.get("recording_refresh_owner", False))
        if not self._recording_unlock_required:
            self._unlock_status.set_label("Save unlock not required")
            self._unlock_status.remove_css_class("success")
            self._unlock_status.remove_css_class("error")
        elif self._recording_unlocked and self._recording_refresh_owner:
            self._unlock_status.set_label("Save access unlocked")
            self._unlock_status.remove_css_class("error")
            self._unlock_status.add_css_class("success")
        elif self._recording_unlocked:
            self._unlock_status.set_label("Unlocked in another session")
            self._unlock_status.remove_css_class("success")
            self._unlock_status.add_css_class("error")
        else:
            self._unlock_status.set_label("Save access locked")
            self._unlock_status.remove_css_class("success")
            self._unlock_status.remove_css_class("error")

        self._update_unlock_ui()

    def _apply_macro_recording_state(self, result: dict | None) -> None:
        result = result or {}
        self._macro_recording_enabled = bool(result.get("macro_recording_enabled", False))
        self._macro_recording_source = str(result.get("macro_recording_source", "none") or "none")
        try:
            self._macro_recording_expires_at = int(result.get("macro_recording_expires_at", 0) or 0)
        except (TypeError, ValueError):
            self._macro_recording_expires_at = 0
        self._update_macro_recording_ui()

    def _update_macro_recording_ui(self) -> None:
        if self._macro_recording_enabled:
            if self._macro_recording_source == "runtime" and self._macro_recording_expires_at:
                subtitle = "Enabled temporarily"
            elif self._macro_recording_source == "persistent":
                subtitle = "Enabled"
            else:
                subtitle = "Enabled"
            self._macro_recording_row.set_subtitle(subtitle)
            self._macro_recording_toggle_btn.set_child(
                self._make_button_content("channel-secure-symbolic", "Disable")
            )
            self._macro_recording_toggle_btn.set_tooltip_text("Disable macro recording")
            self._macro_recording_toggle_btn.add_css_class("destructive-action")
            self._macro_recording_toggle_btn.remove_css_class("suggested-action")
            return

        self._macro_recording_row.set_subtitle("Disabled")
        self._macro_recording_toggle_btn.set_child(
            self._make_button_content("channel-insecure-symbolic", "Enable")
        )
        self._macro_recording_toggle_btn.set_tooltip_text("Enable macro recording")
        self._macro_recording_toggle_btn.add_css_class("suggested-action")
        self._macro_recording_toggle_btn.remove_css_class("destructive-action")

    def _on_macro_recording_toggle_clicked(self, _btn: Gtk.Button) -> None:
        if self._macro_recording_enabled:
            present_disable = getattr(
                self._parent,
                "present_macro_recording_disable_dialog",
                None,
            )
            if callable(present_disable):
                present_disable(on_success=self._refresh_unlock_state_async)
                return
            self._show_error("Macro recording opt-out is only available from the main window")
            return

        present_enable = getattr(self._parent, "present_macro_recording_enable_dialog", None)
        if callable(present_enable):
            present_enable(on_success=self._refresh_unlock_state_async)
            return
        self._show_error("Macro recording opt-in is only available from the main window")

    def _on_unlock_clicked(self, _btn: Gtk.Button) -> None:
        present_unlock = getattr(self._parent, "present_unlock_dialog", None)
        if callable(present_unlock):
            present_unlock(on_success=self._refresh_unlock_state_async)
            return
        self._show_error("Unlock is only available from the main window")

    def _refresh_unlock_state_async(self) -> None:
        threading.Thread(target=self._refresh_unlock_state, daemon=True).start()

    def _on_device_check_toggled(self, check: Gtk.CheckButton) -> None:
        if self._applying_settings or not self._settings_loaded:
            return

        for recording_id, dev_check in self._device_checks.items():
            if dev_check is check:
                device = self._find_device_by_recording_id(recording_id)
                if device is not None:
                    self._store_device_selection(device, check.get_active())
                else:
                    self._device_overrides[recording_id] = check.get_active()
                break
        self._update_selection_ui()
        self._sync_settings_async()

    def _make_button_content(self, icon_name: str, label: str) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        box.append(icon)
        lbl = Gtk.Label(label=label)
        box.append(lbl)
        return box

    def _make_unlock_button_content(self, label: str) -> Gtk.Box:
        return self._make_button_content("channel-insecure-symbolic", label)

    def _update_unlock_ui(self) -> None:
        if not self._recording_unlock_required:
            self._unlock_btn.set_visible(False)
            return

        has_active_unlock = self._recording_unlocked and self._recording_refresh_owner
        self._unlock_btn.set_visible(not has_active_unlock)
        label = "Claim" if self._recording_unlocked else "Unlock"
        self._unlock_btn.set_child(self._make_unlock_button_content(label))
        if self._recording_unlocked:
            self._unlock_btn.set_tooltip_text(
                "Claim this GUI as the active owner before saving temporary recording slots."
            )
        else:
            self._unlock_btn.set_tooltip_text(
                "Authorize saving temporary recording slots and protected macro body access."
            )

    def _device_types(self, device: dict) -> list[str]:
        return normalize_input_classes(
            device.get("device_types"),
            device.get("device_type", "other"),
        )

    def _device_type_text(self, device: dict) -> str:
        return " / ".join(input_class_label(dtype) for dtype in self._device_types(device))

    def _update_selection_ui(self) -> None:
        self._update_unlock_ui()
        self._update_selection_summary()

    def _update_selection_summary(self) -> None:
        selected_devices = [device for device in self._devices if self._is_selected_device(device)]
        counts = {"keyboard": 0, "mouse": 0, "gamepad": 0}
        for device in selected_devices:
            for device_type in {"keyboard", "mouse", "gamepad"}:
                if device_type in self._device_types(device):
                    counts[device_type] += 1

        detail_parts: list[str] = []
        if counts["keyboard"]:
            detail_parts.append(f"{counts['keyboard']}kb")
        if counts["mouse"]:
            detail_parts.append(f"{counts['mouse']}m")
        if counts["gamepad"]:
            detail_parts.append(f"{counts['gamepad']}gp")

        if detail_parts:
            summary = f"{len(selected_devices)} selected ({', '.join(detail_parts)})"
        else:
            summary = f"{len(selected_devices)} selected"
        self._selection_summary.set_label(summary)

        has_selected_mouse = any(
            "mouse" in self._device_types(device) for device in selected_devices
        )
        warning = ""
        if not selected_devices:
            warning = "No recording sources selected. Starting a recording will capture nothing."
        elif (self._record_mouse_movement or self._record_mouse_clicks) and not has_selected_mouse:
            warning = (
                "Mouse movement or clicks are enabled, but no selected source can produce mouse "
                "events."
            )
        self._selection_warning.set_label(warning)
        self._selection_warning.set_visible(bool(warning))

    def _on_save_settings(self, btn: Gtk.Button) -> None:
        if not self._settings_loaded:
            return
        self._save_btn.set_sensitive(False)

        def worker() -> None:
            result = session_request(self._settings_payload(), timeout=2.0)
            if self._closed:
                return
            GLib.idle_add(self._on_save_settings_done, result)

        threading.Thread(target=worker, daemon=True).start()

    def _on_save_settings_done(self, result: dict | None) -> bool:
        if self._closed:
            return False
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
        while True:
            if not self._settings_loaded or self._closed:
                with self._settings_sync_lock:
                    self._settings_sync_worker_running = False
                return

            with self._settings_sync_lock:
                generation = self._settings_sync_generation

            try:
                session_request(self._settings_payload(), timeout=0.5)
            except Exception:
                log.exception("Failed to sync macro recording settings")

            with self._settings_sync_lock:
                if generation == self._settings_sync_generation:
                    self._settings_sync_worker_running = False
                    return

    def _sync_settings_async(self) -> None:
        if not self._settings_loaded or self._closed:
            return

        with self._settings_sync_lock:
            self._settings_sync_generation += 1
            if self._settings_sync_worker_running:
                return
            self._settings_sync_worker_running = True
        threading.Thread(target=self._sync_settings_to_session, daemon=True).start()

    def _settings_payload(self) -> dict[str, object]:
        return {
            "command": "set_recording_settings",
            "include_mouse_movement": self._record_mouse_movement,
            "include_mouse_clicks": self._record_mouse_clicks,
            "record_start_position": self._record_start_position,
            "device_overrides": dict(self._device_overrides),
        }

    def _apply_recording_settings(self, result: dict | None) -> None:
        data = result or {}
        overrides = data.get("device_overrides", {})

        self._record_mouse_movement = bool(
            data.get("include_mouse_movement", self._record_mouse_movement)
        )
        self._record_mouse_clicks = bool(
            data.get("include_mouse_clicks", self._record_mouse_clicks)
        )
        self._record_start_position = bool(
            data.get("record_start_position", self._record_start_position)
        )
        if isinstance(overrides, dict):
            self._device_overrides = {str(k): bool(v) for k, v in overrides.items()}

        self._applying_settings = True
        try:
            self._record_movement_check.set_active(self._record_mouse_movement)
            self._record_clicks_check.set_active(self._record_mouse_clicks)
            self._record_start_pos_check.set_active(self._record_start_position)
        finally:
            self._applying_settings = False
        self._update_selection_ui()

    def _show_error(self, message: str) -> None:
        dialog = Adw.AlertDialog()
        dialog.set_heading("Error")
        dialog.set_body(message)
        dialog.add_response("ok", "OK")
        dialog.present(self._parent)
