"""Composed GTK dialog for managing reusable Super Keys."""

import logging
from typing import cast

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import (  # pyright: ignore[reportAttributeAccessIssue]
    Adw,  # pyright: ignore[reportAttributeAccessIssue]
    Gdk,  # pyright: ignore[reportAttributeAccessIssue]
    GObject,  # pyright: ignore[reportAttributeAccessIssue]
    Gtk,  # pyright: ignore[reportAttributeAccessIssue]
)

from keymasq import __version__
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.superkeys import SuperkeyAction, SuperkeyConfig
from keymasq.gui.widgets.action_sequence import ActionSequenceDialog, ActionSequenceMode
from keymasq.gui.widgets.docs_links import docs_page_url
from keymasq.gui.widgets.fuzzy_search import superkey_search_text
from keymasq.gui.widgets.managed_editor.shell import (
    ManagedEditorCallbacks,
    ManagedEditorLabels,
    ManagedEditorShell,
)
from keymasq.gui.widgets.managed_editor.state import EditorSelection, EditorState
from keymasq.gui.widgets.managed_editor.unsaved import (
    UnsavedChangesCallbacks,
    UnsavedChangesController,
    UnsavedChangesMessages,
)
from keymasq.gui.widgets.superkey_editor.action_slot import ActionSlot
from keymasq.gui.widgets.superkey_editor.draft import SuperkeyDraft
from keymasq.gui.widgets.superkey_editor.persistence import SuperkeyPersistence
from keymasq.gui.widgets.superkey_editor.view import SuperkeyEditorView
from keymasq.session.profile.manager import ProfileManager
from keymasq.session.superkeys import SuperkeyManager

log = logging.getLogger("keymasq.gui.widgets.superkey_editor.dialog")


def _superkeys_docs_url() -> str:
    return docs_page_url("SUPERKEYS", version=__version__)


