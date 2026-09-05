# ruff: noqa: E402, I001
"""Exercise bulk editing through the GTK timeline and its commands."""

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import cairo
import evdev
from gi.repository import Gdk

from keymasq.gui.widgets.macro_editor.model import EditableEvent, reconstruct_events
from tests.gui.macro_editor_dialog_support import _build_macro_dialog


def _key(start: int, end: int, code: int = evdev.ecodes.KEY_A) -> EditableEvent:
    return EditableEvent("keyboard", evdev.ecodes.EV_KEY, code, start, end)


def _loaded_dialog(monkeypatch):
    dialog = _build_macro_dialog(monkeypatch)
    events = [
        _key(100_000, 150_000),
        _key(300_000, 350_000, evdev.ecodes.KEY_B),
        _key(500_000, 550_000, evdev.ecodes.KEY_C),
    ]
    dialog._apply_macro_state(
        {
            "name": "demo_macro",
            "events": reconstruct_events(events, [], [], [], []),
            "duration_us": 700_000,
            "revision": 7,
        }
    )
    dialog._sync_macro_settings_controls()
    dialog._initial_state_loaded = True
    dialog._initial_macro_data = dialog._current_macro_payload()
    dialog._set_editor_busy(False)
    dialog._auto_zoom_enabled = False
    dialog._timeline._pps = 1000
    dialog._update_stats()
    dialog._sync_close_guard()
    return dialog


class _Gesture:
    def __init__(self, modifiers: int):
        self.modifiers = modifiers

    def get_current_event_state(self) -> int:
        return self.modifiers


def _click(timeline, item, modifiers=0):
    x = timeline._time_to_x(item.press_t_us + 10_000)
    y = timeline._kb_y + 12
    gesture = _Gesture(modifiers)
    timeline._on_drag_begin(gesture, x, y)
    timeline._on_drag_end(gesture, 0, 0)


def test_ctrl_shift_and_select_all_work_while_move_locked(monkeypatch) -> None:
    dialog = _loaded_dialog(monkeypatch)
    timeline = dialog._timeline
    first, second, third = dialog._events
    _click(timeline, first)
    _click(timeline, third, Gdk.ModifierType.CONTROL_MASK)
    assert timeline.selected_items() == [first, third]
    assert timeline._selected is None
    assert not dialog._revealer.get_reveal_child()
    _click(timeline, first, Gdk.ModifierType.CONTROL_MASK)
    assert timeline.selected_items() == [third]
    _click(timeline, first)
    _click(timeline, third, Gdk.ModifierType.SHIFT_MASK)
    assert timeline.selected_items() == [first, second, third]
    assert len(timeline._build_render_state()._selected_ids) == 3
    timeline._on_selection_key(None, Gdk.KEY_Escape, 0, 0)
    assert timeline.selected_items() == []
    timeline._on_selection_key(None, Gdk.KEY_a, 0, Gdk.ModifierType.CONTROL_MASK)
    assert timeline.selected_items() == [first, second, third]
    assert not dialog._edit_history.past


def test_marquee_selects_whole_pair_and_recorded_movement_across_tracks(monkeypatch) -> None:
    dialog = _loaded_dialog(monkeypatch)
    timeline = dialog._timeline
    raw = {"device_type": "mouse", "type": 2, "code": 0, "value": 8, "t_us": 130_000}
    dialog._rel_events = [raw]
    timeline._recompute_lanes()
    x0 = timeline._time_to_x(90_000)
    x1 = timeline._time_to_x(140_000)
    y0, y1 = timeline._kb_y + 1, timeline._wave_y + 40
    timeline._on_drag_begin(None, x0, y0)
    timeline._on_drag_update(None, x1 - x0, y1 - y0)
    assert timeline.selected_items() == [dialog._events[0], raw]
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 800, 450)
    timeline._draw(None, cairo.Context(surface), 800, 450, None)
    timeline._on_drag_end(None, x1 - x0, y1 - y0)
    assert timeline._selection_box is None
    assert timeline.selected_items() == [dialog._events[0], raw]


