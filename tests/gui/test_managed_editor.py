from collections.abc import Callable

import pytest

from keymasq.gui.widgets.managed_editor.state import (
    EditorSelection,
    EditorSelectionKind,
    EditorState,
)


def test_editor_selection_has_typed_new_and_saved_identities() -> None:
    new_item = EditorSelection.new_item()
    saved_item = EditorSelection.saved_item("Alpha")

    assert new_item.kind is EditorSelectionKind.NEW_ITEM
    assert new_item.item_id is None
    assert new_item.is_new_item is True
    assert saved_item.kind is EditorSelectionKind.SAVED_ITEM
    assert saved_item.item_id == "Alpha"
    assert saved_item.is_new_item is False

    with pytest.raises(ValueError, match="cannot have an item id"):
        EditorSelection(EditorSelectionKind.NEW_ITEM, "unexpected")
    with pytest.raises(ValueError, match="requires a non-empty item id"):
        EditorSelection.saved_item("")


def test_editor_state_guards_dirty_selection_and_consumes_pending_atomically() -> None:
    state = EditorState()
    alpha = EditorSelection.saved_item("Alpha")
    beta = EditorSelection.saved_item("Beta")
    state.activate(alpha)

    assert state.selection_change_needs_confirmation(beta) is False
    state.mark_dirty()
    assert state.selection_change_needs_confirmation(alpha) is False
    assert state.selection_change_needs_confirmation(beta) is True

    state.queue_transition(beta)
    pending = state.take_pending_transition()

    assert pending is not None
    assert pending.selection == beta
    assert pending.restart_new_item is False
    assert state.pending_transition is None
    assert state.take_pending_transition() is None


def test_editor_state_distinguishes_pristine_and_modified_new_draft_restarts() -> None:
    state = EditorState()
    state.activate(EditorSelection.new_item())
    state.mark_dirty()

    assert state.new_draft_restart_needs_confirmation(pristine_draft=True) is False
    assert state.new_draft_restart_needs_confirmation(pristine_draft=False) is True

    state.activate(EditorSelection.saved_item("Alpha"))
    assert state.new_draft_restart_needs_confirmation(pristine_draft=True) is True

    state.queue_transition(EditorSelection.new_item(), restart_new_item=True)
    pending = state.take_pending_transition()
    assert pending is not None
    assert pending.restart_new_item is True

    with pytest.raises(ValueError, match="must target the new-item selection"):
        state.queue_transition(EditorSelection.saved_item("Alpha"), restart_new_item=True)


def test_editor_state_balances_nested_selection_syncs() -> None:
    state = EditorState()

    state.begin_selection_sync()
    state.begin_selection_sync()
    assert state.selection_guard_suppressed is True
    state.end_selection_sync()
    assert state.selection_guard_suppressed is True
    state.end_selection_sync()
    assert state.selection_guard_suppressed is False

    with pytest.raises(RuntimeError, match="was not active"):
        state.end_selection_sync()


def _unsaved_controller(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: EditorState,
    save_current: Callable[[], bool],
) -> tuple[object, dict[str, list[object]], list[object]]:
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk

    import keymasq.gui.widgets.managed_editor.unsaved as unsaved

    calls: dict[str, list[object]] = {
        "close": [],
        "dismissed": [],
        "select": [],
        "restart": [],
        "restore": [],
        "buttons": [],
        "cleanup": [],
    }
    alerts: list[object] = []
    monkeypatch.setattr(
        unsaved.Adw.AlertDialog,
        "present",
        lambda alert, _parent: alerts.append(alert),
    )
    monkeypatch.setattr(
        unsaved.Adw.AlertDialog,
        "force_close",
        lambda alert: calls["dismissed"].append(alert),
    )
    controller = unsaved.UnsavedChangesController(
        parent=Gtk.Box(),
        state=state,
        messages=unsaved.UnsavedChangesMessages(
            heading="Unsaved Things",
            close_body="Close body",
            switch_body="Switch body",
            restart_new_item_body="Restart body",
        ),
        callbacks=unsaved.UnsavedChangesCallbacks(
            save_current=save_current,
            close_editor=lambda: calls["close"].append(True),
            select_pending_target=lambda target: calls["select"].append(target),
            restart_new_item=lambda: calls["restart"].append(True),
            restore_active_selection=lambda: calls["restore"].append(True),
            update_buttons=lambda: calls["buttons"].append(True),
            before_close=lambda: calls["cleanup"].append(True),
        ),
    )
    return controller, calls, alerts


