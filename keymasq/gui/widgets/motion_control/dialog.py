"""Managed editor dialog for reusable Motion Controls."""

import logging

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
from keymasq.common.model.motion import MotionControlConfig
from keymasq.gui.widgets.docs_links import docs_page_url
from keymasq.gui.widgets.fuzzy_search import start_search_from_keypress
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
from keymasq.gui.widgets.motion_control.draft import MotionControlDraft
from keymasq.gui.widgets.motion_control.persistence import MotionControlPersistence
from keymasq.gui.widgets.motion_control.view import MotionControlEditorView
from keymasq.session.motion_controls import MotionControlManager
from keymasq.session.profile.manager import ProfileManager

log = logging.getLogger(__name__)


def motion_controls_docs_url() -> str:
    return docs_page_url("MOTION_CONTROLS", version=__version__)


def motion_control_search_text(config: MotionControlConfig | None, name: str) -> str:
    if config is None:
        return name
    mode = {
        "mouse": "gyro mouse",
        "gamepad": "gyro controller gamepad stick",
        "tilt_mouse": "accelerometer tilt continuous mouse",
        "tilt_gamepad": "accelerometer tilt controller gamepad stick",
        "area_mouse": "accelerometer tilt area mouse pointer",
    }[config.mode]
    return " ".join((config.name, config.description or "", mode))