@pytest.mark.parametrize("reverse", [False, True])
def test_ruler_drag_copies_padding_and_paste_is_one_undo_step(monkeypatch, reverse) -> None:
    source = _loaded_dialog(monkeypatch)
    timeline = source._timeline
    timeline._scroll_offset = 25
    first, last = (250_000, 450_000)
    if reverse:
        first, last = last, first
    x0, x1 = timeline._time_to_x(first), timeline._time_to_x(last)
    timeline._on_drag_begin(None, x0, 10)
    timeline._on_drag_update(None, x1 - x0, 0)
    timeline._on_drag_end(None, x1 - x0, 0)
    assert timeline._time_selection == (250_000, 450_000)
    assert timeline.selected_items() == [source._events[1]]
    assert not source._has_pending_changes()
    assert not source._edit_history.past
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 800, 450)
    timeline._draw(None, cairo.Context(surface), 800, 450, None)
    source._copy_selection()
    target = _loaded_dialog(monkeypatch)
    target._paste_selection(at_us=800_000)
    assert target._timeline.selected_items()[0].press_t_us == 850_000
    assert target._timeline._time_selection == (800_000, 1_000_000)
    assert target._timeline._insertion_us == 1_000_000
    assert target._duration_us == 1_000_000
    target._copy_selection()
    fragment = target._available_fragment()
    assert fragment.duration_us == 200_000
    assert [event["t_us"] for event in fragment.events] == [50_000, 100_000]
    target._restore_history()
    assert len(target._events) == 3
    assert target._duration_us == 700_000
    assert target._timeline._time_selection is None
    target._restore_history(redo=True)
    assert len(target._events) == 4
    assert target._duration_us == 1_000_000


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("move_enabled", [False, True])
@pytest.mark.parametrize(
    "tracks", [("ruler", "ruler"), ("mouse", "keyboard"), ("keyboard", "movement")]
)
def test_shift_click_selects_time_from_insertion_cursor(
    monkeypatch, reverse, move_enabled, tracks
) -> None:
    dialog = _loaded_dialog(monkeypatch)
    _present_editor(dialog)
    timeline = dialog._timeline
    dialog._move_btn.set_active(move_enabled)
    first, last = (250_000, 450_000)
    if reverse:
        first, last = last, first
    positions = {
        "ruler": 10,
        "keyboard": timeline._kb_y + 12,
        "mouse": timeline._m_y + 12,
        "movement": timeline._wave_y + 12,
    }

    def click_time(stamp, modifiers=0):
        gesture = _Gesture(modifiers)
        track = tracks[1] if modifiers else tracks[0]
        timeline._on_drag_begin(gesture, timeline._time_to_x(stamp), positions[track])
        timeline._on_drag_end(gesture, 0, 0)

    click_time(first)
    timeline._scroll_offset = 25
    click_time(last, Gdk.ModifierType.SHIFT_MASK)
    assert timeline._time_selection == (250_000, 450_000)
    assert timeline.selected_items() == [dialog._events[1]]
    assert timeline._gap_popover is None
    fragment = dialog._capture_selection()
    assert fragment.duration_us == 200_000
    assert [event["t_us"] for event in fragment.events] == [50_000, 100_000]

    # Cross the original anchor into pure silence, then extend back through a hold.
    silent_end = first + (25_000 if reverse else -25_000)
    click_time(silent_end, Gdk.ModifierType.SHIFT_MASK)
    assert timeline._time_selection == tuple(sorted((first, silent_end)))
    assert timeline.selected_items() == []
    assert dialog._capture_selection().duration_us == 25_000
    click_time(325_000, Gdk.ModifierType.SHIFT_MASK)
    assert timeline._time_selection == ((300_000, 450_000) if reverse else (250_000, 350_000))
    assert timeline.selected_items() == [dialog._events[1]]
    assert timeline._insertion_us == first
    # A new insertion position replaces the anchor used by all Shift+clicks.
    click_time(200_000)
    click_time(325_000, Gdk.ModifierType.SHIFT_MASK)
    assert timeline._time_selection == (200_000, 350_000)
    assert timeline._insertion_us == 200_000
    assert not dialog._has_pending_changes()
    assert not dialog._edit_history.past