class SuperkeyDialog(Adw.Dialog):
    """Coordinate list selection, one typed draft, persistence, and prompts."""

    __gsignals__ = {
        "superkey-saved": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "superkey-deleted": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(
        self,
        parent: Gtk.Window,
        profile_manager: ProfileManager | None = None,
        *,
        manager: SuperkeyManager | None = None,
        persistence: SuperkeyPersistence | None = None,
    ) -> None:
        super().__init__(title="Manage Super Keys", content_width=920, content_height=640)
        self._parent = parent
        self.manager = manager or SuperkeyManager()
        self._persistence = persistence or SuperkeyPersistence(self.manager)
        self.profile_manager = profile_manager
        self.state = EditorState()
        self._current_config: SuperkeyConfig | None = None
        self._original_name: str | None = None

        def save_item() -> None:
            self._save_current_superkey()

        self.shell = ManagedEditorShell(
            state=self.state,
            labels=ManagedEditorLabels(
                sidebar_title="Super Keys",
                search_placeholder="Search Super Keys",
                search_tooltip="Filter Super Keys by name, description, or mode",
                documentation_tooltip="Open Super Keys documentation",
                add_tooltip="Add a new Super Key",
            ),
            callbacks=ManagedEditorCallbacks(
                selection_changed=self._on_selection_changed,
                open_documentation=self._open_documentation,
                add_item=self._request_new_superkey,
                delete_item=self._request_delete,
                save_item=save_item,
                revert_item=self._revert,
                close_editor=self._request_close,
            ),
        )
        self.editor = SuperkeyEditorView(
            modified=self._mark_modified,
            edit_pattern_slot=self._edit_pattern_slot,
            edit_overload_slot=self._edit_overload_slot,
        )
        self.shell.append_editor_widget(self.editor)
        self.set_child(self.shell.root)

        def force_close() -> None:
            self.force_close()

        self.unsaved = UnsavedChangesController(
            parent=self,
            state=self.state,
            messages=UnsavedChangesMessages(
                heading="Unsaved Super Key Changes",
                close_body="Save your changes before closing, or discard them?",
                switch_body="Save your changes before switching, or discard them?",
                restart_new_item_body=(
                    "Save your changes before starting a new Super Key, or discard them?"
                ),
            ),
            callbacks=UnsavedChangesCallbacks(
                save_current=self._save_current_superkey,
                close_editor=force_close,
                select_pending_target=self._select_target,
                restart_new_item=self._restart_new_superkey,
                restore_active_selection=self._restore_active_selection,
                update_buttons=self._update_buttons,
            ),
        )

        self._load_superkeys()
        self._setup_shortcuts()

    def _setup_shortcuts(self) -> None:
        key_controller = Gtk.EventControllerKey()
        key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)

    def _on_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if keyval in (Gdk.KEY_f, Gdk.KEY_F) and state & Gdk.ModifierType.CONTROL_MASK:
            self.shell.show_search()
            return True
        if keyval == Gdk.KEY_Escape and self.shell.search_entry.get_visible():
            self.shell.hide_search()
            return True
        if keyval == Gdk.KEY_Escape:
            self._request_close()
            return True
        return False

    def do_close_attempt(self) -> None:
        self._request_close()

    def start_new_superkey(self) -> None:
        self._request_new_superkey()

    def select_superkey_by_name(self, name: str) -> None:
        selection = EditorSelection.saved_item(name)
        if selection == self.state.active_selection:
            return
        if self.shell.row_for_selection(selection) is not None:
            self.unsaved.request_selection_change(selection)

    def _load_superkeys(self, preferred: EditorSelection | None = None) -> None:
        names = self.manager.list_superkeys()
        configs = self.manager.get_all_superkeys()
        new_selection = EditorSelection.new_item()

        self.state.begin_selection_sync()
        try:
            self.shell.clear_rows()
            for name in names:
                self.shell.append_text_row(
                    EditorSelection.saved_item(name),
                    label=name,
                    search_text=superkey_search_text(configs.get(name), name),
                )
            self.shell.append_text_row(
                new_selection,
                label="+ Add",
                search_text="add new super key",
                tooltip="Add a new Super Key",
            )
            if preferred is not None and self.shell.row_for_selection(preferred) is not None:
                selected = preferred
            elif names:
                selected = EditorSelection.saved_item(names[0])
            else:
                selected = new_selection
            self.shell.select(selected)
        finally:
            self.state.end_selection_sync()
        self._activate_selection(selected)

    def _on_selection_changed(self, selection: EditorSelection | None) -> None:
        if selection is not None and selection.is_new_item:
            self._request_new_superkey()
            return
        self.unsaved.request_selection_change(selection)

    def _select_target(self, selection: EditorSelection | None) -> None:
        resolved_selection = selection
        self.state.begin_selection_sync()
        try:
            if not self.shell.select(selection):
                resolved_selection = None
                self.shell.select(None)
        finally:
            self.state.end_selection_sync()
        self._activate_selection(resolved_selection)

    def _restore_active_selection(self) -> None:
        self.shell.restore_active_selection()

    def _activate_selection(self, selection: EditorSelection | None) -> None:
        if selection is None:
            self._current_config = None
            self._original_name = None
            self.state.activate(None)
            self.state.mark_clean()
            self._update_buttons()
            return
        if selection.is_new_item:
            self._begin_new_superkey()
            return

        name = selection.item_id
        config = self.manager.get_superkey(name or "")
        if config is None:
            self._current_config = None
            self._original_name = None
            self.state.activate(None)
            self.state.mark_clean()
            self._update_buttons()
            return
        self._current_config = config
        self._original_name = config.name
        self.editor.populate(SuperkeyDraft.from_config(config))
        self.state.activate(selection)
        self.state.mark_clean()
        self._update_buttons()

    def _begin_new_superkey(self) -> None:
        self._current_config = None
        self._original_name = None
        self.editor.populate(SuperkeyDraft.new())
        self.state.activate(EditorSelection.new_item())
        self.state.mark_dirty()
        self._update_buttons()
        self.editor.focus_name()

    def _restart_new_superkey(self) -> None:
        self._select_target(EditorSelection.new_item())

    def _request_new_superkey(self) -> None:
        self.unsaved.request_new_item(pristine_draft=self.editor.draft().is_pristine_new_draft())

    def _mark_modified(self) -> None:
        self.state.mark_dirty()
        self._update_buttons()

    def _update_buttons(self) -> None:
        dirty = self.state.is_dirty
        self.shell.revert_button.set_sensitive(dirty)
        self.shell.save_button.set_sensitive(dirty)
        selection = self.state.active_selection
        self.shell.delete_button.set_sensitive(
            selection is not None and not selection.is_new_item and self._original_name is not None
        )
        self.shell.editor_container.set_sensitive(selection is not None)
        self.set_can_close(not dirty)

    def _revert(self) -> None:
        if self._current_config is not None:
            self.editor.populate(SuperkeyDraft.from_config(self._current_config))
        elif self.state.active_selection is not None and self.state.active_selection.is_new_item:
            self.editor.populate(SuperkeyDraft.new())
        self.state.mark_clean()
        self._update_buttons()

    def _edit_pattern_slot(self, slot: ActionSlot[SuperkeyAction]) -> None:
        title = f"Edit {slot.row.get_title()} Actions"
        dialog = ActionSequenceDialog(
            self._parent,
            title,
            ActionSequenceMode.SUPERKEY_PATTERN,
            current_actions=slot.actions,
            action_key=slot.action_key,
        )
        dialog.connect("actions-selected", self._on_pattern_actions_selected, slot)
        dialog.present(self._parent)

    def _edit_overload_slot(self, slot: ActionSlot[MappingAction]) -> None:
        dialog = ActionSequenceDialog(
            self._parent,
            f"Edit {slot.row.get_title()}",
            ActionSequenceMode.MAPPING,
            current_actions=slot.actions,
            action_key=slot.action_key,
        )
        dialog.connect("actions-selected", self._on_overload_actions_selected, slot)
        dialog.present(self._parent)

    def _on_pattern_actions_selected(
        self,
        _dialog: ActionSequenceDialog,
        actions: object,
        slot: ActionSlot[SuperkeyAction],
    ) -> None:
        slot.set_actions(cast(list[SuperkeyAction], actions), notify=True)

    def _on_overload_actions_selected(
        self,
        _dialog: ActionSequenceDialog,
        actions: object,
        slot: ActionSlot[MappingAction],
    ) -> None:
        slot.set_actions(cast(list[MappingAction], actions), notify=True)

    def _save_current_superkey(self) -> bool:
        if not self.editor.name_entry.get_text().strip():
            return False

        try:
            config = self.editor.draft().to_config()
            self._persistence.save(
                config,
                replacing_name=self._original_name,
                profiles=self.profile_manager,
            )
        except ValueError as exc:
            self._show_save_error(str(exc))
            return False

        self._current_config = config
        self.state.mark_clean()
        selection = EditorSelection.saved_item(config.name)
        self._load_superkeys(selection)
        self.emit("superkey-saved", config.name)
        return True

    def _request_delete(self) -> None:
        name = self._original_name
        if name is None:
            return
        if self.profile_manager is not None:
            affected = self.profile_manager.find_profiles_using_superkey(name)
            if affected:
                dialog = Adw.AlertDialog()
                dialog.set_heading("Delete Super Key?")
                dialog.set_body(
                    f"'{name}' is used in {len(affected)} profile(s). "
                    "It will be replaced with Suppress in those profiles."
                )
                dialog.add_response("cancel", "Cancel")
                dialog.add_response("delete", "Delete")
                dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
                dialog.connect("response", self._on_delete_confirmed, name)
                dialog.present(self._parent)
                return
        self._delete(name)

    def _on_delete_confirmed(
        self,
        _dialog: Adw.AlertDialog,
        response: str,
        name: str,
    ) -> None:
        if response == "delete":
            self._delete(name)

    def _delete(self, name: str) -> bool:
        if not self._persistence.delete(name, profiles=self.profile_manager):
            return False
        self._load_superkeys()
        self.emit("superkey-deleted", name)
        return True

    def _show_save_error(self, message: str) -> None:
        dialog = Adw.AlertDialog()
        dialog.set_heading("Unable To Save Super Key")
        dialog.set_body(message)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self)

    def _request_close(self) -> None:
        self.unsaved.request_close()

    def _open_documentation(self) -> None:
        url = _superkeys_docs_url()
        try:
            launcher = Gtk.UriLauncher.new(url)
            launcher.launch(None, None, None)
        except Exception:
            log.exception("Could not open Super Keys documentation %s", url)
