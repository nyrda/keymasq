"""Analog-control manager dialog composition root."""

from __future__ import annotations

import logging
from collections.abc import Callable

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
from keymasq.common.model.analog import AnalogControlConfig
from keymasq.common.slurp import get_slurp_capture
from keymasq.gui.compositor_state import session_compositor_id
from keymasq.gui.widgets.action_sequence import ActionSequenceDialog, ActionSequenceMode
from keymasq.gui.widgets.analog_control.draft import ControlDraft
from keymasq.gui.widgets.analog_control.options import (
    analog_control_search_text,
    group_analog_control_names,
)
from keymasq.gui.widgets.analog_control.persistence import AnalogControlPersistence
from keymasq.gui.widgets.analog_control.view import AnalogControlEditorView, OutputChoicesLoader
from keymasq.gui.widgets.docs_links import actions_docs_url
from keymasq.gui.widgets.gamepad_output_choices import virtual_gamepad_count
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
from keymasq.gui.widgets.position_capture import (
    PositionCaptureController,
    PositionCaptureMessages,
)
from keymasq.gui.widgets.spin_inputs import SPLIT_DESYNC_KEYS
from keymasq.session.analog_controls import AnalogControlManager
from keymasq.session.profile.manager import ProfileManager

log = logging.getLogger(__name__)


def analog_controls_docs_url() -> str:
    return actions_docs_url("analog-controls", version=__version__)


def _set_capture_status(label: Gtk.Label | None, text: str, error: bool) -> None:
    if label is None:
        return
    label.set_text(text)
    if error:
        label.add_css_class("capture-error-label")
    else:
        label.remove_css_class("capture-error-label")


