import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import logging
from datetime import datetime

import evdev
from gi.repository import Adw, GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keyforge.gui.session_client import (
    GuiTaskResult,
    JsonDict,
    run_gui_task,
    session_request,
    session_request_async,
    session_request_with_hooks,
)

log = logging.getLogger("keyforge.gui.widgets.macro_manager_dialog")


def _suggest_unique_macro_name(existing_names: set[str]) -> str:
    base = "macro"
    name = base
    index = 1
    while name in existing_names:
        name = f"{base}_{index}"
        index += 1
    return name


def _suggest_duplicate_macro_name(source_name: str, existing_names: set[str]) -> str:
    index = 1
    while True:
        candidate = f"{source_name}_{index}"
        if candidate not in existing_names:
            return candidate
        index += 1


class MacroManagerDialog(Adw.Dialog):
    def __init__(self, parent: Gtk.Window):
        super().__init__(title="Macros", content_width=560)
        self._parent = parent
        self._macros: list[JsonDict] = []
        self._recording_active: bool = False
        self._recording_unlocked: bool = False
        self._record_btn: Gtk.Button | None = None
        self._cancel_playback_btn: Gtk.Button | None = None
        self._build_ui()
        GLib.idle_add(self._load_initial_state)

        # Listen for events via the window's event handler system
        if hasattr(parent, "register_event_handler"):
            parent.register_event_handler("macro_saved", self._on_macro_saved)
            parent.register_event_handler("recording_started", self._on_recording_started)
            parent.register_event_handler("recording_stopped", self._on_recording_stopped)
        self.connect("closed", self._on_dialog_closed)

    def _build_ui(self) -> None:
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main_box.set_margin_top(12)
        main_box.set_margin_bottom(12)
        main_box.set_margin_start(12)
        main_box.set_margin_end(12)

        frame = Gtk.Frame()
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        title_label = Gtk.Label(label="Macros")
        title_label.add_css_class("title-3")
        title_label.set_halign(Gtk.Align.CENTER)
        title_label.set_margin_top(12)
        title_label.set_margin_bottom(12)
        inner.append(title_label)
        inner.append(Gtk.Separator())

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.set_margin_top(12)
        content.set_margin_bottom(8)
        content.set_margin_start(12)
        content.set_margin_end(12)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_height(180)
        scrolled.set_max_content_height(360)
        scrolled.set_vexpand(True)

        self._listbox = Gtk.ListBox()
        self._listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._listbox.add_css_class("boxed-list")
        scrolled.set_child(self._listbox)
        content.append(scrolled)

        self._empty_label = Gtk.Label(label="No macros recorded yet")
        self._empty_label.add_css_class("dim-label")
        self._empty_label.set_margin_top(8)
        self._empty_label.set_margin_bottom(8)
        self._empty_label.set_visible(False)
        content.append(self._empty_label)

        inner.append(content)
        inner.append(Gtk.Separator())

        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        footer.set_margin_top(8)
        footer.set_margin_bottom(8)
        footer.set_margin_start(12)
        footer.set_margin_end(12)

        action_size_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        utility_size_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)

        create_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        record_btn = Gtk.Button(label="Record Macro…")
        record_btn.connect("clicked", self._on_record_new)
        action_size_group.add_widget(record_btn)
        create_row.append(record_btn)
        self._record_btn = record_btn

        empty_btn = Gtk.Button(label="Empty Macro…")
        empty_btn.add_css_class("suggested-action")
        empty_btn.connect("clicked", self._on_create_empty_macro)
        action_size_group.add_widget(empty_btn)
        create_row.append(empty_btn)

        type_btn = Gtk.Button(label="Type Macro…")
        type_btn.connect("clicked", self._on_create_type_macro)
        action_size_group.add_widget(type_btn)
        create_row.append(type_btn)

        footer.append(create_row)

        utility_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        settings_btn = Gtk.Button(label="Recording Settings…")
        settings_btn.connect("clicked", self._on_record_settings)
        utility_size_group.add_widget(settings_btn)
        utility_row.append(settings_btn)

        cancel_playback_btn = Gtk.Button(label="Cancel Playback")
        cancel_playback_btn.add_css_class("destructive-action")
        cancel_playback_btn.connect("clicked", self._on_cancel_playback)
        utility_size_group.add_widget(cancel_playback_btn)
        utility_row.append(cancel_playback_btn)
        self._cancel_playback_btn = cancel_playback_btn

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        utility_row.append(spacer)

        close_wrap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        close_wrap.set_halign(Gtk.Align.END)
        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", self._on_close_clicked)
        close_wrap.append(close_btn)
        utility_row.append(close_wrap)

        footer.append(utility_row)

        inner.append(footer)
        frame.set_child(inner)
        main_box.append(frame)
        self.set_child(main_box)

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _load_initial_state(self) -> bool:
        run_gui_task(self._fetch_initial_state, self._on_initial_state_loaded)
        return False

    def _fetch_initial_state(self) -> tuple[JsonDict | None, JsonDict | None]:
        from keyforge.gui.session_client import session_request

        return (
            session_request({"command": "get_status"}) or {},
            session_request({"command": "list_macros"}) or {},
        )

    def _on_initial_state_loaded(
        self,
        result: GuiTaskResult[tuple[JsonDict | None, JsonDict | None]],
    ) -> bool:
        status, macros = result.value if result.ok and result.value is not None else ({}, {})
        status = status or {}
        self._recording_active = bool(status.get("recording_active", False))
        unlock_required = bool(status.get("recording_unlock_required", True))
        self._recording_unlocked = bool(
            status.get("recording_unlocked", False)
        ) or not unlock_required
        self._sync_record_button_state()
        self._macros = (macros or {}).get("macros", [])
        self._populate_list()
        return False

    def _load_macros(self) -> bool:
        session_request_async({"command": "list_macros"}, self._on_macros_loaded)
        return False

    def _on_macros_loaded(self, result: JsonDict | None) -> bool:
        self._macros = (result or {}).get("macros", [])
        self._populate_list()
        return False

    def _populate_list(self) -> None:
        while self._listbox.get_first_child():
            self._listbox.remove(self._listbox.get_first_child())

        if not self._macros:
            self._empty_label.set_visible(True)
            return

        self._empty_label.set_visible(False)

        for macro in self._macros:
            self._listbox.append(self._build_macro_row(macro))

    def _build_macro_row(self, macro: JsonDict) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_selectable(False)

        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row_box.set_margin_top(6)
        row_box.set_margin_bottom(6)
        row_box.set_margin_start(12)
        row_box.set_margin_end(8)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_box.set_hexpand(True)
        info_box.set_valign(Gtk.Align.CENTER)

        name_label = Gtk.Label(label=macro["name"])
        name_label.set_halign(Gtk.Align.START)
        info_box.append(name_label)

        duration_ms = int(macro.get("duration_ms", 0) or 0)
        device_types = [str(device_type) for device_type in macro.get("device_types", [])]
        device_abbrevs = "+".join(
            {"keyboard": "kbd", "mouse": "mouse", "gamepad": "pad"}.get(t, t) for t in device_types
        )
        if duration_ms < 1000:
            meta_text = f"{duration_ms}ms"
        else:
            meta_text = f"{duration_ms / 1000.0:.1f}s"
        if device_abbrevs:
            meta_text += f" · {device_abbrevs}"
        event_count = macro.get("event_count", 0)
        if event_count:
            meta_text += f" · {event_count} events"

        meta_label = Gtk.Label(label=meta_text)
        meta_label.add_css_class("caption")
        meta_label.add_css_class("dim-label")
        meta_label.set_halign(Gtk.Align.START)
        info_box.append(meta_label)

        row_box.append(info_box)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        btn_box.set_valign(Gtk.Align.CENTER)

        play_btn = Gtk.Button()
        play_btn.set_icon_name("media-playback-start-symbolic")
        play_btn.set_tooltip_text("Play")
        play_btn.add_css_class("flat")
        play_btn.connect("clicked", self._on_play_clicked, macro["name"], play_btn)
        btn_box.append(play_btn)

        edit_btn = Gtk.Button()
        edit_btn.set_icon_name("document-edit-symbolic")
        edit_btn.set_tooltip_text("Edit")
        edit_btn.add_css_class("flat")
        edit_btn.connect("clicked", self._on_edit_clicked, macro["name"])
        btn_box.append(edit_btn)

        duplicate_btn = Gtk.Button()
        duplicate_btn.set_icon_name("edit-copy-symbolic")
        duplicate_btn.set_tooltip_text("Duplicate")
        duplicate_btn.add_css_class("flat")
        duplicate_btn.connect("clicked", self._on_duplicate_clicked, macro["name"], duplicate_btn)
        btn_box.append(duplicate_btn)

        delete_btn = Gtk.Button()
        delete_btn.set_icon_name("edit-delete-symbolic")
        delete_btn.set_tooltip_text("Delete")
        delete_btn.add_css_class("flat")
        delete_btn.add_css_class("destructive-action")
        delete_btn.connect("clicked", self._on_delete_clicked, macro["name"])
        btn_box.append(delete_btn)

        row_box.append(btn_box)
        row.set_child(row_box)
        return row

    def _on_edit_clicked(self, btn: Gtk.Button, name: str) -> None:
        from keyforge.gui.widgets.macro_editor_dialog import MacroEditorDialog

        dialog = MacroEditorDialog(self._parent, name)
        dialog.connect("closed", self._on_editor_closed)
        dialog.present(self._parent)

    def _on_duplicate_clicked(
        self,
        _btn: Gtk.Button,
        name: str,
        duplicate_btn: Gtk.Button,
    ) -> None:
        def request_duplicate() -> JsonDict | None:
            return self._duplicate_macro_request(name)

        def on_duplicate_start() -> None:
            duplicate_btn.set_sensitive(False)

        def on_duplicate_done() -> None:
            duplicate_btn.set_sensitive(True)

        run_gui_task(
            request_duplicate,
            self._on_duplicate_finished,
            on_start=on_duplicate_start,
            on_done=on_duplicate_done,
        )

    def _duplicate_macro_request(self, name: str) -> JsonDict | None:
        response = session_request({"command": "get_macro", "name": name}) or {}
        macro = response.get("macro")
        if response.get("status") != "ok" or not isinstance(macro, dict):
            return {"status": "error", "message": response.get("message", "Failed to load macro")}

        existing_names = {
            str(item.get("name", ""))
            for item in self._macros
            if str(item.get("name", ""))
        }
        duplicate_name = _suggest_duplicate_macro_name(name, existing_names)
        duplicate_macro = dict(macro)
        duplicate_macro.pop("revision", None)
        duplicate_macro["name"] = duplicate_name
        return session_request({"command": "create_macro", "macro": duplicate_macro}) or {}

    def _on_duplicate_finished(self, result: GuiTaskResult[JsonDict | None]) -> bool:
        payload = result.value if result.ok and isinstance(result.value, dict) else {}
        if payload.get("status") == "ok":
            self._load_macros()
            return False

        dialog = Adw.AlertDialog()
        dialog.set_heading("Duplicate Macro")
        dialog.set_body(payload.get("message", "Failed to duplicate macro"))
        dialog.add_response("ok", "OK")
        dialog.present(self._parent)
        return False

    def _on_create_empty_macro(self, _btn: Gtk.Button) -> None:
        session_request_async({"command": "list_macros"}, self._on_empty_macro_names_loaded)

    def _on_empty_macro_names_loaded(self, result: JsonDict | None) -> bool:
        existing_names = {
            str(m.get("name", ""))
            for m in (result or {}).get("macros", [])
            if str(m.get("name", ""))
        }
        self._open_empty_macro_editor(_suggest_unique_macro_name(existing_names))
        return False

    def _open_empty_macro_editor(self, name: str) -> None:
        from keyforge.gui.widgets.macro_editor_dialog import MacroEditorDialog

        dialog = MacroEditorDialog(self._parent, name)
        dialog.connect("closed", self._on_editor_closed)
        dialog.present(self._parent)

    def _on_play_clicked(self, btn: Gtk.Button, name: str, play_btn: Gtk.Button) -> None:
        play_btn.set_sensitive(False)

        def on_play_requested(result: JsonDict | None) -> bool:
            return self._on_play_requested(result, play_btn)

        session_request_with_hooks(
            {"command": "play_macro", "name": name},
            on_play_requested,
        )

    def _on_play_requested(
        self,
        result: JsonDict | None,
        play_btn: Gtk.Button,
    ) -> bool:
        if not result or result.get("status") != "ok":
            play_btn.set_sensitive(True)
            return False
        play_btn.set_sensitive(True)
        return False

    def _on_cancel_playback(self, _btn: Gtk.Button) -> None:
        session_request_with_hooks(
            {"command": "cancel_macro_playback"},
            self._on_cancel_playback_finished,
        )

    def _on_cancel_playback_finished(self, result: dict | None) -> bool:
        if (result or {}).get("status") == "ok":
            self._populate_list()
        return False

    def _on_delete_clicked(self, btn: Gtk.Button, name: str) -> None:
        dialog = Adw.AlertDialog()
        dialog.set_heading("Delete Macro")
        dialog.set_body(f"Delete '{name}'? This cannot be undone.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_delete_response, name)
        dialog.present(self._parent)

    def _on_delete_response(self, dialog, response: str, name: str) -> None:
        if response == "delete":
            def on_delete_finished(_result: JsonDict | None) -> bool:
                return self._load_macros()

            session_request_with_hooks(
                {"command": "delete_macro", "name": name},
                on_delete_finished,
            )

    def _on_record_new(self, btn: Gtk.Button) -> None:
        if not self._recording_active and not self._recording_unlocked:
            present_unlock = getattr(self._parent, "present_unlock_dialog", None)
            if callable(present_unlock):
                present_unlock(on_success=self._on_unlock_success)
            else:
                self._open_recording_settings_dialog()
            return

        btn.set_sensitive(False)
        command = "stop_recording" if self._recording_active else "start_recording"

        def on_record_request(result: JsonDict | None) -> bool:
            return self._on_record_request_finished(result, command)

        def on_record_done() -> None:
            btn.set_sensitive(True)

        session_request_with_hooks(
            {"command": command},
            on_record_request,
            on_done=on_record_done,
        )

    def _on_record_request_finished(self, result: JsonDict | None, command: str) -> bool:
        result = result or {}
        log.debug("macro recording request finished: command=%s result=%r", command, result)
        is_stop_success = command == "stop_recording" and self._is_stop_recording_success(result)
        if result.get("status") == "ok" or is_stop_success:
            if command == "stop_recording":
                self._recording_active = False
                self._sync_record_button_state()
            else:
                self._recording_unlocked = True
                self._sync_record_button_state()
            return False

        if command == "start_recording" and self._is_recording_locked(result):
            self._recording_unlocked = False
            self._sync_record_button_state()
            self._open_recording_settings_dialog()
            return False

        fallback = (
            "Failed to stop recording"
            if command == "stop_recording"
            else "Failed to start recording"
        )
        msg = result.get("message", fallback)
        log.warning(
            "showing recording error dialog: command=%s message=%s result=%r",
            command,
            msg,
            result,
        )
        dialog = Adw.AlertDialog()
        dialog.set_heading("Recording Error")
        dialog.set_body(msg)
        dialog.add_response("ok", "OK")
        dialog.present(self._parent)
        return False

    def _is_stop_recording_success(self, result: dict) -> bool:
        if result.get("status") == "error":
            return False
        return isinstance(result.get("events"), list) and "duration_ms" in result

    def _on_recording_started(self, data: dict) -> None:
        self._recording_active = True
        self._recording_unlocked = True
        self._sync_record_button_state()

    def _on_recording_stopped(self, data: dict) -> None:
        self._recording_active = False
        self._sync_record_button_state()

    def _sync_record_button_state(self) -> None:
        if not self._record_btn:
            return

        if self._recording_active:
            self._record_btn.set_label("Stop Recording")
            self._record_btn.add_css_class("destructive-action")
            return

        self._record_btn.add_css_class("destructive-action")

        if not self._recording_unlocked:
            self._record_btn.set_label("Unlock Recording")
        else:
            self._record_btn.set_label("Record Macro…")

    def _on_unlock_success(self) -> None:
        session_request_async({"command": "get_status"}, self._on_status_after_unlock)

    def _on_status_after_unlock(self, status: dict | None) -> bool:
        status = status or {}
        unlock_required = bool(status.get("recording_unlock_required", True))
        self._recording_unlocked = bool(
            status.get("recording_unlocked", False)
        ) or not unlock_required
        self._sync_record_button_state()
        return False

    def _on_record_settings(self, btn: Gtk.Button) -> None:
        self._open_recording_settings_dialog()

    def _open_recording_settings_dialog(self) -> None:
        present_settings = getattr(self._parent, "present_recording_settings_dialog", None)
        if callable(present_settings):
            present_settings()
            return
        from keyforge.gui.widgets.record_macro_dialog import RecordMacroDialog

        record_dialog = RecordMacroDialog(self._parent)
        record_dialog.present(self._parent)

    def _is_recording_locked(self, result: dict) -> bool:
        error_code = str(result.get("error_code", "") or "").strip().lower()
        if error_code == "recording_locked":
            return True
        message = str(result.get("message", "") or "").strip().lower()
        return "recording_locked" in message

    def _on_create_type_macro(self, btn: Gtk.Button) -> None:
        dialog = TypeMacroDialog(self._parent, on_created=self._load_macros)
        dialog.present(self._parent)

    def _on_macro_saved(self, data: dict) -> None:
        self._load_macros()

    def _on_dialog_closed(self, dialog) -> None:
        if hasattr(self._parent, "unregister_event_handler"):
            self._parent.unregister_event_handler("macro_saved", self._on_macro_saved)
            self._parent.unregister_event_handler("recording_started", self._on_recording_started)
            self._parent.unregister_event_handler("recording_stopped", self._on_recording_stopped)


class TypeMacroDialog(Adw.Dialog):
    def __init__(self, parent: Gtk.Window, on_created=None):
        super().__init__(title="Create Type Macro", content_width=560)
        self._parent = parent
        self._on_created = on_created
        self._build_ui()

    def _build_ui(self) -> None:
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        main.set_margin_top(12)
        main.set_margin_bottom(12)
        main.set_margin_start(12)
        main.set_margin_end(12)

        name_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name_label = Gtk.Label(label="Macro name:")
        name_label.set_halign(Gtk.Align.START)
        name_row.append(name_label)

        self.name_entry = Gtk.Entry()
        self.name_entry.set_hexpand(True)
        self.name_entry.set_placeholder_text("e.g., type_hello")
        self.name_entry.set_text(f"type_{datetime.now().strftime('%H%M%S')}")
        name_row.append(self.name_entry)
        main.append(name_row)

        text_label = Gtk.Label(label="Text to type:")
        text_label.set_halign(Gtk.Align.START)
        main.append(text_label)

        text_scrolled = Gtk.ScrolledWindow()
        text_scrolled.set_min_content_height(130)
        text_scrolled.set_max_content_height(220)
        text_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.text_view = Gtk.TextView()
        self.text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        text_scrolled.set_child(self.text_view)
        main.append(text_scrolled)

        timing = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        timing.set_halign(Gtk.Align.START)

        down_label = Gtk.Label(label="Key down (ms):")
        timing.append(down_label)

        self.down_spin = Gtk.SpinButton()
        self.down_spin.set_adjustment(
            Gtk.Adjustment(value=10, lower=1, upper=1000, step_increment=1)
        )
        timing.append(self.down_spin)

        pause_label = Gtk.Label(label="Pause between keys (ms):")
        timing.append(pause_label)

        self.pause_spin = Gtk.SpinButton()
        self.pause_spin.set_adjustment(
            Gtk.Adjustment(value=20, lower=0, upper=1000, step_increment=1)
        )
        timing.append(self.pause_spin)

        main.append(timing)

        self.error_label = Gtk.Label()
        self.error_label.add_css_class("error")
        self.error_label.add_css_class("caption")
        self.error_label.set_halign(Gtk.Align.START)
        self.error_label.set_visible(False)
        main.append(self.error_label)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.END)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_close_clicked)
        btn_row.append(cancel_btn)

        self._create_btn = Gtk.Button(label="Create")
        self._create_btn.add_css_class("suggested-action")
        self._create_btn.connect("clicked", self._on_create)
        btn_row.append(self._create_btn)

        main.append(btn_row)
        self.set_child(main)

    def _on_create(self, btn: Gtk.Button) -> None:
        import re

        name = self.name_entry.get_text().strip()
        if not name:
            self._show_error("Macro name is required")
            return
        if not re.match(r"^[a-zA-Z0-9_\-]+$", name):
            self._show_error("Only letters, numbers, underscores and hyphens")
            return

        buffer = self.text_view.get_buffer()
        start = buffer.get_start_iter()
        end = buffer.get_end_iter()
        text = buffer.get_text(start, end, False)
        if not text:
            self._show_error("Please enter text to type")
            return

        down_ms = int(self.down_spin.get_value())
        pause_ms = int(self.pause_spin.get_value())

        try:
            events = self._build_type_events(text, down_ms, pause_ms)
        except ValueError as e:
            self._show_error(str(e))
            return

        duration_ms = int(events[-1]["t_us"] / 1000) if events else 0
        data = {
            "name": name,
            "created_at": datetime.now().isoformat(),
            "duration_ms": duration_ms,
            "device_types": ["keyboard"],
            "events": events,
        }

        def on_create_start() -> None:
            self._create_btn.set_sensitive(False)

        def on_create_done() -> None:
            self._create_btn.set_sensitive(True)

        session_request_with_hooks(
            {"command": "create_macro", "macro": data},
            self._on_create_finished,
            on_start=on_create_start,
            on_done=on_create_done,
        )

    def _on_create_finished(self, result: JsonDict | None) -> bool:
        result = result or {}
        if result.get("status") != "ok":
            self._show_error(result.get("message", "Failed to create macro"))
            return False

        if self._on_created:
            self._on_created()
        self.close()
        return False

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _on_editor_closed(self, _dialog: Adw.Dialog) -> None:
        self._load_macros()

    def _show_error(self, message: str) -> None:
        self.error_label.set_label(message)
        self.error_label.set_visible(True)

    def _build_type_events(self, text: str, down_ms: int, pause_ms: int) -> list[dict]:
        events: list[dict] = []
        t_us = 0
        modifier_settle_us = 1_000

        for i, ch in enumerate(text):
            code, needs_shift = self._char_to_key(ch)

            if needs_shift:
                events.append(
                    {
                        "device_type": "keyboard",
                        "type": evdev.ecodes.EV_KEY,
                        "code": evdev.ecodes.KEY_LEFTSHIFT,
                        "value": 1,
                        "t_us": t_us,
                    }
                )
                t_us += modifier_settle_us

            events.append(
                {
                    "device_type": "keyboard",
                    "type": evdev.ecodes.EV_KEY,
                    "code": code,
                    "value": 1,
                    "t_us": t_us,
                }
            )

            t_us += down_ms * 1000

            events.append(
                {
                    "device_type": "keyboard",
                    "type": evdev.ecodes.EV_KEY,
                    "code": code,
                    "value": 0,
                    "t_us": t_us,
                }
            )

            if needs_shift:
                t_us += modifier_settle_us
                events.append(
                    {
                        "device_type": "keyboard",
                        "type": evdev.ecodes.EV_KEY,
                        "code": evdev.ecodes.KEY_LEFTSHIFT,
                        "value": 0,
                        "t_us": t_us,
                    }
                )

            if i < len(text) - 1 and pause_ms > 0:
                t_us += pause_ms * 1000

        return events

    def _char_to_key(self, ch: str) -> tuple[int, bool]:
        letters = "abcdefghijklmnopqrstuvwxyz"
        if ch.lower() in letters:
            return getattr(evdev.ecodes, f"KEY_{ch.upper()}"), ch.isupper()

        digits = {
            "1": evdev.ecodes.KEY_1,
            "2": evdev.ecodes.KEY_2,
            "3": evdev.ecodes.KEY_3,
            "4": evdev.ecodes.KEY_4,
            "5": evdev.ecodes.KEY_5,
            "6": evdev.ecodes.KEY_6,
            "7": evdev.ecodes.KEY_7,
            "8": evdev.ecodes.KEY_8,
            "9": evdev.ecodes.KEY_9,
            "0": evdev.ecodes.KEY_0,
        }
        if ch in digits:
            return digits[ch], False

        specials = {
            " ": (evdev.ecodes.KEY_SPACE, False),
            "\n": (evdev.ecodes.KEY_ENTER, False),
            "\t": (evdev.ecodes.KEY_TAB, False),
            "-": (evdev.ecodes.KEY_MINUS, False),
            "_": (evdev.ecodes.KEY_MINUS, True),
            "=": (evdev.ecodes.KEY_EQUAL, False),
            "+": (evdev.ecodes.KEY_EQUAL, True),
            "[": (evdev.ecodes.KEY_LEFTBRACE, False),
            "{": (evdev.ecodes.KEY_LEFTBRACE, True),
            "]": (evdev.ecodes.KEY_RIGHTBRACE, False),
            "}": (evdev.ecodes.KEY_RIGHTBRACE, True),
            "\\": (evdev.ecodes.KEY_BACKSLASH, False),
            "|": (evdev.ecodes.KEY_BACKSLASH, True),
            ";": (evdev.ecodes.KEY_SEMICOLON, False),
            ":": (evdev.ecodes.KEY_SEMICOLON, True),
            "'": (evdev.ecodes.KEY_APOSTROPHE, False),
            '"': (evdev.ecodes.KEY_APOSTROPHE, True),
            ",": (evdev.ecodes.KEY_COMMA, False),
            "<": (evdev.ecodes.KEY_COMMA, True),
            ".": (evdev.ecodes.KEY_DOT, False),
            ">": (evdev.ecodes.KEY_DOT, True),
            "/": (evdev.ecodes.KEY_SLASH, False),
            "?": (evdev.ecodes.KEY_SLASH, True),
            "`": (evdev.ecodes.KEY_GRAVE, False),
            "~": (evdev.ecodes.KEY_GRAVE, True),
            "!": (evdev.ecodes.KEY_1, True),
            "@": (evdev.ecodes.KEY_2, True),
            "#": (evdev.ecodes.KEY_3, True),
            "$": (evdev.ecodes.KEY_4, True),
            "%": (evdev.ecodes.KEY_5, True),
            "^": (evdev.ecodes.KEY_6, True),
            "&": (evdev.ecodes.KEY_7, True),
            "*": (evdev.ecodes.KEY_8, True),
            "(": (evdev.ecodes.KEY_9, True),
            ")": (evdev.ecodes.KEY_0, True),
        }
        if ch in specials:
            return specials[ch]

        raise ValueError(f"Unsupported character for typing macro: {repr(ch)}")
