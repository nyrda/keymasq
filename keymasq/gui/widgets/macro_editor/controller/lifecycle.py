"""Dirty-state and close-flow coordination for the macro editor."""

# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.widgets.macro_editor.document import (
    CloseAction,
    close_action,
    close_response_action,
    has_pending_changes,
    normalized_payload,
)


class LifecycleControllerMixin:
    """Guard dialog closure while preserving explicit save/discard choices."""

    def _current_macro_payload(self) -> dict:
        name = (
            self._name_entry.get_text().strip()
            if hasattr(self, "_name_entry")
            else self._macro_name
        )
        return self._build_macro_payload(name)

    def _macro_payload_for_dirty_compare(self, payload: dict) -> dict:
        return normalized_payload(payload)

    def _has_pending_changes(self) -> bool:
        if not self._initial_state_loaded or not self._initial_macro_data:
            return False
        return has_pending_changes(
            initial_state_loaded=True,
            initial_payload=self._initial_macro_data,
            current_payload=self._current_macro_payload(),
        )

    def _sync_close_guard(self) -> None:
        if not hasattr(self, "_name_entry"):
            return
        if hasattr(self, "_edit_history"):
            self._record_edit_history()
        self.set_can_close(not self._save_in_flight and not self._has_pending_changes())

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self._request_close()

    def _request_close(self) -> None:
        action = close_action(self._has_pending_changes())
        if action is CloseAction.CLOSE:
            self._force_close_without_warning()
            return
        self._show_unsaved_close_warning()

    def _force_close_without_warning(self) -> None:
        self._dialog_closed = True
        if getattr(self, "_paste_cancellable", None) is not None:
            self._paste_cancellable.cancel()
        self._cancel_capture_start_position("")
        self._cancel_capture_selected_move("")
        self.set_can_close(True)
        self.force_close()

    def _show_unsaved_close_warning(self) -> None:
        if self._close_warning_dialog is not None:
            return

        dialog = Adw.AlertDialog()
        dialog.set_heading("Unsaved Macro Changes")
        dialog.set_body("Save your changes before closing, or discard them?")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("discard", "Discard")
        dialog.add_response("save", "Save")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._on_unsaved_close_response)
        self._close_warning_dialog = dialog
        dialog.present(self)

    def _on_unsaved_close_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        self._close_warning_dialog = None
        action = close_response_action(response)
        if action is CloseAction.DISCARD:
            self._force_close_without_warning()
            return
        if action is CloseAction.SAVE:
            self._save_current_macro(None, close_after_save=True)

    def _on_close_dialog_clicked(self, _button: Gtk.Button, dialog: Adw.Dialog) -> None:
        dialog.close()

    def _on_popover_cancel_clicked(self, _button: Gtk.Button, popover: Gtk.Popover) -> None:
        popover.popdown()