def test_track_shift_drag_extends_from_ruler_insertion_cursor(monkeypatch) -> None:
    dialog = _loaded_dialog(monkeypatch)
    timeline = dialog._timeline
    timeline._on_drag_begin(None, timeline._time_to_x(250_000), 10)
    timeline._on_drag_update(None, 200, 0)
    timeline._on_drag_end(None, 200, 0)
    gesture = _Gesture(Gdk.ModifierType.SHIFT_MASK)
    timeline._on_drag_begin(gesture, timeline._time_to_x(400_000), timeline._m_y + 12)
    timeline._on_drag_update(gesture, 200, 0)
    timeline._on_drag_end(gesture, 200, 0)
    assert timeline._time_selection == (250_000, 600_000)
    assert timeline.selected_items() == dialog._events[1:]


def test_action_selection_discards_ruler_padding(monkeypatch) -> None:
    dialog = _loaded_dialog(monkeypatch)
    timeline = dialog._timeline
    timeline.set_time_selection(250_000, 450_000)
    _click(timeline, dialog._events[1])
    assert timeline._time_selection is None
    assert dialog._capture_selection().duration_us == 50_000
    timeline.set_time_selection(250_000, 450_000)
    _click(timeline, dialog._events[0], Gdk.ModifierType.CONTROL_MASK)
    assert timeline._time_selection is None
    assert dialog._capture_selection().duration_us == 250_000
    timeline.set_time_selection(250_000, 450_000)
    timeline._on_selection_key(None, Gdk.KEY_Escape, 0, 0)
    assert timeline._time_selection is None
    assert dialog._capture_selection() is None
    assert not timeline._on_selection_key(None, Gdk.KEY_d, 0, Gdk.ModifierType.CONTROL_MASK)


def test_silent_range_insert_moves_later_actions_and_can_be_undone(monkeypatch) -> None:
    dialog = _loaded_dialog(monkeypatch)
    dialog._timeline.set_time_selection(200_000, 250_000)
    assert dialog._timeline.selected_items() == []
    dialog._copy_selection()
    dialog._paste_selection(at_us=200_000, insert=True)
    assert [event.press_t_us for event in dialog._events] == [100_000, 350_000, 550_000]
    assert dialog._duration_us == 750_000
    assert dialog._timeline._time_selection == (200_000, 250_000)
    dialog._restore_history()
    assert [event.press_t_us for event in dialog._events] == [100_000, 300_000, 500_000]
    assert dialog._duration_us == 700_000


def test_range_group_drag_keeps_padding_and_clamps_at_zero(monkeypatch) -> None:
    dialog = _loaded_dialog(monkeypatch)
    timeline = dialog._timeline
    timeline.set_time_selection(250_000, 450_000)
    dialog._move_btn.set_active(True)
    timeline._on_drag_begin(None, timeline._time_to_x(310_000), timeline._kb_y + 12)
    timeline._on_drag_update(None, -500, 0)
    timeline._on_drag_end(None, -500, 0)
    assert timeline._time_selection == (0, 200_000)
    assert timeline.selected_items()[0].press_t_us == 50_000
    assert dialog._capture_selection().duration_us == 200_000
    dialog._restore_history()
    assert [event.press_t_us for event in dialog._events] == [100_000, 300_000, 500_000]


def test_group_drag_is_one_undo_step_and_redo_preserves_revision(monkeypatch) -> None:
    dialog = _loaded_dialog(monkeypatch)
    timeline = dialog._timeline
    first, second, third = dialog._events
    _click(timeline, first)
    _click(timeline, second, Gdk.ModifierType.CONTROL_MASK)
    dialog._move_btn.set_active(True)
    timeline._on_drag_begin(None, timeline._time_to_x(110_000), timeline._kb_y + 12)
    timeline._on_drag_update(None, 20, 0)
    timeline._on_drag_update(None, 70, 0)
    assert not dialog._edit_history.past
    timeline._on_drag_end(None, 70, 0)
    assert (first.press_t_us, second.press_t_us, third.press_t_us) == (170_000, 370_000, 500_000)
    assert len(dialog._edit_history.past) == 1
    dialog._undo_button.emit("clicked")
    assert [event.press_t_us for event in dialog._events] == [100_000, 300_000, 500_000]
    assert dialog._macro_data["revision"] == 7
    assert not dialog._has_pending_changes()
    dialog._redo_button.emit("clicked")
    assert [event.press_t_us for event in dialog._events] == [170_000, 370_000, 500_000]
    assert dialog._has_pending_changes()


