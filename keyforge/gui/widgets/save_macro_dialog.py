import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")


from gi.repository import Adw, GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keyforge.gui.session_client import session_request_async, session_request_with_hooks


class SaveMacroDialog(Adw.Dialog):
    def __init__(self, parent: Gtk.Window, recording_data: dict):
        super().__init__(title="Save Macro", content_width=420)
        self._parent = parent
        self._recording_data = recording_data
        self._saved = False
        has_start_pos = ("start_x" in recording_data) and ("start_y" in recording_data)
        self._move_to_start = bool(recording_data.get("move_to_start", has_start_pos))
        self._start_x = int(recording_data.get("start_x", 0) or 0)
        self._start_y = int(recording_data.get("start_y", 0) or 0)
        self._block_mouse_movement = bool(recording_data.get("block_mouse_movement", False))
        self._existing_macro_names: set[str] = set()
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

        title_label = Gtk.Label(label="Save Macro")
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

        discard_btn = Gtk.Button(label="Discard")
        discard_btn.add_css_class("destructive-action")
        discard_btn.add_css_class("flat")
        discard_btn.connect("clicked", self._on_discard_clicked)
        footer.append(discard_btn)

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
        self._suggest_name()
        return False

    def _on_name_changed(self, entry: Gtk.Entry) -> None:
        self._validate_name(entry.get_text())

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
        self._save_btn.set_sensitive(True)

    def _show_error(self, message: str) -> None:
        self._error_label.set_label(message)
        self._error_label.set_visible(True)

    def _hide_error(self) -> None:
        self._error_label.set_visible(False)

    def _on_save_clicked(self, btn: Gtk.Button) -> None:
        name = self._name_entry.get_text().strip()
        if not name:
            return

        payload = (
            {
                "command": "save_recording",
                "name": name,
                "move_to_start": self._move_to_start,
                "start_x": int(self._start_x_spin.get_value()),
                "start_y": int(self._start_y_spin.get_value()),
                "block_mouse_movement": self._block_mouse_check.get_active(),
            }
        )
        session_request_with_hooks(
            payload,
            self._on_save_finished,
            on_start=lambda: self._save_btn.set_sensitive(False),
            on_done=self._on_save_request_done,
        )

    def _on_save_request_done(self) -> None:
        if not self._saved:
            self._save_btn.set_sensitive(True)

    def _on_save_finished(self, result: dict | None) -> bool:
        if result and result.get("status") == "ok":
            self._saved = True
            self.close()
        else:
            msg = (result or {}).get("message", "Failed to save macro")
            self._show_error(msg)
        return False

    def _on_discard_clicked(self, btn: Gtk.Button) -> None:
        self._saved = True  # Prevent double-discard in _on_dialog_closed
        session_request_async({"command": "discard_recording"}, lambda _result: False)
        self.close()

    def _on_dialog_closed(self, dialog) -> None:
        if not self._saved:
            session_request_async({"command": "discard_recording"}, lambda _result: False)