class AnalogControlDialog(Adw.Dialog):
    """Compose catalog, editor view, persistence, and guarded transitions."""

    __gsignals__ = {
        "analog-control-saved": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "analog-control-deleted": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(
        self,
        parent: Gtk.Window,
        profile_manager: ProfileManager | None = None,
        *,
        manager: AnalogControlManager | None = None,
        persistence: AnalogControlPersistence | None = None,
        position_capture: PositionCaptureController | None = None,
        output_choices_loader: OutputChoicesLoader | None = None,
        output_count_loader: Callable[[], int] = virtual_gamepad_count,
    ) -> None:
        super().__init__(title="Manage Analog Controls", content_width=920, content_height=640)
        self._parent = parent
        self.profile_manager = profile_manager
        self.manager = manager or AnalogControlManager()
        self._persistence = persistence or AnalogControlPersistence(self.manager)
        self.state = EditorState()
        self._current_config: AnalogControlConfig | None = None
        self._current_name: str | None = None

        def save_item() -> None:
            self._save_current()

        self.shell = ManagedEditorShell(
            state=self.state,
            labels=ManagedEditorLabels(
                sidebar_title="Analog Controls",
                search_placeholder="Search Analog Controls",
                search_tooltip=(
                    "Filter Analog Controls by name, description, input type, or output"
                ),
                documentation_tooltip="Open Analog Controls documentation",
                add_tooltip="Add a new Analog Control",
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
        capture = position_capture or self._create_position_capture()
        if output_choices_loader is None:
            self.editor = AnalogControlEditorView(
                position_capture=capture,
                on_modified=self._mark_dirty,
                open_threshold_actions=self._open_threshold_actions,
                output_count_loader=output_count_loader,
            )
        else:
            self.editor = AnalogControlEditorView(
                position_capture=capture,
                on_modified=self._mark_dirty,
                open_threshold_actions=self._open_threshold_actions,
                output_choices_loader=output_choices_loader,
                output_count_loader=output_count_loader,
            )
        self.shell.append_editor_widget(self.editor)
        self.set_child(self.shell.root)

        def close_editor() -> None:
            self.force_close()

        self.unsaved = UnsavedChangesController(
            parent=self,
            state=self.state,
            messages=UnsavedChangesMessages(
                heading="Unsaved Analog Control Changes",
                close_body="Save your changes before closing, or discard them?",
                switch_body="Save your changes before switching, or discard them?",
                restart_new_item_body=(
                    "Save your changes before starting a new Analog Control, or discard them?"
                ),
            ),
            callbacks=UnsavedChangesCallbacks(
                save_current=self._save_current,
                close_editor=close_editor,
                select_pending_target=self._activate_selection,
                restart_new_item=self._restart_new_control,
                restore_active_selection=self._restore_active_selection,
                update_buttons=self._update_buttons,
                before_close=self.editor.cancel_capture,
            ),
        )
        self.connect("closed", self._on_closed)
        self._setup_shortcuts()
        self._load_controls()

    def _create_position_capture(self) -> PositionCaptureController:
        slurp = get_slurp_capture()
        slurp.set_compositor(session_compositor_id())
        return PositionCaptureController(
            slurp_capture=slurp,
            slurp_available=slurp.available,
            set_status=_set_capture_status,
            messages=PositionCaptureMessages(slurp_success="", response_success=""),
        )

    def _setup_shortcuts(self) -> None:
        controller = Gtk.EventControllerKey()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect("key-pressed", self._on_key_pressed)
        controller.connect("key-released", self._on_key_released)
        self.add_controller(controller)

    def _on_key_pressed(
        self,
        _controller: Gtk.EventControllerKey | None,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType | int,
    ) -> bool:
        if keyval in SPLIT_DESYNC_KEYS:
            self.editor.set_modifier_key(keyval, True)
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

    def _on_key_released(
        self,
        _controller: Gtk.EventControllerKey | None,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType | int,
    ) -> None:
        if keyval in SPLIT_DESYNC_KEYS:
            self.editor.set_modifier_key(keyval, False)

    def do_close_attempt(self) -> None:
        self._request_close()

    def _on_closed(self, _dialog: Adw.Dialog) -> None:
        self.editor.cancel_capture()

    def _load_controls(self, preferred: EditorSelection | None = None) -> None:
        self.state.begin_selection_sync()
        try:
            self.shell.clear_rows()
            names = self.manager.list_analog_controls()
            configs = self.manager.get_all_analog_controls()
            first_saved: EditorSelection | None = None
            for heading, group_names in group_analog_control_names(names, configs):
                self.shell.append_heading_row(heading, search_text=heading)
                for name in group_names:
                    selection = EditorSelection.saved_item(name)
                    self.shell.append_text_row(
                        selection,
                        label=name,
                        search_text=analog_control_search_text(configs.get(name), name, heading),
                    )
                    if first_saved is None:
                        first_saved = selection
            new_selection = EditorSelection.new_item()
            self.shell.append_text_row(
                new_selection,
                label="+ Add",
                search_text="add new analog control",
                tooltip="Add a new Analog Control",
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
        if selection != self.state.active_selection:
            self.editor.cancel_capture()
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
        name = selection.item_id
        config = self.manager.get_analog_control(name or "")
        if config is None:
            self._activate_selection(None)
            return
        self._current_config = config
        self._current_name = config.name
        self.editor.load(ControlDraft.from_config(config))
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
        self.editor.load(ControlDraft.new())
        self.shell.editor_container.set_sensitive(True)
        self.state.activate(selection)
        self.state.mark_dirty()
        self._update_buttons()
        self.editor.name_entry.grab_focus()

    def _restart_new_control(self) -> None:
        self.editor.cancel_capture()
        self._begin_new_control()

    def _request_new_control(self) -> None:
        self.unsaved.request_new_item(pristine_draft=self._is_pristine_new_draft())

    def _is_pristine_new_draft(self) -> bool:
        active = self.state.active_selection
        return bool(
            active is not None
            and active.is_new_item
            and self.editor.draft().is_pristine_new_control()
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
        self.set_can_close(not dirty)

    def _revert_current(self) -> None:
        active = self.state.active_selection
        if active is None:
            return
        self.editor.cancel_capture()
        if active.is_new_item:
            self.editor.load(ControlDraft.new())
        elif self._current_config is not None:
            self.editor.load(ControlDraft.from_config(self._current_config))
        self.state.mark_clean()
        self._update_buttons()

    def _save_current(self) -> bool:
        try:
            self.editor.sync_thresholds_for_input_type()
            config = self.editor.draft().to_config()
            self._persistence.save(
                config,
                replacing_name=self._current_name,
                profiles=self.profile_manager,
            )
        except ValueError as error:
            self._show_save_error(str(error))
            return False

        self.editor.cancel_capture()
        self._current_config = config
        self._current_name = config.name
        self.state.mark_clean()
        selection = EditorSelection.saved_item(config.name)
        self._load_controls(selection)
        self.emit("analog-control-saved", config.name)
        return True

    def _delete_current(self) -> None:
        name = self._current_name
        if not name or not self._persistence.delete(name, profiles=self.profile_manager):
            return
        self.editor.cancel_capture()
        self.emit("analog-control-deleted", name)
        self._current_config = None
        self._current_name = None
        self.state.mark_clean()
        self._load_controls()

    def _open_threshold_actions(self, index: int) -> None:
        thresholds = self.editor.thresholds.thresholds
        if not 0 <= index < len(thresholds):
            return
        dialog = ActionSequenceDialog(
            self._parent,
            f"Edit Range {index + 1} Actions",
            ActionSequenceMode.MAPPING,
            current_actions=list(thresholds[index].actions),
            action_key="analog_threshold",
        )
        dialog.connect("actions-selected", self._on_threshold_actions_selected, index)
        dialog.present(self._parent)

    def _on_threshold_actions_selected(
        self,
        _dialog: Adw.Dialog,
        actions: list[object],
        index: int,
    ) -> None:
        self.editor.thresholds.actions_selected(actions, index)

    def _open_documentation(self) -> None:
        url = analog_controls_docs_url()
        try:
            Gtk.UriLauncher.new(url).launch(None, None, None)
        except Exception:
            log.exception("Could not open Analog Controls documentation %s", url)

    def _show_save_error(self, message: str) -> None:
        dialog = Adw.AlertDialog(
            heading="Unable To Save Analog Control",
            body=message,
        )
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self)

    def _request_close(self) -> None:
        self.unsaved.request_close()

    def select_control_by_name(self, name: str) -> None:
        """Select a saved analog control when opening the manager from a mapping."""

        selection = EditorSelection.saved_item(name)
        if self.shell.select(selection):
            return
