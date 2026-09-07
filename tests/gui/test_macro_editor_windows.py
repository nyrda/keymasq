# ruff: noqa: E402, I001
"""Independent editor entry points and document lifetime."""

import weakref
from uuid import uuid4

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, Gtk

from keymasq.gui.session_client import GuiTaskResult
from keymasq.gui.widgets.macro_editor import dialog as editor_module
from keymasq.gui.widgets.macro_editor.model import EditableControl, EditableEvent
from keymasq.gui.widgets.macro_manager_dialog import MacroManagerDialog
from tests.gui.macro_editor_dialog_support import _FakeSlurpCapture


@pytest.fixture
def editor_parent(monkeypatch):
    monkeypatch.setattr(editor_module, "_editors", weakref.WeakSet())
    monkeypatch.setattr(editor_module, "get_slurp_capture", _FakeSlurpCapture)
    monkeypatch.setattr(editor_module.MacroEditorMixin, "_load_initial_state_async", lambda _: None)
    app = Adw.Application(
        application_id=f"tools.keymasq.test.Editor{uuid4().hex}",
        flags=Gio.ApplicationFlags.NON_UNIQUE,
    )
    app.register(None)
    parent = Adw.ApplicationWindow(application=app)
    parent.set_default_size(1000, 800)
    parent.present()
    yield parent
    for editor in list(editor_module._editors):
        editor._force_close_without_warning()
    parent.destroy()


def load_empty(editor, name):
    editor._on_initial_state_loaded(
        GuiTaskResult(
            value={
                "macro": {"name": name, "revision": 2, "events": [], "duration_us": 0},
            }
        )
    )


def test_default_dialog_and_explicit_independent_window(editor_parent):
    embedded = editor_module.get_macro_editor(editor_parent, "parent")
    embedded.present(editor_parent)
    window = editor_module.get_macro_editor(editor_parent, "child", standalone=True)
    window.present(editor_parent)

    assert isinstance(embedded, Adw.Dialog)
    assert embedded.get_root() is editor_parent
    assert isinstance(window, editor_module.MacroEditorWindow)
    assert window.get_application() is editor_parent.get_application()
    assert window.get_transient_for() is None
    assert not window.get_modal()
    assert window.get_resizable()
    assert window.get_root() is window
    embedded._force_close_without_warning()
    assert window.get_visible()
    assert not window._dialog_closed


def test_child_pencil_opens_selected_macro_and_reuses_unsaved_editor(editor_parent):
    parent = editor_module.get_macro_editor(editor_parent, "parent")
    parent.present(editor_parent)
    control = EditableControl(mode="macro_parallel", t_us=0, macro_name="child")
    parent._timeline._selected = control
    parent._on_selection_changed(control)
    assert parent._edit_child_macro_btn.get_visible()
    parent._edit_child_macro_btn.emit("clicked")
    child = next(e for e in editor_module._editors if e._macro_name == "child")
    assert isinstance(child, editor_module.MacroEditorWindow)
    load_empty(child, "child")
    child._name_entry.set_text("unsaved_name")
    history = child._edit_history

    parent._edit_child_macro_btn.emit("clicked")
    assert len(editor_module._editors) == 2
    assert child._name_entry.get_text() == "unsaved_name"
    assert child._edit_history is history
    assert editor_module.get_macro_editor(editor_parent, "child") is child

    parent._on_selection_changed(
        EditableEvent(device_type="keyboard", ev_type=1, code=30, press_t_us=0, release_t_us=1000)
    )
    assert not parent._edit_child_macro_btn.get_visible()
    control.macro_name = ""
    parent._on_selection_changed(control)
    assert not parent._edit_child_macro_btn.get_sensitive()


def test_window_prompts_are_owned_by_editor_and_close_can_be_cancelled(editor_parent, monkeypatch):
    window = editor_module.get_macro_editor(editor_parent, "child", standalone=True)
    window.present()
    load_empty(window, "child")
    alerts = []
    monkeypatch.setattr(Adw.AlertDialog, "present", lambda dialog, parent: alerts.append(parent))
    window._show_save_error("Example error")
    assert alerts == [window]
    window._name_entry.set_text("unsaved")
    window.emit("close-request")
    assert alerts[-1] is window
    window._on_unsaved_close_response(window._close_warning_dialog, "cancel")
    assert window.get_visible()
    assert not window._dialog_closed
    window.emit("close-request")
    window._on_unsaved_close_response(window._close_warning_dialog, "discard")
    assert window._dialog_closed
    assert window not in editor_module._editors


