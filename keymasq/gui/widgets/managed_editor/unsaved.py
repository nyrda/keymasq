"""Unsaved-change prompt controller for managed-resource editors."""

from collections.abc import Callable
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # pyright: ignore[reportAttributeAccessIssue]

from keymasq.gui.widgets.managed_editor.state import EditorSelection, EditorState


@dataclass(frozen=True, slots=True)
class UnsavedChangesMessages:
    """Resource-specific copy used by unsaved-change prompts."""

    heading: str
    close_body: str
    switch_body: str
    restart_new_item_body: str


@dataclass(frozen=True, slots=True)
class UnsavedChangesCallbacks:
    """Explicit domain operations used after an unsaved-change decision."""

    save_current: Callable[[], bool]
    close_editor: Callable[[], None]
    select_pending_target: Callable[[EditorSelection | None], None]
    restart_new_item: Callable[[], None]
    restore_active_selection: Callable[[], None]
    update_buttons: Callable[[], None]
    before_close: Callable[[], None] | None = None


class UnsavedChangesController:
    """Owns prompt lifecycles and applies the resulting editor transitions."""

    __slots__ = (
        "_callbacks",
        "_close_dialog",
        "_messages",
        "_parent",
        "_selection_dialog",
        "_selection_response_handler",
        "_state",
    )

    def __init__(
        self,
        *,
        parent: Gtk.Widget,
        state: EditorState,
        messages: UnsavedChangesMessages,
        callbacks: UnsavedChangesCallbacks,
    ) -> None:
        self._parent = parent
        self._state = state
        self._messages = messages
        self._callbacks = callbacks
        self._close_dialog: Adw.AlertDialog | None = None
        self._selection_dialog: Adw.AlertDialog | None = None
        self._selection_response_handler: int | None = None

    @property
    def close_prompt_open(self) -> bool:
        return self._close_dialog is not None

    def request_close(self) -> None:
        self._cancel_selection_prompt()
        if not self._state.is_dirty:
            self._close()
            return
        self._show_close_prompt()

    def request_selection_change(self, target: EditorSelection | None) -> bool:
        """Select ``target`` now, or queue it behind an unsaved-change prompt."""

        if self._close_dialog is not None:
            self._callbacks.restore_active_selection()
            return False
        if self._selection_dialog is not None:
            self._callbacks.restore_active_selection()
            return False
        if target == self._state.active_selection:
            return True
        if not self._state.selection_change_needs_confirmation(target):
            self._callbacks.select_pending_target(target)
            return True

        self._state.queue_transition(target)
        self._callbacks.restore_active_selection()
        self._show_selection_prompt(restarting_new_item=False)
        return False

    def request_new_item(self, *, pristine_draft: bool) -> bool:
        """Start a new draft now, or guard replacement of the current draft."""

        if self._close_dialog is not None:
            self._callbacks.restore_active_selection()
            return False
        if self._selection_dialog is not None:
            self._callbacks.restore_active_selection()
            return False
        if not self._state.new_draft_restart_needs_confirmation(
            pristine_draft=pristine_draft
        ):
            self._callbacks.restart_new_item()
            return True

        self._state.queue_transition(
            EditorSelection.new_item(),
            restart_new_item=True,
        )
        self._callbacks.restore_active_selection()
        self._show_selection_prompt(restarting_new_item=True)
        return False

    def _show_close_prompt(self) -> None:
        if self._close_dialog is not None:
            return
        dialog = self._build_prompt(self._messages.close_body)
        dialog.connect("response", self._on_close_response)
        self._close_dialog = dialog
        dialog.present(self._parent)

    def _show_selection_prompt(self, *, restarting_new_item: bool) -> None:
        body = (
            self._messages.restart_new_item_body
            if restarting_new_item
            else self._messages.switch_body
        )
        dialog = self._build_prompt(body)
        response_handler = dialog.connect("response", self._on_selection_response)
        self._selection_dialog = dialog
        self._selection_response_handler = response_handler
        dialog.present(self._parent)

    def _build_prompt(self, body: str) -> Adw.AlertDialog:
        dialog = Adw.AlertDialog()
        dialog.set_heading(self._messages.heading)
        dialog.set_body(body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("discard", "Discard")
        dialog.add_response("save", "Save")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        return dialog

    def _on_close_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        if _dialog is not self._close_dialog:
            return
        self._close_dialog = None
        if response == "discard":
            self._state.mark_clean()
            self._callbacks.update_buttons()
            self._close()
            return
        if response == "save" and self._callbacks.save_current():
            self._close()

    def _on_selection_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        if _dialog is not self._selection_dialog:
            return
        pending = self._state.take_pending_transition()
        self._selection_dialog = None
        self._selection_response_handler = None
        if pending is None:
            self._callbacks.restore_active_selection()
            return

        if response == "discard":
            self._state.mark_clean()
            self._callbacks.update_buttons()
            self._apply_transition(pending.selection, pending.restart_new_item)
            return
        if response == "save" and self._callbacks.save_current():
            self._apply_transition(pending.selection, pending.restart_new_item)
            return
        self._callbacks.restore_active_selection()

    def _apply_transition(
        self,
        selection: EditorSelection | None,
        restart_new_item: bool,
    ) -> None:
        if restart_new_item:
            self._callbacks.restart_new_item()
            return
        self._callbacks.select_pending_target(selection)

    def _cancel_selection_prompt(self) -> None:
        dialog = self._selection_dialog
        if dialog is None:
            self._state.clear_pending_transition()
            return

        response_handler = self._selection_response_handler
        self._selection_dialog = None
        self._selection_response_handler = None
        self._state.clear_pending_transition()
        if response_handler is not None:
            dialog.disconnect(response_handler)
        dialog.force_close()

    def _close(self) -> None:
        self._cancel_selection_prompt()
        if self._callbacks.before_close is not None:
            self._callbacks.before_close()
        self._callbacks.close_editor()
