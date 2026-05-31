import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")


from gi.repository import Adw, GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.session_client import session_request_async, session_request_with_hooks


class SaveMacroDialog(Adw.Dialog):
    def __init__(self, parent: Gtk.Window, recording_data: dict):
        super().__init__(title="Save Macro", content_width=420)
        self._parent = parent
        self._recording_data = recording_data
        self._saved = False
        self._closing_after_resolution = False
        self._request_inflight = False
        self._request_error_message: str | None = None
        self._unlock_denied_for_save = False
        self._pending_save_token = str(recording_data.get("pending_save_token", "") or "")
        self._recording_slot = int(recording_data.get("recording_slot", 0) or 0)
        has_start_pos = ("start_x" in recording_data) and ("start_y" in recording_data)
        self._move_to_start = bool(recording_data.get("move_to_start", has_start_pos))
        self._start_x = int(recording_data.get("start_x", 0) or 0)
        self._start_y = int(recording_data.get("start_y", 0) or 0)
        self._block_mouse_movement = bool(recording_data.get("block_mouse_movement", False))
        self._existing_macro_names: set[str] = set()
        self._later_btn: Gtk.Button | None = None
        self._build_ui()
        GLib.idle_add(self._load_existing_macro_names)
        self.connect("closed", self._on_dialog_closed)

    def _build_ui(self) -> None:
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main_box.set_margin_top(12)
        main_box.set_margin_bottom(12)
        main_box.set_margin_start(12)
        main_box.set_margin_end(12)

        frame = Gtk.Frame()
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        title = f"Save Slot {self._recording_slot}" if self._recording_slot else "Save Macro"
        title_label = Gtk.Label(label=title)
        title_label.add_css_class("title-3")
        title_label.set_halign(Gtk.Align.CENTER)
        title_label.set_margin_top(12)
        title_label.set_margin_bottom(12)
        inner.append(title_label)
        inner.append(Gtk.Separator())

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(16)
        content.set_margin_bottom(12)
        content.set_margin_start(16)
        content.set_margin_end(16)

        name_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name_row.set_valign(Gtk.Align.CENTER)

        name_label = Gtk.Label(label="Name:")
        name_label.set_halign(Gtk.Align.START)
        name_label.set_width_chars(10)
        name_row.append(name_label)

        self._name_entry = Gtk.Entry()
        self._name_entry.set_hexpand(True)
        self._name_entry.set_placeholder_text("macro_name")
        self._name_entry.connect("changed", self._on_name_changed)
        self._name_entry.connect("activate", self._on_name_activated)
        name_row.append(self._name_entry)
        content.append(name_row)

        self._error_label = Gtk.Label()
        self._error_label.add_css_class("error")
        self._error_label.add_css_class("caption")
        self._error_label.set_halign(Gtk.Align.START)
        self._error_label.set_visible(False)
        content.append(self._error_label)

        self._locked_notice = self._build_locked_notice()
        content.append(self._locked_notice)

        content.append(Gtk.Separator())

        info_grid = Gtk.Grid()
        info_grid.set_row_spacing(4)
        info_grid.set_column_spacing(12)

        duration_ms = self._recording_data.get("duration_ms", 0)
        duration_s = duration_ms / 1000.0
        event_count = self._recording_data.get("event_count", 0)
        device_types = self._recording_data.get("device_types", [])
        devices_str = ", ".join(t.capitalize() for t in device_types) if device_types else "—"

        rows = [
            ("Duration:", f"{duration_s:.3f} seconds"),
            ("Events:", str(event_count)),
            ("Devices:", devices_str),
        ]

        for i, (lbl_text, val_text) in enumerate(rows):
            lbl = Gtk.Label(label=lbl_text)
            lbl.add_css_class("dim-label")
            lbl.set_halign(Gtk.Align.START)
            info_grid.attach(lbl, 0, i, 1, 1)

            val = Gtk.Label(label=val_text)
            val.set_halign(Gtk.Align.START)
            info_grid.attach(val, 1, i, 1, 1)

        content.append(info_grid)

        content.append(Gtk.Separator())

        start_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        start_row.set_halign(Gtk.Align.START)

        self._move_to_start_check = Gtk.CheckButton(label="Move mouse to:")
        self._move_to_start_check.set_active(self._move_to_start)
        self._move_to_start_check.connect("toggled", self._on_move_to_start_toggled)
        start_row.append(self._move_to_start_check)

        self._start_x_spin = Gtk.SpinButton()
        self._start_x_spin.set_adjustment(
            Gtk.Adjustment(value=self._start_x, lower=-100000, upper=100000, step_increment=1)
        )
        self._start_x_spin.set_digits(0)
        self._start_x_spin.set_width_chars(7)
        self._start_x_spin.connect("value-changed", self._on_start_pos_changed)
        start_row.append(self._start_x_spin)

        self._start_y_spin = Gtk.SpinButton()
        self._start_y_spin.set_adjustment(
            Gtk.Adjustment(value=self._start_y, lower=-100000, upper=100000, step_increment=1)
        )
        self._start_y_spin.set_digits(0)
        self._start_y_spin.set_width_chars(7)
        self._start_y_spin.connect("value-changed", self._on_start_pos_changed)
        start_row.append(self._start_y_spin)

        start_suffix = Gtk.Label(label="at the start of the macro")
        start_suffix.add_css_class("dim-label")
        start_row.append(start_suffix)

        content.append(start_row)

        self._block_mouse_check = Gtk.CheckButton(
            label="Block physical mouse movement during playback"
        )
        self._block_mouse_check.set_active(self._block_mouse_movement)
        content.append(self._block_mouse_check)

        inner.append(content)
        inner.append(Gtk.Separator())

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END)
        footer.set_margin_top(8)
        footer.set_margin_bottom(8)
        footer.set_margin_end(12)

        if self._recording_slot:
            later_btn = Gtk.Button(label="Later")
            later_btn.add_css_class("flat")
            later_btn.connect("clicked", self._on_later_clicked)
            self._later_btn = later_btn
            footer.append(later_btn)

        self._unlock_btn = Gtk.Button()
        self._unlock_btn.set_child(self._make_unlock_button_content("Unlock"))
        self._unlock_btn.connect("clicked", self._on_unlock_clicked)
        footer.append(self._unlock_btn)

        self._save_btn = Gtk.Button(label="Save")
        self._save_btn.add_css_class("suggested-action")
        self._save_btn.set_sensitive(False)
        self._save_btn.connect("clicked", self._on_save_clicked)
        footer.append(self._save_btn)

        inner.append(footer)
        frame.set_child(inner)
        main_box.append(frame)
        self.set_child(main_box)
        self._update_start_pos_controls()
        self._update_unlock_ui()

    def _build_locked_notice(self) -> Gtk.Box:
        notice = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        notice.add_css_class("recording-locked-notice")
        notice.set_visible(False)

        icon = Gtk.Image.new_from_icon_name("channel-insecure-symbolic")
        icon.set_valign(Gtk.Align.START)
        notice.append(icon)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True)

        title = Gtk.Label(label="Saving needs unlock")
        title.add_css_class("heading")
        title.set_halign(Gtk.Align.START)
        text_box.append(title)

        body = Gtk.Label(
            label=(
                "Unlock before saving this temporary slot as a regular macro. "
                "This may show a system authorization prompt."
            )
        )
        body.set_wrap(True)
        body.set_halign(Gtk.Align.START)
        text_box.append(body)

        notice.append(text_box)
        return notice

    def do_close_attempt(self) -> None:
        if self._saved or self._closing_after_resolution:
            self.force_close()
            return
        if self._request_inflight:
            return
        self._close_for_later()

    def _on_move_to_start_toggled(self, check: Gtk.CheckButton) -> None:
        self._move_to_start = check.get_active()
        self._update_start_pos_controls()

    def _on_start_pos_changed(self, spin: Gtk.SpinButton) -> None:
        self._start_x = int(self._start_x_spin.get_value())
        self._start_y = int(self._start_y_spin.get_value())

    def _update_start_pos_controls(self) -> None:
        self._start_x_spin.set_sensitive(self._move_to_start)
        self._start_y_spin.set_sensitive(self._move_to_start)

    def _suggest_name(self) -> None:
        base = "macro"
        name = base
        i = 1
        while name in self._existing_macro_names:
            name = f"{base}_{i}"
            i += 1

        self._name_entry.set_text(name)
        self._name_entry.select_region(0, -1)
        self._validate_name(name)

    def _load_existing_macro_names(self) -> bool:
        session_request_async({"command": "list_macros"}, self._on_existing_macro_names_loaded)
        return False

    def _on_existing_macro_names_loaded(self, result: dict | None) -> bool:
        self._existing_macro_names = {
            str(m.get("name", ""))
            for m in (result or {}).get("macros", [])
            if str(m.get("name", ""))
        }
        current_name = self._name_entry.get_text()
        if current_name:
            self._validate_name(current_name)
        else:
            self._suggest_name()
        return False

    def _on_name_changed(self, entry: Gtk.Entry) -> None:
        self._validate_name(entry.get_text().strip())

    def _on_name_activated(self, entry: Gtk.Entry) -> None:
        if self._save_btn.get_sensitive():
            self._on_save_clicked(self._save_btn)

    def _validate_name(self, name: str) -> None:
        if not name:
            self._show_error("Name cannot be empty")
            self._save_btn.set_sensitive(False)
            return

        import re

        if not re.match(r"^[a-zA-Z0-9_\-]+$", name):
            self._show_error("Only letters, numbers, underscores and hyphens allowed")
            self._save_btn.set_sensitive(False)
            return

        if name in self._existing_macro_names:
            self._show_error(f"A macro named '{name}' already exists")
            self._save_btn.set_sensitive(False)
            return

        self._hide_error()
        self._save_btn.set_sensitive(not self._request_inflight and self._persist_unlock_ready())

    def _refresh_submit_state(self) -> None:
        self._validate_name(self._name_entry.get_text().strip())

    def _show_error(self, message: str) -> None:
        self._error_label.set_label(message)
        self._error_label.set_visible(True)

    def _hide_error(self) -> None:
        self._error_label.set_visible(False)

    def _on_save_clicked(self, btn: Gtk.Button) -> None:
        name = self._name_entry.get_text().strip()
        if not name or not self._persist_unlock_ready():
            return

        payload = self._save_payload(name)
        self._submit_save(payload)

    def _save_payload(self, name: str) -> dict:
        payload = {
            "command": "save_recording",
            "name": name,
            "move_to_start": self._move_to_start,
            "start_x": int(self._start_x_spin.get_value()),
            "start_y": int(self._start_y_spin.get_value()),
            "block_mouse_movement": self._block_mouse_check.get_active(),
        }
        if self._pending_save_token:
            payload["pending_save_token"] = self._pending_save_token
        if self._recording_slot:
            payload["recording_slot"] = self._recording_slot
        return payload

    def _persist_unlock_ready(self) -> bool:
        unlock_required = self._persist_unlock_required()
        if not unlock_required:
            return True
        if self._unlock_denied_for_save:
            return False
        return bool(getattr(self._parent, "_recording_unlocked", False)) and bool(
            getattr(self._parent, "_recording_refresh_owner", False)
        )

    def _persist_unlock_required(self) -> bool:
        if self._unlock_denied_for_save:
            return True
        if not hasattr(self._parent, "_recording_unlock_required"):
            return False
        return bool(getattr(self._parent, "_recording_unlock_required", True))

    def _recording_unlocked_elsewhere(self) -> bool:
        if not self._persist_unlock_required():
            return False
        return bool(getattr(self._parent, "_recording_unlocked", False)) and not bool(
            getattr(self._parent, "_recording_refresh_owner", False)
        )

    def _on_unlock_clicked(self, _btn: Gtk.Button) -> None:
        present_unlock = getattr(self._parent, "present_unlock_dialog", None)
        if callable(present_unlock):
            present_unlock(on_success=self._on_unlock_success)
            return
        self._show_error("Unlock is only available from the main window")

    def _on_unlock_success(self) -> None:
        self._unlock_denied_for_save = False
        self._update_unlock_ui()
        self._refresh_submit_state()

    def _make_unlock_button_content(self, label: str) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        icon = Gtk.Image.new_from_icon_name("channel-insecure-symbolic")
        box.append(icon)
        lbl = Gtk.Label(label=label)
        box.append(lbl)
        return box

    def _update_unlock_ui(self) -> None:
        unlock_required = self._persist_unlock_required()
        unlock_ready = self._persist_unlock_ready()
        needs_unlock = unlock_required and not unlock_ready
        self._locked_notice.set_visible(needs_unlock)
        self._unlock_btn.set_visible(needs_unlock)
        if needs_unlock:
            if self._recording_unlocked_elsewhere():
                self._unlock_btn.set_child(self._make_unlock_button_content("Claim"))
                self._unlock_btn.set_tooltip_text(
                    "Claim this GUI as the active owner before saving the slot."
                )
            else:
                self._unlock_btn.set_child(self._make_unlock_button_content("Unlock"))
                self._unlock_btn.set_tooltip_text(
                    "Authorize saving this temporary slot as a regular macro."
                )
            self._unlock_btn.add_css_class("suggested-action")
            self._save_btn.remove_css_class("suggested-action")
            self._save_btn.set_tooltip_text("Unlock before saving this slot")
        else:
            self._unlock_btn.remove_css_class("suggested-action")
            self._save_btn.add_css_class("suggested-action")
            self._save_btn.set_tooltip_text(None)

    def _submit_save(self, payload: dict) -> None:
        if self._request_inflight:
            return
        session_request_with_hooks(
            payload,
            self._on_save_finished,
            on_start=self._on_save_request_start,
            on_done=self._on_save_request_done,
        )

    def _on_save_request_start(self) -> None:
        self._request_error_message = None
        self._unlock_denied_for_save = False
        self._set_request_inflight(True)

    def _on_save_request_done(self) -> None:
        if not self._saved:
            self._set_request_inflight(False)
            if self._request_error_message:
                self._show_error(self._request_error_message)
                self._request_error_message = None

    def _on_save_finished(self, result: dict | None) -> bool:
        if result and result.get("status") == "ok":
            self._saved = True
            self._closing_after_resolution = True
            self.force_close()
        else:
            result = result or {}
            error_code = str(result.get("error_code", "") or "").strip()
            if error_code in {"recording_locked", "sensitive_command_denied"}:
                self._unlock_denied_for_save = True
                self._request_error_message = "Unlock before saving this slot."
                self._update_unlock_ui()
                return False
            self._request_error_message = result.get("message", "Failed to save macro")
        return False

    def _on_later_clicked(self, btn: Gtk.Button) -> None:
        self._close_for_later()

    def _on_dialog_closed(self, dialog) -> None:
        return

    def _close_for_later(self) -> None:
        self._closing_after_resolution = True
        self.force_close()

    def _set_request_inflight(self, inflight: bool) -> None:
        self._request_inflight = inflight
        self.set_can_close(not inflight)
        self._save_btn.set_sensitive(False)
        if self._later_btn is not None:
            self._later_btn.set_sensitive(not inflight)
        self._name_entry.set_sensitive(not inflight)
        self._move_to_start_check.set_sensitive(not inflight)
        self._block_mouse_check.set_sensitive(not inflight)
        if inflight:
            self._unlock_btn.set_sensitive(False)
            self._start_x_spin.set_sensitive(False)
            self._start_y_spin.set_sensitive(False)
            return

        self._unlock_btn.set_sensitive(True)
        self._update_start_pos_controls()
        self._update_unlock_ui()
        self._refresh_submit_state()