def test_unsaved_controller_discards_dirty_close_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = EditorState()
    state.mark_dirty()
    controller, calls, alerts = _unsaved_controller(
        monkeypatch,
        state=state,
        save_current=lambda: True,
    )

    controller.request_close()
    controller.request_close()
    assert controller.close_prompt_open is True
    assert len(alerts) == 1

    alert = alerts[0]
    assert alert.get_heading() == "Unsaved Things"
    assert alert.get_body() == "Close body"
    alert.emit("response", "discard")

    assert state.is_dirty is False
    assert controller.close_prompt_open is False
    assert calls["buttons"] == [True]
    assert calls["cleanup"] == [True]
    assert calls["close"] == [True]


def test_unsaved_controller_applies_queued_selection_after_discard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = EditorState()
    alpha = EditorSelection.saved_item("Alpha")
    beta = EditorSelection.saved_item("Beta")
    state.activate(alpha)
    state.mark_dirty()
    controller, calls, alerts = _unsaved_controller(
        monkeypatch,
        state=state,
        save_current=lambda: True,
    )

    assert controller.request_selection_change(beta) is False
    assert calls["restore"] == [True]
    assert state.pending_transition is not None
    assert len(alerts) == 1
    assert alerts[0].get_body() == "Switch body"

    alerts[0].emit("response", "discard")

    assert state.is_dirty is False
    assert state.pending_transition is None
    assert calls["select"] == [beta]
    assert calls["buttons"] == [True]


def test_same_active_dirty_selection_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = EditorState()
    alpha = EditorSelection.saved_item("Alpha")
    state.activate(alpha)
    state.mark_dirty()
    controller, calls, alerts = _unsaved_controller(
        monkeypatch,
        state=state,
        save_current=lambda: True,
    )

    assert controller.request_selection_change(alpha) is True

    assert state.active_selection == alpha
    assert state.is_dirty is True
    assert state.pending_transition is None
    assert calls["select"] == []
    assert calls["restore"] == []
    assert alerts == []


def test_open_selection_prompt_preserves_its_original_pending_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = EditorState()
    alpha = EditorSelection.saved_item("Alpha")
    beta = EditorSelection.saved_item("Beta")
    gamma = EditorSelection.saved_item("Gamma")
    state.activate(alpha)
    state.mark_dirty()
    controller, calls, alerts = _unsaved_controller(
        monkeypatch,
        state=state,
        save_current=lambda: True,
    )

    assert controller.request_selection_change(beta) is False
    original_pending = state.pending_transition
    assert original_pending is not None

    assert controller.request_selection_change(alpha) is False
    state.mark_clean()
    assert controller.request_selection_change(gamma) is False
    assert controller.request_new_item(pristine_draft=True) is False

    assert len(alerts) == 1
    assert state.pending_transition is original_pending
    assert state.pending_transition.selection == beta
    assert state.pending_transition.restart_new_item is False
    assert calls["select"] == []
    assert calls["restart"] == []
    assert calls["restore"] == [True, True, True, True]

    controller._on_selection_response(alerts[0], "discard")
    assert calls["select"] == [beta]