def test_copy_across_dialogs_and_paste_stays_selected(monkeypatch) -> None:
    source = _loaded_dialog(monkeypatch)
    source._timeline.set_selection(source._events[:2])
    source._copy_selection()
    assert source._available_fragment() is not None
    target = _loaded_dialog(monkeypatch)
    target._insertion_spin.set_value(800)
    target._paste_selection()
    assert [event.press_t_us for event in target._events] == [
        100_000,
        300_000,
        500_000,
        800_000,
        1_000_000,
    ]
    assert [event.press_t_us for event in target._timeline.selected_items()] == [800_000, 1_000_000]
    assert target._duration_us == 1_050_000
    assert target._timeline._insertion_us == 1_050_000
    assert len(target._edit_history.past) == 1
    target._restore_history()
    assert len(target._events) == 3
    target._restore_history(redo=True)
    assert len(target._events) == 5


def test_cut_delete_and_paste_are_reversible(monkeypatch) -> None:
    dialog = _loaded_dialog(monkeypatch)
    dialog._timeline.set_selection(dialog._events[:2])
    dialog._cut_selection()
    assert [e.press_t_us for e in dialog._events] == [500_000]
    assert dialog._duration_us == 700_000
    dialog._restore_history()
    dialog._timeline.set_selection([dialog._events[0]])
    dialog._copy_selection()
    dialog._paste_selection(at_us=150_000)
    assert [e.press_t_us for e in dialog._events] == [100_000, 150_000, 300_000, 500_000]
    assert dialog._timeline.selected_items()[0].press_t_us == 150_000
    dialog._delete_selection()
    assert [e.press_t_us for e in dialog._events] == [100_000, 300_000, 500_000]
    dialog._restore_history()
    assert len(dialog._events) == 4


@pytest.mark.parametrize("keyval", [Gdk.KEY_Delete, Gdk.KEY_BackSpace])
@pytest.mark.parametrize("ruler", [False, True])
def test_delete_shortcuts_remove_selected_actions_immediately(monkeypatch, keyval, ruler) -> None:
    from gi.repository import Adw

    def unexpected_dialog(*args, **kwargs):
        pytest.fail("Deleting selected actions must not ask for confirmation")

    monkeypatch.setattr(Adw.AlertDialog, "present", unexpected_dialog)
    dialog = _loaded_dialog(monkeypatch)
    timeline = dialog._timeline
    if ruler:
        timeline.set_time_selection(250_000, 450_000)
    else:
        timeline.set_selection([dialog._events[1]])
    assert timeline._on_selection_key(None, keyval, 0, 0)
    assert [event.press_t_us for event in dialog._events] == [100_000, 500_000]
    assert dialog._duration_us == 700_000
    assert timeline.selected_items() == []
    assert timeline._time_selection is None
    assert len(dialog._edit_history.past) == 1
    dialog._restore_history()
    assert [event.press_t_us for event in dialog._events] == [100_000, 300_000, 500_000]
    assert dialog._duration_us == 700_000


@pytest.mark.parametrize("first,last", [(200_000, 250_000), (650_000, 800_000)])
def test_erase_silence_clamps_to_macro_end_and_is_one_undo_step(monkeypatch, first, last) -> None:
    dialog = _loaded_dialog(monkeypatch)
    before = dialog._current_macro_payload()
    timeline = dialog._timeline
    timeline._insertion_us = 700_000
    dialog._erase_btn.set_active(True)
    x0, x1 = timeline._time_to_x(first), timeline._time_to_x(last)
    timeline._on_drag_begin(None, x0, timeline._kb_y + 12)
    timeline._on_drag_update(None, x1 - x0, 0)
    assert timeline._erase_pending == []
    assert timeline._erase_x1 == timeline._time_to_x(min(last, 700_000))
    assert dialog._current_macro_payload() == before
    timeline._on_drag_end(None, x1 - x0, 0)
    assert len(dialog._events) == 3
    assert dialog._duration_us == 650_000
    assert timeline._insertion_us == 650_000
    assert len(dialog._edit_history.past) == 1
    dialog._restore_history()
    assert dialog._current_macro_payload() == before
    dialog._restore_history(redo=True)
    assert dialog._duration_us == 650_000