class MotionControlDialog(Adw.Dialog):
    """Compose the shared manager shell with the Motion Control editor."""

    __gsignals__ = {
        "motion-control-saved": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "motion-control-deleted": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(
        self,
        parent: Gtk.Widget,
        profile_manager: ProfileManager | None = None,
        *,
        manager: MotionControlManager | None = None,
        persistence: MotionControlPersistence | None = None,
    ) -> None:
        super().__init__(title="Manage Motion Controls", content_width=920, content_height=640)
        self._parent = parent
        self.profile_manager = profile_manager
        self.manager = manager or MotionControlManager()
        self._persistence = persistence or MotionControlPersistence(self.manager)
        self.state = EditorState()
        self._current_config: MotionControlConfig | None = None
        self._current_name: str | None = None

        def save_item() -> None:
            self._save_current()

        self.shell = ManagedEditorShell(
            state=self.state,
            labels=ManagedEditorLabels(
                sidebar_title="Motion Controls",
                search_placeholder="Search Motion Controls",
                search_tooltip="Filter Motion Controls by name, description, or output",
                documentation_tooltip="Open Motion Controls documentation",
                add_tooltip="Add a new Motion Control",
            ),
            callbacks=ManagedEditorCallbacks(
                selection_changed=self._on_selection_changed,
                open_documentation=self._open_documentation,
                add_item=self._request_new_control,
                delete_item=self._delete_current,
                save_item=save_item,
                revert_item=self._revert_current,
                close_editor=self._request_close,
            ),
        )
        self.editor = MotionControlEditorView(on_modified=self._mark_dirty)
        self.shell.append_editor_widget(self.editor)
        self.set_child(self.shell.root)

        self.unsaved = UnsavedChangesController(
            parent=self,
            state=self.state,
            messages=UnsavedChangesMessages(
                heading="Unsaved Motion Control Changes",
                close_body="Save your changes before closing, or discard them?",
                switch_body="Save your changes before switching, or discard them?",
                restart_new_item_body=(
                    "Save your changes before starting a new Motion Control, or discard them?"
                ),
            ),
            callbacks=UnsavedChangesCallbacks(
                save_current=self._save_current,
                close_editor=self.force_close,
                select_pending_target=self._activate_selection,
                restart_new_item=self._restart_new_control,
                restore_active_selection=self._restore_active_selection,
                update_buttons=self._update_buttons,
            ),
        )
        self._setup_shortcuts()
        self._load_controls()

    def _setup_shortcuts(self) -> None:
        controller = Gtk.EventControllerKey()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(controller)

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
        return start_search_from_keypress(
            self,
            self.shell.search_entry,
            keyval,
            state,
            show_search=self.shell.show_search,
        )

    def do_close_attempt(self) -> None:
        self._request_close()

    def _load_controls(self, preferred: EditorSelection | None = None) -> None:
        names = self.manager.list_motion_controls()
        configs = self.manager.get_all_motion_controls()
        new_selection = EditorSelection.new_item()
        first_saved: EditorSelection | None = None
        self.state.begin_selection_sync()
        try:
            self.shell.clear_rows()
            for name in names:
                selection = EditorSelection.saved_item(name)
                self.shell.append_text_row(
                    selection,
                    label=name,
                    search_text=motion_control_search_text(configs.get(name), name),
                )
                if first_saved is None:
                    first_saved = selection
            self.shell.append_text_row(
                new_selection,
                label="+ Add",
                search_text="add new motion control",
                tooltip="Add a new Motion Control",
            )
        finally:
            self.state.end_selection_sync()
        target = preferred
        if target is None or self.shell.row_for_selection(target) is None:
            target = first_saved or new_selection
        self._activate_selection(target)

    def _on_selection_changed(self, selection: EditorSelection | None) -> None:
        if selection == self.state.active_selection and self.shell.editor_container.get_sensitive():
            return
        if selection is not None and selection.is_new_item:
            self.unsaved.request_new_item(pristine_draft=self._is_pristine_new_draft())
            return
        self.unsaved.request_selection_change(selection)

    def _activate_selection(self, selection: EditorSelection | None) -> None:
        self._sync_shell_selection(selection)
        if selection is None:
            self._current_config = None
            self._current_name = None
            self.state.activate(None)
            self.state.mark_clean()
            self.shell.editor_container.set_sensitive(False)
            self._update_buttons()
            return
        if selection.is_new_item:
            self._begin_new_control()
            return
        config = self.manager.get_motion_control(selection.item_id or "")
        if config is None:
            self._activate_selection(None)
            return
        self._current_config = config
        self._current_name = config.name
        self.editor.load(MotionControlDraft.from_config(config))
        self.shell.editor_container.set_sensitive(True)
        self.state.activate(selection)
        self.state.mark_clean()
        self._update_buttons()

    def _sync_shell_selection(self, selection: EditorSelection | None) -> None:
        self.state.begin_selection_sync()
        try:
            self.shell.select(selection)
        finally:
            self.state.end_selection_sync()

    def _restore_active_selection(self) -> None:
        self.shell.restore_active_selection()

    def _begin_new_control(self) -> None:
        selection = EditorSelection.new_item()
        self._sync_shell_selection(selection)
        self._current_config = None
        self._current_name = None
        self.editor.load(MotionControlDraft.new())
        self.shell.editor_container.set_sensitive(True)
        self.state.activate(selection)
        self.state.mark_dirty()
        self._update_buttons()
        self.editor.focus_name()

    def _restart_new_control(self) -> None:
        self._begin_new_control()

    def _request_new_control(self) -> None:
        self.unsaved.request_new_item(pristine_draft=self._is_pristine_new_draft())

    def _is_pristine_new_draft(self) -> bool:
        active = self.state.active_selection
        return bool(
            active is not None
            and active.is_new_item
            and self.editor.draft().is_pristine_new_draft()
        )

    def _mark_dirty(self) -> None:
        self.state.mark_dirty()
        self._update_buttons()

    def _update_buttons(self) -> None:
        dirty = self.state.is_dirty
        active = self.state.active_selection
        self.shell.save_button.set_sensitive(dirty)
        self.shell.revert_button.set_sensitive(dirty)
        self.shell.delete_button.set_sensitive(active is not None and not active.is_new_item)
        self.shell.editor_container.set_sensitive(active is not None)
        self.set_can_close(not dirty)

    def _revert_current(self) -> None:
        active = self.state.active_selection
        if active is None:
            return
        if active.is_new_item:
            self.editor.load(MotionControlDraft.new())
        elif self._current_config is not None:
            self.editor.load(MotionControlDraft.from_config(self._current_config))
        self.state.mark_clean()
        self._update_buttons()

    def _save_current(self) -> bool:
        try:
            config = self.editor.draft().to_config()
            self._persistence.save(
                config,
                replacing_name=self._current_name,
                profiles=self.profile_manager,
            )
        except ValueError as error:
            self._show_save_error(str(error))
            return False
        self._current_config = config
        self._current_name = config.name
        self.state.mark_clean()
        selection = EditorSelection.saved_item(config.name)
        self._load_controls(selection)
        self.emit("motion-control-saved", config.name)
        return True

    def _delete_current(self) -> None:
        name = self._current_name
        if not name or not self._persistence.delete(name, profiles=self.profile_manager):
            return
        self.emit("motion-control-deleted", name)
        self._current_config = None
        self._current_name = None
        self.state.mark_clean()
        self._load_controls()

    def _show_save_error(self, message: str) -> None:
        dialog = Adw.AlertDialog(heading="Unable To Save Motion Control", body=message)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self)

    def _request_close(self) -> None:
        self.unsaved.request_close()

    def _open_documentation(self) -> None:
        url = motion_controls_docs_url()
        try:
            Gtk.UriLauncher.new(url).launch(None, None, None)
        except Exception:
            log.exception("Could not open Motion Controls documentation %s", url)

    def select_control_by_name(self, name: str) -> None:
        selection = EditorSelection.saved_item(name)
        if selection == self.state.active_selection:
            return
        if self.shell.row_for_selection(selection) is not None:
            self.unsaved.request_selection_change(selection)