def test_close_request_supersedes_open_selection_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = EditorState()
    alpha = EditorSelection.saved_item("Alpha")
    beta = EditorSelection.saved_item("Beta")
    state.activate(alpha)
    state.mark_dirty()
    controller, calls, alerts = _unsaved_controller(
        monkeypatch,
        state=state,
        save_current=lambda: True,
    )

    assert controller.request_selection_change(beta) is False
    selection_alert = alerts[0]
    assert state.pending_transition is not None

    controller.request_close()

    assert calls["dismissed"] == [selection_alert]
    assert state.pending_transition is None
    assert controller.close_prompt_open is True
    assert len(alerts) == 2

    controller._on_selection_response(selection_alert, "discard")
    assert calls["select"] == []

    close_alert = alerts[1]
    controller._on_close_response(close_alert, "discard")
    assert calls["close"] == [True]
    assert state.pending_transition is None


def test_selection_request_is_rejected_while_close_prompt_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = EditorState()
    alpha = EditorSelection.saved_item("Alpha")
    beta = EditorSelection.saved_item("Beta")
    state.activate(alpha)
    state.mark_dirty()
    controller, calls, alerts = _unsaved_controller(
        monkeypatch,
        state=state,
        save_current=lambda: True,
    )

    controller.request_close()
    assert controller.close_prompt_open is True

    assert controller.request_selection_change(beta) is False

    assert len(alerts) == 1
    assert calls["restore"] == [True]
    assert calls["select"] == []
    assert state.pending_transition is None

    controller._on_close_response(alerts[0], "discard")
    assert calls["close"] == [True]


def test_unsaved_controller_restores_after_failed_save_of_new_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = EditorState()
    state.activate(EditorSelection.new_item())
    state.mark_dirty()
    controller, calls, alerts = _unsaved_controller(
        monkeypatch,
        state=state,
        save_current=lambda: False,
    )

    assert controller.request_new_item(pristine_draft=False) is False
    assert alerts[0].get_body() == "Restart body"
    alerts[0].emit("response", "save")

    assert state.pending_transition is None
    assert calls["restart"] == []
    assert calls["restore"] == [True, True]


def test_managed_editor_shell_owns_typed_row_metadata() -> None:
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")

    from keymasq.gui.widgets.managed_editor.shell import (
        ManagedEditorCallbacks,
        ManagedEditorLabels,
        ManagedEditorShell,
    )

    state = EditorState()
    selections: list[EditorSelection | None] = []
    shell = ManagedEditorShell(
        state=state,
        labels=ManagedEditorLabels(
            sidebar_title="Things",
            search_placeholder="Search Things",
            search_tooltip="Filter Things",
            documentation_tooltip="Open documentation",
            add_tooltip="Add a Thing",
        ),
        callbacks=ManagedEditorCallbacks(
            selection_changed=selections.append,
            open_documentation=lambda: None,
            add_item=lambda: None,
            delete_item=lambda: None,
            save_item=lambda: None,
            revert_item=lambda: None,
            close_editor=lambda: None,
        ),
    )
    alpha = EditorSelection.saved_item("Alpha")
    new_item = EditorSelection.new_item()
    heading = shell.append_heading_row("Pointers")
    alpha_row = shell.append_text_row(alpha, label="Alpha", search_text="Alpha pointer")
    new_row = shell.append_text_row(
        new_item,
        label="+ Add",
        search_text="add new thing",
        tooltip="Add a Thing",
    )

    assert heading.get_selectable() is False
    assert shell.selection_for_row(heading) is None
    assert shell.selection_for_row(alpha_row) == alpha
    assert shell.row_for_selection(new_item) is new_row
    assert shell.search_text_for_row(alpha_row) == "Alpha pointer"
    assert new_row.has_css_class("managed-editor-add-row") is True
    assert not hasattr(alpha_row, "_search_text")

    assert shell.select(alpha) is True
    assert selections == [alpha]
    state.activate(alpha)
    assert shell.restore_active_selection() is True
    assert selections == [alpha]