def test_apply_keeps_window_open_updates_revision_and_reuses_saved_name(editor_parent, monkeypatch):
    window = editor_module.get_macro_editor(editor_parent, "child", standalone=True)
    window.present()
    load_empty(window, "child")
    requests = []
    saved = []

    def request(payload):
        requests.append(payload)
        return {"status": "ok", "macro": {**payload["macro"], "revision": 3}}

    def run(worker, callback, *, on_start=None, on_done=None):
        if on_start:
            on_start()
        callback(GuiTaskResult(value=worker()))
        if on_done:
            on_done()

    monkeypatch.setattr(window, "_session_request", request)
    monkeypatch.setattr(window, "_run_gui_task", run)
    monkeypatch.setattr(editor_module, "notify_session_reload_async", lambda: None)
    window.connect("saved", lambda _: saved.append(True))
    window._on_apply(Gtk.Button())
    assert requests[0]["expected_revision"] == 2
    assert window._macro_data["revision"] == 3
    assert saved == [True]
    assert window.get_visible()
    assert not window._has_pending_changes()
    window._apply_saved_macro_state(
        {"macro": {**window._macro_data, "name": "renamed"}},
        "renamed",
        {},
    )
    assert editor_module.get_macro_editor(editor_parent, "renamed", standalone=True) is window


def test_quit_stops_on_cancel_and_waits_for_discard(editor_parent, monkeypatch):
    window = editor_module.get_macro_editor(editor_parent, "child", standalone=True)
    window.present()
    load_empty(window, "child")
    window._name_entry.set_text("unsaved")
    monkeypatch.setattr(Adw.AlertDialog, "present", lambda *_: None)
    finished = []
    editor_module.close_macro_editors(
        editor_parent.get_application(), lambda: finished.append(True)
    )
    window._on_unsaved_close_response(window._close_warning_dialog, "cancel")
    assert finished == []
    assert window.get_visible()
    editor_module.close_macro_editors(
        editor_parent.get_application(), lambda: finished.append(True)
    )
    window._on_unsaved_close_response(window._close_warning_dialog, "discard")
    assert finished == [True]


def test_failed_child_load_shows_error_before_destroying_window(editor_parent, monkeypatch):
    window = editor_module.get_macro_editor(editor_parent, "missing", standalone=True)
    window.present()
    alerts = []
    monkeypatch.setattr(
        Adw.AlertDialog, "present", lambda dialog, parent: alerts.append((dialog, parent))
    )
    window._on_initial_state_loaded(GuiTaskResult(value={"macro_load_error": "Macro not found"}))
    alert, parent = alerts[0]
    assert parent is window
    assert window.get_visible()
    assert not window._dialog_closed
    alert.emit("response", "ok")
    assert window._dialog_closed
    assert window not in editor_module._editors


def test_shift_edit_uses_button_activation_and_resets_cancelled_click(editor_parent, monkeypatch):
    from gi.repository import GLib

    monkeypatch.setattr(MacroManagerDialog, "_load_initial_state", lambda _: False)
    manager = MacroManagerDialog(editor_parent)
    opened = []
    monkeypatch.setattr(manager, "_open_macro_editor", lambda name, **kw: opened.append((name, kw)))
    button = Gtk.Button()
    pending = []
    monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: pending.append((callback, args)))

    class Gesture:
        def get_widget(self):
            return button

        def get_current_event_state(self):
            return Gdk.ModifierType.SHIFT_MASK

    gesture = Gesture()
    manager._on_edit_pressed(gesture, 1, 10, 10)
    assert opened == []
    manager._on_edit_released(gesture, 1, 10, 10)
    manager._on_edit_clicked(button, "child")
    assert opened == [("child", {"standalone": True})]
    for callback, args in pending:
        callback(*args)
    pending.clear()

    # A cancelled pointer click must not affect later keyboard activation.
    opened.clear()
    manager._on_edit_pressed(gesture, 1, 10, 10)
    manager._on_edit_released(gesture, 1, 50, 10)
    for callback, args in pending:
        callback(*args)
    assert opened == []
    manager._on_edit_clicked(button, "child")
    assert opened == [("child", {})]
    opened.clear()
    manager._on_edit_pressed(gesture, 1, 10, 10)
    manager._on_edit_key_pressed(gesture, 0, 0, Gdk.ModifierType(0))
    manager._on_edit_clicked(button, "child")
    assert opened == [("child", {})]
