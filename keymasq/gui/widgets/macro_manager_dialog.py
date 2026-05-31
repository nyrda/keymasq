import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import logging
from datetime import datetime

from gi.repository import Adw, Gdk, GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq import __version__
from keymasq.common.macro_compile import (
    build_type_macro_events,
    can_type_directly,
    char_to_key,
    normalize_type_macro_text,
    normalize_unicode_type_macro_text,
)
from keymasq.common.models import MAX_MACRO_RECORDING_SLOTS
from keymasq.gui.session_client import (
    GuiTaskResult,
    JsonDict,
    run_gui_task,
    session_request,
    session_request_async,
    session_request_with_hooks,
)
from keymasq.gui.widgets.fuzzy_search import install_listbox_fuzzy_filter, macro_search_text

log = logging.getLogger("keymasq.gui.widgets.macro_manager_dialog")

_normalize_type_macro_text = normalize_type_macro_text
_normalize_unicode_type_macro_text = normalize_unicode_type_macro_text


def _docs_version() -> str:
    version = __version__.strip()
    if not version:
        return "master"
    if "dev" in version:
        return "master"
    return f"v{version.removeprefix('v')}"


def _macros_docs_url() -> str:
    return f"https://keymasq.tools/docs/{_docs_version()}/MACROS/"


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
        self._macro_recording_enabled: bool = False
        self._recording_slot: int = 1
        self._active_recording_slot: int = 0
        self._record_btn: Gtk.Button | None = None
        self._slot_dropdown: Gtk.DropDown | None = None
        self._search_button: Gtk.Button | None = None
        self.macros_docs_btn: Gtk.Button | None = None
        self._build_ui()
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)
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

        # Toolbar row for create actions
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_margin_bottom(4)

        slot_model = Gtk.StringList()
        for slot in range(1, MAX_MACRO_RECORDING_SLOTS + 1):
            slot_model.append(f"Slot {slot}")
        slot_dropdown = Gtk.DropDown()
        slot_dropdown.set_model(slot_model)
        slot_dropdown.set_selected(0)
        slot_dropdown.set_tooltip_text("Temporary recording slot")
        slot_dropdown.connect("notify::selected", self._on_recording_slot_changed)
        toolbar.append(slot_dropdown)
        self._slot_dropdown = slot_dropdown

        record_btn = Gtk.Button()
        record_btn.set_child(
            self._make_button_content("media-record-symbolic", "Record", "error")
        )
        record_btn.set_tooltip_text("Record a new macro")
        record_btn.connect("clicked", self._on_record_new)
        toolbar.append(record_btn)
        self._record_btn = record_btn

        empty_btn = Gtk.Button()
        empty_btn.set_child(
            self._make_button_content("document-new-symbolic", "Empty")
        )
        empty_btn.set_tooltip_text("Create an empty macro to edit")
        empty_btn.connect("clicked", self._on_create_empty_macro)
        toolbar.append(empty_btn)

        type_btn = Gtk.Button()
        type_btn.set_child(
            self._make_button_content("input-keyboard-symbolic", "Type")
        )
        type_btn.set_tooltip_text("Create a macro that types text")
        type_btn.connect("clicked", self._on_create_type_macro)
        toolbar.append(type_btn)

        toolbar_spacer = Gtk.Box()
        toolbar_spacer.set_hexpand(True)
        toolbar.append(toolbar_spacer)

        search_btn = Gtk.Button()
        search_btn.set_icon_name("system-search-symbolic")
        search_btn.set_tooltip_text("Search macros")
        search_btn.connect("clicked", self._on_search_clicked)
        toolbar.append(search_btn)
        self._search_button = search_btn

        settings_btn = Gtk.Button()
        settings_btn.set_child(
            self._make_button_content("emblem-system-symbolic", "Settings")
        )
        settings_btn.set_tooltip_text("Recording settings")
        settings_btn.connect("clicked", self._on_record_settings)
        toolbar.append(settings_btn)

        content.append(toolbar)

        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Search macros")
        self._search_entry.set_tooltip_text("Filter macros by name, device type, or event count")
        self._search_entry.set_visible(False)
        self._search_entry.connect("stop-search", self._on_search_stop)
        content.append(self._search_entry)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_height(240)
        scrolled.set_max_content_height(400)
        scrolled.set_vexpand(True)

        self._listbox = Gtk.ListBox()
        self._listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._listbox.add_css_class("boxed-list")
        install_listbox_fuzzy_filter(self._listbox, self._search_entry)
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

        footer = Gtk.CenterBox(orientation=Gtk.Orientation.HORIZONTAL)
        footer.set_margin_top(8)
        footer.set_margin_bottom(8)
        footer.set_margin_start(12)
        footer.set_margin_end(12)

        docs_btn = Gtk.Button(label="?")
        docs_btn.add_css_class("flat")
        docs_btn.add_css_class("actions-docs-button")
        docs_btn.set_tooltip_text("Open Macros documentation")
        docs_btn.connect("clicked", self._on_macros_docs_clicked)
        footer.set_start_widget(docs_btn)
        self.macros_docs_btn = docs_btn

        self.playback_stop_hint = Gtk.Label(label="Interrupt macro playback: Ctrl+Alt+Esc")
        self.playback_stop_hint.add_css_class("dim-label")
        self.playback_stop_hint.add_css_class("caption")
        self.playback_stop_hint.set_halign(Gtk.Align.CENTER)
        footer.set_center_widget(self.playback_stop_hint)

        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", self._on_close_clicked)
        footer.set_end_widget(close_btn)

        inner.append(footer)
        frame.set_child(inner)
        main_box.append(frame)
        self.set_child(main_box)
        self._sync_record_button_state()

    def _on_key_pressed(self, _controller, keyval, _keycode, state) -> bool:
        if keyval in (Gdk.KEY_f, Gdk.KEY_F) and state & Gdk.ModifierType.CONTROL_MASK:
            self._show_search()
            return True
        return False

    def _show_search(self) -> None:
        self._search_entry.set_visible(True)
        self._search_entry.grab_focus()
        self._search_entry.select_region(0, -1)

    def _hide_search(self) -> None:
        self._search_entry.set_text("")
        self._search_entry.set_visible(False)

    def _on_search_clicked(self, _button: Gtk.Button) -> None:
        self._show_search()

    def _on_search_stop(self, _entry: Gtk.SearchEntry) -> None:
        self._hide_search()

    def _on_recording_slot_changed(self, dropdown: Gtk.DropDown, _pspec) -> None:
        if self._recording_active:
            active_slot = self._active_recording_slot or self._recording_slot
            if 1 <= active_slot <= MAX_MACRO_RECORDING_SLOTS:
                selected = int(dropdown.get_selected())
                expected = active_slot - 1
                if selected != expected:
                    dropdown.set_selected(expected)
            return
        self._recording_slot = int(dropdown.get_selected()) + 1

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _make_button_content(
        self,
        icon_name: str,
        label: str,
        icon_css_class: str | None = None,
    ) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        if icon_css_class:
            icon.add_css_class(icon_css_class)
        box.append(icon)
        lbl = Gtk.Label(label=label)
        box.append(lbl)
        return box

    def _load_initial_state(self) -> bool:
        run_gui_task(self._fetch_initial_state, self._on_initial_state_loaded)
        return False

    def _fetch_initial_state(self) -> tuple[JsonDict | None, JsonDict | None]:
        from keymasq.gui.session_client import session_request

        return (
            session_request({"command": "get_status"}) or {},
            session_request({"command": "list_macros", "include_slots": True}) or {},
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
        self._macro_recording_enabled = bool(status.get("macro_recording_enabled", False))
        active_slot = int(status.get("recording_slot", 0) or 0)
        if 1 <= active_slot <= MAX_MACRO_RECORDING_SLOTS:
            self._recording_slot = active_slot
            if self._recording_active:
                self._active_recording_slot = active_slot
            if self._slot_dropdown is not None:
                self._slot_dropdown.set_selected(active_slot - 1)
        self._sync_record_button_state()
        self._macros = (macros or {}).get("macros", [])
        self._populate_list()
        return False

    def _load_macros(self) -> bool:
        session_request_async(
            {"command": "list_macros", "include_slots": True},
            self._on_macros_loaded,
        )
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
        self._listbox.invalidate_filter()

    def _build_macro_row(self, macro: JsonDict) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row._search_text = macro_search_text(macro)
        is_slot = str(macro.get("kind", "") or "") == "recording_slot"
        macro_name = str(macro.get("name", "") or "")

        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row_box.set_margin_top(6)
        row_box.set_margin_bottom(6)
        row_box.set_margin_start(12)
        row_box.set_margin_end(8)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_box.set_hexpand(True)
        info_box.set_valign(Gtk.Align.CENTER)

        name_label = Gtk.Label(label=str(macro.get("display_name", macro_name) or ""))
        name_label.set_halign(Gtk.Align.START)
        info_box.append(name_label)

        duration_ms = int(macro.get("duration_us", 0) or 0) // 1000
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
        if is_slot:
            meta_text = f"temporary · {meta_text}"

        meta_label = Gtk.Label(label=meta_text)
        meta_label.add_css_class("caption")
        meta_label.add_css_class("dim-label")
        meta_label.set_halign(Gtk.Align.START)
        info_box.append(meta_label)

        row_box.append(info_box)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        btn_box.set_valign(Gtk.Align.CENTER)

        if is_slot:
            save_btn = Gtk.Button()
            save_btn.set_icon_name("document-save-symbolic")
            save_btn.set_tooltip_text("Save slot as macro")
            save_btn.add_css_class("flat")
            save_btn.connect("clicked", self._on_save_slot_clicked, macro)
            btn_box.append(save_btn)

            delete_btn = Gtk.Button()
            delete_btn.set_icon_name("edit-delete-symbolic")
            delete_btn.set_tooltip_text("Delete slot")
            delete_btn.add_css_class("flat")
            delete_btn.add_css_class("destructive-action")
            delete_btn.connect("clicked", self._on_delete_slot_clicked, macro)
            btn_box.append(delete_btn)
        else:
            play_btn = Gtk.Button()
            play_btn.set_icon_name("media-playback-start-symbolic")
            play_btn.set_tooltip_text("Play")
            play_btn.add_css_class("flat")
            play_btn.connect("clicked", self._on_play_clicked, macro_name, play_btn)
            btn_box.append(play_btn)

            edit_btn = Gtk.Button()
            edit_btn.set_icon_name("document-edit-symbolic")
            edit_btn.set_tooltip_text("Edit")
            edit_btn.add_css_class("flat")
            edit_btn.connect("clicked", self._on_edit_clicked, macro_name)
            btn_box.append(edit_btn)

            duplicate_btn = Gtk.Button()
            duplicate_btn.set_icon_name("edit-copy-symbolic")
            duplicate_btn.set_tooltip_text("Duplicate")
            duplicate_btn.add_css_class("flat")
            duplicate_btn.connect(
                "clicked",
                self._on_duplicate_clicked,
                macro_name,
                duplicate_btn,
            )
            btn_box.append(duplicate_btn)

            delete_btn = Gtk.Button()
            delete_btn.set_icon_name("edit-delete-symbolic")
            delete_btn.set_tooltip_text("Delete")
            delete_btn.add_css_class("flat")
            delete_btn.add_css_class("destructive-action")
            delete_btn.connect("clicked", self._on_delete_clicked, macro_name)
            btn_box.append(delete_btn)

        row_box.append(btn_box)
        row.set_child(row_box)
        return row

    def _on_edit_clicked(self, btn: Gtk.Button, name: str) -> None:
        from keymasq.gui.widgets.macro_editor_dialog import MacroEditorDialog

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

    def _on_save_slot_clicked(self, _btn: Gtk.Button, macro: JsonDict) -> None:
        from keymasq.gui.widgets.save_macro_dialog import SaveMacroDialog

        dialog = SaveMacroDialog(self._parent, dict(macro))
        dialog.connect("closed", self._on_slot_save_dialog_closed)
        dialog.present(self._parent)

    def _on_slot_save_dialog_closed(self, _dialog: Adw.Dialog) -> None:
        self._load_macros()

    def _on_delete_slot_clicked(self, _btn: Gtk.Button, macro: JsonDict) -> None:
        slot = int(macro.get("recording_slot", 0) or 0)
        token = str(macro.get("pending_save_token", "") or "")
        payload: JsonDict = {"command": "delete_recording_slot", "recording_slot": slot}
        if token:
            payload["pending_save_token"] = token
        session_request_with_hooks(payload, self._on_delete_slot_finished)

    def _on_delete_slot_finished(self, result: JsonDict | None) -> bool:
        if result and result.get("status") == "ok":
            return self._load_macros()

        dialog = Adw.AlertDialog()
        dialog.set_heading("Delete Recording Slot")
        dialog.set_body((result or {}).get("message", "Failed to delete recording slot"))
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
        from keymasq.gui.widgets.macro_editor_dialog import MacroEditorDialog

        dialog = MacroEditorDialog(self._parent, name)
        dialog.connect("closed", self._on_editor_closed)
        dialog.present(self._parent)

    def _on_editor_closed(self, _dialog: Adw.Dialog) -> None:
        self._load_macros()

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

    def _on_macros_docs_clicked(self, _button: Gtk.Button) -> None:
        url = _macros_docs_url()
        try:
            launcher = Gtk.UriLauncher.new(url)
            launcher.launch(None, None, None)
        except Exception as exc:
            log.warning("Could not open Macros documentation %s: %s", url, exc)

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
            session_request_with_hooks(
                {"command": "delete_macro", "name": name},
                self._on_delete_finished,
            )

    def _on_delete_finished(self, result: JsonDict | None) -> bool:
        result = result or {}
        if result.get("status") == "ok":
            return self._load_macros()

        dialog = Adw.AlertDialog()
        dialog.set_heading("Delete Macro")
        dialog.set_body(result.get("message", "Failed to delete macro"))
        dialog.add_response("ok", "OK")
        dialog.present(self._parent)
        return False

    def _on_record_new(self, btn: Gtk.Button) -> None:
        if not self._recording_active and not self._macro_recording_enabled:
            return

        btn.set_sensitive(False)
        command = "stop_recording" if self._recording_active else "start_recording"
        if self._recording_active:
            recording_slot = self._active_recording_slot or self._recording_slot
        else:
            recording_slot = self._recording_slot
        if command == "start_recording":
            self._active_recording_slot = recording_slot

        def on_record_request(result: JsonDict | None) -> bool:
            return self._on_record_request_finished(result, command)

        def on_record_done() -> None:
            btn.set_sensitive(True)

        session_request_with_hooks(
            {"command": command, "recording_slot": int(recording_slot)},
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
                self._active_recording_slot = 0
                self._sync_record_button_state()
            else:
                self._recording_unlocked = True
                self._macro_recording_enabled = True
                self._sync_record_button_state()
            return False

        if command == "start_recording" and self._is_recording_locked(result):
            self._active_recording_slot = 0
            self._recording_unlocked = False
            self._sync_record_button_state()
            self._open_recording_settings_dialog(reason="recording_locked")
            return False

        if command == "start_recording" and self._is_macro_recording_disabled(result):
            self._active_recording_slot = 0
            self._macro_recording_enabled = False
            self._sync_record_button_state()
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
        if command == "start_recording":
            self._active_recording_slot = 0
        return False

    def _is_stop_recording_success(self, result: dict) -> bool:
        if result.get("status") == "error":
            return False
        return bool(result.get("pending_recording_id")) and "duration_ms" in result

    def _on_recording_started(self, data: dict) -> None:
        self._recording_active = True
        self._recording_unlocked = True
        self._macro_recording_enabled = True
        slot = int(data.get("recording_slot", 0) or 0)
        if 1 <= slot <= MAX_MACRO_RECORDING_SLOTS:
            self._recording_slot = slot
            self._active_recording_slot = slot
            if self._slot_dropdown is not None:
                self._slot_dropdown.set_selected(slot - 1)
        self._sync_record_button_state()
        self.close()

    def _on_recording_stopped(self, data: dict) -> None:
        self._recording_active = False
        self._active_recording_slot = 0
        self._sync_record_button_state()

    def _sync_record_button_state(self) -> None:
        if not self._record_btn:
            return

        show_recording_controls = self._recording_active or self._macro_recording_enabled
        self._record_btn.set_visible(show_recording_controls)
        if self._slot_dropdown is not None:
            self._slot_dropdown.set_visible(show_recording_controls)
            self._slot_dropdown.set_sensitive(not self._recording_active)
        if not show_recording_controls:
            self._record_btn.remove_css_class("destructive-action")
            return

        if self._recording_active:
            self._record_btn.set_child(
                self._make_button_content("media-playback-stop-symbolic", "Stop")
            )
            self._record_btn.set_tooltip_text("Stop recording")
            self._record_btn.add_css_class("destructive-action")
            return

        self._record_btn.remove_css_class("destructive-action")

        self._record_btn.set_child(
            self._make_button_content("media-record-symbolic", "Record", "error")
        )
        self._record_btn.set_tooltip_text("Record a new macro")

    def _on_record_settings(self, btn: Gtk.Button) -> None:
        self._open_recording_settings_dialog()

    def _open_recording_settings_dialog(self, reason: str = "settings") -> None:
        present_settings = getattr(self._parent, "present_recording_settings_dialog", None)
        if callable(present_settings):
            present_settings(reason=reason)
            return
        from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

        record_dialog = RecordMacroDialog(self._parent, reason=reason)
        record_dialog.present(self._parent)

    def _is_recording_locked(self, result: dict) -> bool:
        error_code = str(result.get("error_code", "") or "").strip().lower()
        if error_code == "recording_locked":
            return True
        message = str(result.get("message", "") or "").strip().lower()
        return "recording_locked" in message

    def _is_macro_recording_disabled(self, result: dict) -> bool:
        error_code = str(result.get("error_code", "") or "").strip().lower()
        if error_code == "macro_recording_disabled":
            return True
        message = str(result.get("message", "") or "").strip().lower()
        return "macro_recording_disabled" in message or "macro recording opt-in" in message

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
        self.text_view.get_buffer().connect("changed", self._on_text_changed)
        text_scrolled.set_child(self.text_view)
        main.append(text_scrolled)

        self.unicode_check = Gtk.CheckButton(
            label="Use Ctrl+Shift+U for detected Unicode characters"
        )
        self.unicode_check.set_tooltip_text(
            "Best-effort Linux Unicode input. Works in many text fields, but not every app."
        )
        self.unicode_check.set_visible(False)
        main.append(self.unicode_check)

        timing = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        timing.set_halign(Gtk.Align.START)

        down_label = Gtk.Label(label="Key down (ms):")
        timing.append(down_label)

        self.down_spin = Gtk.SpinButton()
        self.down_spin.set_adjustment(
            Gtk.Adjustment(value=10, lower=0, upper=1000, step_increment=1)
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

    def _on_text_changed(self, _buffer: Gtk.TextBuffer) -> None:
        self._sync_unicode_warning()

    def _sync_unicode_warning(self) -> None:
        text = self._text_buffer_text()
        needs_unicode = self._text_needs_unicode_option(text)
        was_visible = self.unicode_check.get_visible()
        self.unicode_check.set_visible(needs_unicode)
        if needs_unicode and not was_visible:
            self.unicode_check.set_active(True)
        elif not needs_unicode:
            self.unicode_check.set_active(False)

    def _text_buffer_text(self) -> str:
        buffer = self.text_view.get_buffer()
        start = buffer.get_start_iter()
        end = buffer.get_end_iter()
        return buffer.get_text(start, end, False)

    def _text_needs_unicode_option(self, text: str) -> bool:
        exact_text = _normalize_unicode_type_macro_text(text)
        direct_text = _normalize_type_macro_text(text)
        if exact_text != direct_text:
            return True
        return any(not self._can_type_directly(ch) for ch in direct_text)

    def _on_create(self, btn: Gtk.Button) -> None:
        import re

        name = self.name_entry.get_text().strip()
        if not name:
            self._show_error("Macro name is required")
            return
        if not re.match(r"^[a-zA-Z0-9_\-]+$", name):
            self._show_error("Only letters, numbers, underscores and hyphens")
            return

        text = self._text_buffer_text()
        use_unicode_input = (
            self.unicode_check.get_visible() and self.unicode_check.get_active()
        )
        text = (
            _normalize_unicode_type_macro_text(text)
            if use_unicode_input
            else _normalize_type_macro_text(text)
        )
        if not text:
            self._show_error("Please enter text to type")
            return

        down_ms = int(self.down_spin.get_value())
        pause_ms = int(self.pause_spin.get_value())

        try:
            events = self._build_type_events(
                text,
                down_ms,
                pause_ms,
                use_unicode_input=use_unicode_input,
            )
        except ValueError as e:
            self._show_error(str(e))
            return

        duration_us = int(events[-1]["t_us"]) if events else 0
        data = {
            "name": name,
            "created_at": datetime.now().isoformat(),
            "duration_us": duration_us,
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

    def _show_error(self, message: str) -> None:
        self.error_label.set_label(message)
        self.error_label.set_visible(True)

    def _build_type_events(
        self,
        text: str,
        down_ms: int,
        pause_ms: int,
        *,
        use_unicode_input: bool = False,
    ) -> list[dict]:
        return list(
            build_type_macro_events(
                text,
                down_ms,
                pause_ms,
                use_unicode_input=use_unicode_input,
            )
        )

    def _can_type_directly(self, ch: str) -> bool:
        return can_type_directly(ch)

    def _char_to_key(self, ch: str) -> tuple[int, bool]:
        return char_to_key(ch)
