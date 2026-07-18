"""Saved-macro, temporary-slot, playback, and creation actions."""

# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import (  # pyright: ignore[reportAttributeAccessIssue]
    Adw,  # pyright: ignore[reportAttributeAccessIssue]
    Gdk,  # pyright: ignore[reportAttributeAccessIssue]
    Gio,  # pyright: ignore[reportAttributeAccessIssue]
    GLib,  # pyright: ignore[reportAttributeAccessIssue]
    Gtk,  # pyright: ignore[reportAttributeAccessIssue]
)

from keymasq.gui.session_client import GuiTaskResult, JsonDict
from keymasq.gui.widgets.macro_manager.state import (
    suggest_duplicate_macro_name,
    suggest_unique_macro_name,
)


class MacroActionsMixin:
    """Coordinate macro mutations and dialogs without owning list rendering."""

    def _on_edit_clicked(self, _btn: Gtk.Button, name: str) -> None:
        self._open_macro_editor(name)

    def _on_row_right_pressed(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
        row: Gtk.ListBoxRow,
        name: str,
    ) -> None:
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._listbox.select_row(row)
        self._show_macro_context_menu(row, name, x, y)

    def _show_macro_context_menu(
        self,
        row: Gtk.ListBoxRow,
        name: str,
        x: float,
        y: float,
    ) -> None:
        def make_action(action_name: str, handler: Callable[[], None]) -> Gio.SimpleAction:
            def on_activate(_action: Gio.SimpleAction, _param: object) -> None:
                handler()

            action = Gio.SimpleAction.new(action_name, None)
            action.connect("activate", on_activate)
            return action

        def copy_name() -> None:
            self._copy_macro_name(name)

        def play() -> None:
            self._play_macro(name)

        def edit() -> None:
            self._open_macro_editor(name)

        def duplicate() -> None:
            self._duplicate_macro(name)

        def delete() -> None:
            self._on_delete_clicked(None, name)

        actions = Gio.SimpleActionGroup()
        menu = Gio.Menu()
        for action_name, label, handler in (
            ("copy-name", "Copy Name", copy_name),
            ("play", "Play", play),
            ("edit", "Edit", edit),
            ("duplicate", "Duplicate", duplicate),
        ):
            actions.add_action(make_action(action_name, handler))
            menu.append(label, f"macro-row.{action_name}")

        actions.add_action(make_action("delete", delete))
        delete_section = Gio.Menu()
        delete_section.append("Delete", "macro-row.delete")
        menu.append_section(None, delete_section)

        row.insert_action_group("macro-row", actions)
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(row)
        popover.set_has_arrow(False)
        popover.set_halign(Gtk.Align.START)
        pointing_rect = Gdk.Rectangle()
        pointing_rect.x = int(x)
        pointing_rect.y = int(y)
        pointing_rect.width = 1
        pointing_rect.height = 1
        popover.set_pointing_to(pointing_rect)
        popover.connect("closed", self._on_context_menu_closed, row)
        popover.popup()

    def _on_context_menu_closed(self, popover: Gtk.PopoverMenu, row: Gtk.ListBoxRow) -> None:
        # Unparenting while the menu-item activation is still being dispatched
        # would cancel it, so defer teardown to idle.
        def teardown() -> bool:
            popover.unparent()
            row.insert_action_group("macro-row", None)
            return False

        GLib.idle_add(teardown)

    def _copy_macro_name(self, name: str) -> None:
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(name)

    def _on_row_activated(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        macro = getattr(row, "_macro", None)
        state = getattr(row, "_row_state", None)
        if not isinstance(macro, dict) or state is None:
            return
        if state.is_temporary_slot:
            self._on_save_slot_clicked(None, macro)
        elif state.name:
            self._open_macro_editor(state.name)

    def _on_duplicate_clicked(
        self,
        _btn: Gtk.Button,
        name: str,
        duplicate_btn: Gtk.Button,
    ) -> None:
        self._duplicate_macro(name, duplicate_btn)

    def _duplicate_macro(self, name: str, duplicate_btn: Gtk.Button | None = None) -> None:
        def request_duplicate() -> JsonDict | None:
            return self._duplicate_macro_request(name)

        def on_duplicate_start() -> None:
            if duplicate_btn is not None:
                duplicate_btn.set_sensitive(False)

        def on_duplicate_done() -> None:
            if duplicate_btn is not None:
                duplicate_btn.set_sensitive(True)

        self._run_gui_task(
            request_duplicate,
            self._on_duplicate_finished,
            on_start=on_duplicate_start,
            on_done=on_duplicate_done,
        )

    def _duplicate_macro_request(self, name: str) -> JsonDict | None:
        response = self._session_request({"command": "get_macro", "name": name}) or {}
        macro = response.get("macro")
        if response.get("status") != "ok" or not isinstance(macro, dict):
            return {
                "status": "error",
                "message": response.get("message", "Failed to load macro"),
            }

        duplicate_name = suggest_duplicate_macro_name(name, self._catalog.names)
        duplicate_macro = dict(macro)
        duplicate_macro.pop("revision", None)
        duplicate_macro["name"] = duplicate_name
        return self._session_request({"command": "create_macro", "macro": duplicate_macro}) or {}

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

    def _on_save_slot_clicked(self, _btn: Gtk.Button | None, macro: JsonDict) -> None:
        from keymasq.gui.widgets.save_macro_dialog import SaveMacroDialog

        dialog = SaveMacroDialog(self._parent, dict(macro))
        dialog.connect("closed", self._on_slot_save_dialog_closed)
        dialog.present(self._parent)

    def _on_slot_save_dialog_closed(self, _dialog: Adw.Dialog) -> None:
        self._load_macros()

    def _on_delete_slot_clicked(self, _btn: Gtk.Button, macro: JsonDict) -> None:
        slot = int(macro.get("recording_slot", 0) or 0)
        token = str(macro.get("pending_save_token", "") or "")
        payload: JsonDict = {
            "command": "delete_recording_slot",
            "recording_slot": slot,
        }
        if token:
            payload["pending_save_token"] = token
        self._session_request_async(payload, self._on_delete_slot_finished)

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
        self._session_request_async(
            {"command": "list_macros"},
            self._on_empty_macro_names_loaded,
        )

    def _on_empty_macro_names_loaded(self, result: JsonDict | None) -> bool:
        existing_names = {
            str(macro.get("name", ""))
            for macro in (result or {}).get("macros", [])
            if str(macro.get("name", ""))
        }
        self._open_empty_macro_editor(suggest_unique_macro_name(existing_names))
        return False

    def _open_empty_macro_editor(self, name: str) -> None:
        self._open_macro_editor(name, create_new=True)

    def _open_macro_editor(self, name: str, *, create_new: bool = False) -> None:
        from keymasq.gui.widgets.macro_editor.dialog import MacroEditorDialog

        dialog = MacroEditorDialog(self._parent, name, create_new=create_new)
        dialog.connect("closed", self._on_editor_closed)
        dialog.present(self._parent)

    def _on_editor_closed(self, _dialog: Adw.Dialog) -> None:
        self._load_macros()

    def _on_play_clicked(self, play_btn: Gtk.Button, name: str) -> None:
        self._play_macro(name, play_btn)

    def _play_macro(self, name: str, play_btn: Gtk.Button | None = None) -> None:
        if play_btn is not None:
            play_btn.set_sensitive(False)

        def on_play_requested(result: JsonDict | None) -> bool:
            return self._on_play_requested(result, play_btn)

        self._session_request_async(
            {"command": "play_macro", "name": name},
            on_play_requested,
        )

    def _on_play_requested(
        self,
        _result: JsonDict | None,
        play_btn: Gtk.Button | None,
    ) -> bool:
        if play_btn is not None:
            play_btn.set_sensitive(True)
        return False

    def _on_delete_clicked(self, _btn: Gtk.Button | None, name: str) -> None:
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

    def _on_delete_response(self, _dialog, response: str, name: str) -> None:
        if response == "delete":
            self._session_request_async(
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

    def _on_create_type_macro(self, _btn: Gtk.Button) -> None:
        dialog = self._new_type_macro_dialog(on_created=self._load_macros)
        dialog.present(self._parent)