def test_single_property_edit_can_be_undone(monkeypatch) -> None:
    dialog = _loaded_dialog(monkeypatch)
    dialog._timeline.set_selection([dialog._events[0]])
    dialog._duration_spin.set_value(90)
    assert dialog._events[0].release_t_us == 190_000
    dialog._restore_history()
    assert dialog._events[0].release_t_us == 150_000


@pytest.mark.parametrize(
    "tab,value,button_label,starts",
    [
        ("move", 50, "Move", [150_000, 350_000, 500_000]),
        ("pauses", 100, "Set Pauses", [100_000, 250_000, 500_000]),
        ("scale", 50, "Scale", [100_000, 200_000, 500_000]),
    ],
)
def test_selection_timing_tabs_apply_one_operation(monkeypatch, tab, value, button_label, starts):
    from gi.repository import Gtk

    from tests.gui.support import collect_widgets

    dialog = _loaded_dialog(monkeypatch)
    dialog._timeline.set_selection(dialog._events[:2])
    popovers = []
    monkeypatch.setattr(Gtk.Popover, "popup", lambda p: popovers.append(p))
    dialog._show_selection_timing()
    popover = popovers[-1]
    assert popover.get_parent() is dialog._selection_menu_button
    stack = collect_widgets(popover.get_child(), Gtk.Stack)[0]
    stack.set_visible_child_name(tab)
    spin = collect_widgets(stack.get_visible_child(), Gtk.SpinButton)[0]
    spin.set_value(value)
    buttons = collect_widgets(popover.get_child(), Gtk.Button)
    apply = next(b for b in buttons if b.has_css_class("suggested-action"))
    assert apply.get_label() == button_label
    assert apply.get_sensitive()
    assert not dialog._has_pending_changes()
    if tab == "move":
        # Commit typed text with Enter, including text not yet parsed by the spin.
        spin.set_text(str(value))
        controllers = popover.observe_controllers()
        keys = next(
            c
            for i in range(controllers.get_n_items())
            if isinstance(c := controllers.get_item(i), Gtk.EventControllerKey)
            and c.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
        )
        assert keys.emit("key-pressed", Gdk.KEY_Return, 0, 0)
    else:
        apply.emit("clicked")
    assert [event.press_t_us for event in dialog._events] == starts
    if tab == "scale":
        assert dialog._events[0].release_t_us == 125_000
    assert len(dialog._edit_history.past) == 1
    dialog._restore_history()
    assert [event.press_t_us for event in dialog._events] == [100_000, 300_000, 500_000]


@pytest.mark.parametrize("kind", ["overlap", "touching", "single"])
def test_selection_timing_explains_unavailable_pauses_and_blocks_apply(monkeypatch, kind):
    from gi.repository import Gtk
    from tests.gui.support import collect_widgets

    dialog = _loaded_dialog(monkeypatch)
    if kind == "overlap":
        dialog._events[0].release_t_us = 600_000
    elif kind == "touching":
        dialog._events[0].release_t_us = 300_000
        dialog._events[1].release_t_us = 500_000
    else:
        dialog._events = dialog._events[:1]
    dialog._timeline.set_selection(dialog._events)
    before = dialog._current_macro_payload()
    history = len(dialog._edit_history.past)
    popovers = []
    closed = []
    monkeypatch.setattr(Gtk.Popover, "popup", lambda p: popovers.append(p))
    monkeypatch.setattr(Gtk.Popover, "popdown", lambda p: closed.append(p))
    dialog._show_selection_timing()
    popover = popovers[-1]
    stack = collect_widgets(popover.get_child(), Gtk.Stack)[0]
    stack.set_visible_child_name("pauses")
    assert any(
        "There are no pauses to adjust" in label.get_label()
        for label in collect_widgets(stack.get_visible_child(), Gtk.Label)
    )
    spin = collect_widgets(stack.get_visible_child(), Gtk.SpinButton)[0]
    apply = next(
        b
        for b in collect_widgets(popover.get_child(), Gtk.Button)
        if b.has_css_class("suggested-action")
    )
    assert not spin.get_sensitive()
    assert not apply.get_sensitive()
    apply.emit("clicked")
    controllers = popover.observe_controllers()
    keys = next(
        c
        for i in range(controllers.get_n_items())
        if isinstance(c := controllers.get_item(i), Gtk.EventControllerKey)
    )
    assert keys.emit("key-pressed", Gdk.KEY_Return, 0, 0)
    assert not closed
    assert dialog._current_macro_payload() == before
    assert len(dialog._edit_history.past) == history
    for tab in ("move", "scale"):
        stack.set_visible_child_name(tab)
        assert apply.get_sensitive()
    stack.set_visible_child_name("pauses")
    assert not apply.get_sensitive()


def test_context_selection_timing_keeps_original_click_and_escape_cancels(monkeypatch):
    from gi.repository import Gtk

    from tests.gui.support import collect_widgets

    dialog = _loaded_dialog(monkeypatch)
    timeline = dialog._timeline
    timeline.set_selection(dialog._events[:2])
    popovers = []
    monkeypatch.setattr(Gtk.Popover, "popup", lambda p: popovers.append(p))
    x, y = timeline._time_to_x(200_000), timeline._m_y + 12
    timeline._on_right_click(None, 1, x, y)
    command = next(
        b
        for b in collect_widgets(popovers[-1].get_child(), Gtk.Button)
        if b.get_label() == "Selection Timing…"
    )
    command.emit("clicked")
    timing = popovers[-1]
    assert timing.get_parent() is timeline
    has_rect, rect = timing.get_pointing_to()
    assert has_rect
    assert (rect.x, rect.y) == (round(x), round(y))
    stack = collect_widgets(timing.get_child(), Gtk.Stack)[0]
    collect_widgets(stack.get_visible_child(), Gtk.SpinButton)[0].set_value(500)
    controllers = timing.observe_controllers()
    keys = next(
        c
        for i in range(controllers.get_n_items())
        if isinstance(c := controllers.get_item(i), Gtk.EventControllerKey)
        and c.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
    )
    assert keys.emit("key-pressed", Gdk.KEY_Escape, 0, 0)
    assert not dialog._has_pending_changes()
    assert not dialog._edit_history.past


def test_revert_is_one_undo_step_for_events_and_multiple_settings(monkeypatch) -> None:
    dialog = _loaded_dialog(monkeypatch)
    dialog._timeline.set_selection([dialog._events[0]])
    dialog._duration_spin.set_value(90)
    dialog._macro_loop_count_spin.set_value(5)
    dialog._macro_block_mouse_check.set_active(True)
    before = dialog._current_macro_payload()
    count = len(dialog._edit_history.past)
    dialog._on_undo_all_changes(None)
    assert len(dialog._edit_history.past) == count + 1
    assert not dialog._has_pending_changes()
    dialog._restore_history()
    assert dialog._current_macro_payload() == before


def test_apply_does_not_add_an_invisible_undo_step(monkeypatch) -> None:
    dialog = _loaded_dialog(monkeypatch)
    dialog._timeline.set_selection([dialog._events[0]])
    dialog._copy_selection()
    dialog._paste_selection(at_us=150_000)
    count = len(dialog._edit_history.past)
    saved = {
        **dialog._current_macro_payload(),
        "revision": 8,
        "event_count": 8,
        "created_at": "2026-09-05T12:00:00",
    }
    dialog._apply_saved_macro_state({"macro": saved}, "demo_macro", saved)
    assert len(dialog._edit_history.past) == count
    assert not dialog._has_pending_changes()
    dialog._restore_history()
    assert len(dialog._events) == 3
    assert dialog._macro_data["revision"] == 8
    assert dialog._has_pending_changes()
    dialog._restore_history(redo=True)
    assert not dialog._has_pending_changes()


def test_failed_clipboard_write_does_not_cut_actions(monkeypatch) -> None:
    dialog = _loaded_dialog(monkeypatch)
    dialog._timeline.set_selection([dialog._events[0]])
    dialog._copy_selection()
    monkeypatch.setattr(dialog, "_copy_to_clipboard", lambda: False)
    dialog._cut_selection()
    assert len(dialog._events) == 3
    assert not dialog._edit_history.past


def test_context_paste_reads_native_data_after_clipboard_owner_changes(monkeypatch) -> None:
    import json
    from gi.repository import GLib, Gtk
    from keymasq.gui.session_client import GuiTaskResult
    from keymasq.gui.widgets.macro_editor.clipboard import MACRO_FRAGMENT_MIME
    from tests.gui.support import collect_widgets

    dialog = _loaded_dialog(monkeypatch)
    dialog._timeline.set_selection(dialog._events[:2])
    dialog._copy_selection()
    fragment = dialog._available_fragment()
    assert fragment is not None
    # A clipboard manager serves identical data using a different provider.
    replacement = Gdk.ContentProvider.new_for_bytes(
        MACRO_FRAGMENT_MIME, GLib.Bytes.new(json.dumps(fragment.clipboard_payload()).encode())
    )
    assert dialog._timeline.get_clipboard().set_content(replacement)
    assert dialog._available_fragment() is None
    monkeypatch.setattr(
        dialog, "_run_gui_task", lambda worker, callback: callback(GuiTaskResult(value=worker()))
    )
    popovers = []
    monkeypatch.setattr(Gtk.Popover, "popup", lambda popover: popovers.append(popover))
    timeline = dialog._timeline
    # Exercise the actual context-menu button with a scrolled timeline.
    timeline.set_scroll_offset(75)
    timeline._on_right_click(None, 1, timeline._time_to_x(900_000), timeline._m_y + 12)
    button = next(
        b
        for b in collect_widgets(popovers[-1].get_child(), Gtk.Button)
        if (b.get_label() or "").startswith("Paste at ")
    )
    assert button.get_label() == "Paste at 900.0ms"
    assert button.get_sensitive()
    # The action retains the right-click location even if the cursor changes.
    dialog._insertion_spin.set_value(1200)
    button.emit("clicked")
    assert dialog._paste_cancellable is not None
    assert not dialog._editor_content.get_sensitive()
    loop = GLib.MainLoop()

    def finished():
        if dialog._paste_cancellable is not None:
            return True
        loop.quit()
        return False

    poll = GLib.timeout_add(5, finished)
    deadline = GLib.timeout_add_seconds(3, lambda: (loop.quit(), False)[1])
    loop.run()
    if dialog._paste_cancellable is not None:
        GLib.source_remove(poll)
    else:
        GLib.source_remove(deadline)
    assert dialog._paste_cancellable is None
    assert [event.press_t_us for event in dialog._events] == [
        100_000,
        300_000,
        500_000,
        900_000,
        1_100_000,
    ]
    assert dialog._editor_content.get_sensitive()
    assert len(dialog._edit_history.past) == 1
    dialog._restore_history()
    assert len(dialog._events) == 3


def test_async_paste_does_not_modify_a_closed_editor(monkeypatch) -> None:
    from keymasq.gui.widgets.macro_editor.controller import selection as controller

    dialog = _loaded_dialog(monkeypatch)
    monkeypatch.setattr(dialog, "_available_fragment", lambda: None)
    monkeypatch.setattr(controller, "has_macro_fragment", lambda clipboard: True)
    received = []
    monkeypatch.setattr(
        controller, "read_macro_fragment", lambda c, cancel, callback: received.append(callback)
    )
    dialog._paste_selection()
    cancellable = dialog._paste_cancellable
    dialog._force_close_without_warning()
    assert cancellable.is_cancelled()
    received[0](b"{}", None)
    assert len(dialog._events) == 3
    assert not dialog._edit_history.past


def _present_editor(dialog):
    from gi.repository import GLib

    dialog._parent.present()
    dialog.present(dialog._parent)
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


def test_async_paste_restores_focus_for_keyboard_undo_and_redo(monkeypatch) -> None:
    import json
    from keymasq.gui.session_client import GuiTaskResult
    from keymasq.gui.widgets.macro_editor import selection
    from keymasq.gui.widgets.macro_editor.controller import selection as controller

    dialog = _loaded_dialog(monkeypatch)
    _present_editor(dialog)
    try:
        fragment = selection.Fragment.capture(dialog._timeline_lists(), dialog._events[:2])
        monkeypatch.setattr(dialog, "_available_fragment", lambda: None)
        monkeypatch.setattr(controller, "has_macro_fragment", lambda clipboard: True)
        received = []
        monkeypatch.setattr(
            controller, "read_macro_fragment", lambda c, cancel, callback: received.append(callback)
        )
        monkeypatch.setattr(
            dialog,
            "_run_gui_task",
            lambda worker, callback: callback(GuiTaskResult(value=worker())),
        )
        dialog._timeline.grab_focus()
        dialog._paste_selection(at_us=900_000)
        assert not dialog._editor_content.is_sensitive()
        received[0](json.dumps(fragment.clipboard_payload()).encode(), None)
        assert dialog._timeline.get_root().get_focus() is dialog._timeline
        assert len(dialog._events) == 5
        keys = dialog._editor_key_controller
        assert keys.emit("key-pressed", Gdk.KEY_z, 0, Gdk.ModifierType.CONTROL_MASK)
        assert len(dialog._events) == 3
        assert keys.emit(
            "key-pressed", Gdk.KEY_z, 0, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
        )
        assert len(dialog._events) == 5
    finally:
        dialog._force_close_without_warning()
        dialog._parent.destroy()


@pytest.mark.parametrize("load_before_present", [False, True])
def test_paste_into_new_macro_after_closing_source_without_timeline_click(
    monkeypatch, load_before_present
) -> None:
    from keymasq.gui.session_client import GuiTaskResult

    source = _loaded_dialog(monkeypatch)
    source._timeline.set_time_selection(250_000, 450_000)
    source._copy_selection()
    source._force_close_without_warning()
    source._parent.destroy()
    target = _build_macro_dialog(monkeypatch, create_new=True)
    try:
        if not load_before_present:
            _present_editor(target)
        target._on_initial_state_loaded(GuiTaskResult(value={}))
        target._exit_loading_state()
        if load_before_present:
            _present_editor(target)
        assert target._editor_key_controller.emit(
            "key-pressed", Gdk.KEY_v, 0, Gdk.ModifierType.CONTROL_MASK
        )
        assert len(target._events) == 1
        assert target._events[0].press_t_us == 50_000
        assert target._events[0].release_t_us == 100_000
        assert target._timeline._time_selection == (0, 200_000)
        assert target._timeline.get_root().get_focus() is target._timeline
        assert target._editor_key_controller.emit(
            "key-pressed", Gdk.KEY_z, 0, Gdk.ModifierType.CONTROL_MASK
        )
        assert target._events == []
    finally:
        target._force_close_without_warning()
        target._parent.destroy()


def test_editor_shortcuts_work_on_buttons_but_leave_text_fields_alone(monkeypatch) -> None:
    dialog = _loaded_dialog(monkeypatch)
    _present_editor(dialog)
    try:
        dialog._timeline.set_selection(dialog._events[:2])
        dialog._copy_selection()
        dialog._paste_selection(at_us=150_000)
        keys = dialog._editor_key_controller
        dialog._move_btn.grab_focus()
        assert keys.emit("key-pressed", Gdk.KEY_z, 0, Gdk.ModifierType.CONTROL_MASK)
        assert len(dialog._events) == 3
        assert keys.emit("key-pressed", Gdk.KEY_y, 0, Gdk.ModifierType.CONTROL_MASK)
        assert len(dialog._events) == 5
        dialog._move_btn.grab_focus()
        assert keys.emit("key-pressed", Gdk.KEY_v, 0, Gdk.ModifierType.CONTROL_MASK)
        assert len(dialog._events) == 7
        for field in (dialog._name_entry, dialog._insertion_spin):
            field.grab_focus()
            assert not keys.emit("key-pressed", Gdk.KEY_z, 0, Gdk.ModifierType.CONTROL_MASK)
            assert not keys.emit("key-pressed", Gdk.KEY_v, 0, Gdk.ModifierType.CONTROL_MASK)
            assert len(dialog._events) == 7
        dialog._move_btn.grab_focus()
        dialog._set_editor_busy(True)
        assert not keys.emit("key-pressed", Gdk.KEY_z, 0, Gdk.ModifierType.CONTROL_MASK)
        assert not keys.emit("key-pressed", Gdk.KEY_v, 0, Gdk.ModifierType.CONTROL_MASK)
        assert len(dialog._events) == 7
    finally:
        dialog._force_close_without_warning()
        dialog._parent.destroy()
