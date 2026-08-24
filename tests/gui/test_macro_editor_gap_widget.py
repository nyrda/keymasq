# ruff: noqa: E402, I001

import pytest

gi = pytest.importorskip("gi")

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import cairo
import evdev

from keymasq.gui.widgets.macro_editor.model import EditableControl, EditableEvent
from tests.gui.macro_editor_dialog_support import _build_macro_dialog


def _key(code: int, start_us: int, end_us: int) -> EditableEvent:
    return EditableEvent(
        device_type="keyboard",
        ev_type=evdev.ecodes.EV_KEY,
        code=code,
        press_t_us=start_us,
        release_t_us=end_us,
    )


def test_locked_timeline_hides_gaps_but_double_click_edits_next_action(
    monkeypatch,
) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    first = _key(evdev.ecodes.KEY_A, 50_000, 100_000)
    second = _key(evdev.ecodes.KEY_B, 300_000, 350_000)
    third = _key(evdev.ecodes.KEY_C, 600_000, 650_000)
    dialog._events = [first, second, third]
    dialog._duration_us = 650_000
    dialog._update_stats()
    timeline = dialog._timeline

    x = timeline._time_to_x(200_000)
    y = timeline._kb_y + 12
    timeline._on_pointer_motion(None, x, y)

    assert timeline._gap_segments == []
    assert timeline._hover_gap is None
    state = timeline._build_render_state()
    assert state._hover_gap is None

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 800, 420)
    context = cairo.Context(surface)
    timeline._draw(None, context, 800, 420, None)

    timeline._on_drag_begin(None, x, y)
    timeline._on_drag_end(None, 0.0, 0.0)

    assert timeline._selected_gap is None
    assert timeline._gap_spin is None

    timeline._on_gap_click_pressed(None, 2, x, y)

    assert timeline._selected_gap is not None
    assert timeline._selected is None
    assert dialog._revealer.get_reveal_child() is False
    assert timeline._gap_spin is not None
    assert timeline._gap_spin.get_value() == pytest.approx(200.0)
    assert timeline._gap_apply_button is not None

    timeline._gap_spin.set_value(50.0)
    timeline._gap_apply_button.emit("clicked")

    assert (second.press_t_us, second.release_t_us) == (150_000, 200_000)
    assert (third.press_t_us, third.release_t_us) == (600_000, 650_000)
    assert dialog._duration_us == 650_000
    assert timeline._selected_gap is None


def test_timeline_gap_editor_can_move_next_and_later_actions(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    first = _key(evdev.ecodes.KEY_A, 0, 100_000)
    second = _key(evdev.ecodes.KEY_B, 300_000, 400_000)
    third = _key(evdev.ecodes.KEY_C, 1_000_000, 1_100_000)
    dialog._events = [first, second, third]
    dialog._duration_us = 1_100_000
    dialog._update_stats()
    timeline = dialog._timeline
    dialog._lock_btn.set_active(False)
    gap = timeline._track_gaps["keyboard"][0]

    timeline._select_gap(gap, timeline._time_to_x(200_000), timeline._kb_y + 12)

    assert timeline._gap_spin is not None
    assert timeline._gap_next_only_check is not None
    assert timeline._gap_next_only_check.get_active() is True
    assert timeline._gap_track_following_check is not None
    assert timeline._gap_timeline_following_check is not None
    assert timeline._gap_apply_button is not None
    timeline._gap_spin.set_value(100.0)
    timeline._gap_timeline_following_check.set_active(True)
    timeline._gap_apply_button.emit("clicked")

    assert (second.press_t_us, second.release_t_us) == (200_000, 300_000)
    assert (third.press_t_us, third.release_t_us) == (900_000, 1_000_000)
    assert [gap.duration_us for gap in timeline._gap_segments] == [100_000, 600_000]


def test_gap_edit_preserves_explicit_trailing_duration(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    first = _key(evdev.ecodes.KEY_A, 0, 100_000)
    second = _key(evdev.ecodes.KEY_B, 300_000, 400_000)
    dialog._events = [first, second]
    dialog._duration_us = 1_000_000
    dialog._update_stats()
    dialog._lock_btn.set_active(False)
    gap = dialog._timeline._track_gaps["keyboard"][0]

    dialog._edit_timeline_gap(gap, 100_000, move_scope="next")

    assert dialog._duration_us == 1_000_000


def test_timeline_gap_edit_moves_explicit_trailing_duration(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    first = _key(evdev.ecodes.KEY_A, 0, 100_000)
    second = _key(evdev.ecodes.KEY_B, 300_000, 400_000)
    dialog._events = [first, second]
    dialog._duration_us = 1_000_000
    dialog._update_stats()
    dialog._lock_btn.set_active(False)
    gap = dialog._timeline._track_gaps["keyboard"][0]

    dialog._edit_timeline_gap(gap, 100_000, move_scope="timeline")

    assert dialog._duration_us == 900_000


def test_timeline_gap_edit_does_not_add_silence_past_unchanged_hold(
    monkeypatch,
) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    long_hold = _key(evdev.ecodes.KEY_LEFTCTRL, 0, 10_000_000)
    nested_tap = _key(evdev.ecodes.KEY_A, 1_000_000, 1_100_000)
    dialog._events = [long_hold, nested_tap]
    dialog._duration_us = long_hold.release_t_us
    dialog._update_stats()
    dialog._lock_btn.set_active(False)
    gap = dialog._timeline._gap_segments[0]

    dialog._edit_timeline_gap(
        gap,
        gap.duration_us + 1_000_000,
        move_scope="timeline",
    )

    assert (long_hold.press_t_us, long_hold.release_t_us) == (0, 10_000_000)
    assert (nested_tap.press_t_us, nested_tap.release_t_us) == (
        2_000_000,
        2_100_000,
    )
    assert dialog._duration_us == 10_000_000


def test_gap_editor_accepts_gaps_longer_than_one_hour(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    first = _key(evdev.ecodes.KEY_A, 0, 100_000)
    second = _key(evdev.ecodes.KEY_B, 3_700_100_000, 3_700_200_000)
    dialog._events = [first, second]
    dialog._duration_us = second.release_t_us
    dialog._update_stats()
    dialog._lock_btn.set_active(False)
    timeline = dialog._timeline
    gap = timeline._track_gaps["keyboard"][0]

    timeline._select_gap(gap, 100.0, timeline._kb_y + 12)

    assert timeline._gap_spin is not None
    assert timeline._gap_spin.get_value() == pytest.approx(3_700_000.0)
    assert timeline._gap_spin.get_adjustment().get_upper() > 3_700_000.0


def test_hover_uses_next_track_action_instead_of_rendered_sublane(
    monkeypatch,
) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    long_hold = _key(evdev.ecodes.KEY_LEFTCTRL, 0, 1_000_000)
    key_a = _key(evdev.ecodes.KEY_A, 100_000, 250_000)
    parallel_key = _key(evdev.ecodes.KEY_S, 150_000, 180_000)
    key_d = _key(evdev.ecodes.KEY_D, 300_000, 350_000)
    key_w = _key(evdev.ecodes.KEY_W, 500_000, 550_000)
    dialog._events = [long_hold, key_a, parallel_key, key_d, key_w]
    dialog._duration_us = 1_000_000
    dialog._update_stats()
    timeline = dialog._timeline
    dialog._lock_btn.set_active(False)

    timeline._recompute_lanes()
    lane_h = timeline._kb_track_h / timeline._kb_num_lanes
    x = timeline._time_to_x(240_000)
    empty_sublane_y = timeline._kb_y + lane_h * 2.5
    timeline._on_pointer_motion(None, x, empty_sublane_y)

    assert timeline._hover_gap is not None
    assert timeline._hover_gap.scope == "track"
    assert timeline._hover_gap.track == "keyboard"
    assert timeline._hover_gap.previous_items == (parallel_key,)
    assert timeline._hover_gap.next_items == (key_d,)
    assert timeline._hover_gap.duration_us == 120_000

    timeline._on_drag_begin(None, x, empty_sublane_y)
    timeline._on_drag_end(None, 0.0, 0.0)
    assert timeline._gap_spin is not None
    assert timeline._gap_track_following_check is not None
    assert timeline._gap_apply_button is not None
    timeline._gap_spin.set_value(50.0)
    timeline._gap_track_following_check.set_active(True)
    timeline._gap_apply_button.emit("clicked")

    assert (key_d.press_t_us, key_d.release_t_us) == (230_000, 280_000)
    assert (key_w.press_t_us, key_w.release_t_us) == (430_000, 480_000)
    assert (long_hold.press_t_us, long_hold.release_t_us) == (0, 1_000_000)
    assert (key_a.press_t_us, key_a.release_t_us) == (100_000, 250_000)
    assert (parallel_key.press_t_us, parallel_key.release_t_us) == (150_000, 180_000)


def test_wait_controls_have_local_gaps_in_the_control_row(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    first_wait = EditableControl(mode="wait", t_us=100_000, duration_us=50_000)
    second_wait = EditableControl(mode="wait", t_us=300_000, duration_us=50_000)
    dialog._control_events = [first_wait, second_wait]
    dialog._duration_us = 350_000
    dialog._update_stats()
    timeline = dialog._timeline
    dialog._lock_btn.set_active(False)

    timeline._on_pointer_motion(
        None,
        timeline._time_to_x(200_000),
        timeline._wave_y + timeline.TRACK_HEIGHT * 0.75,
    )

    assert timeline._hover_gap is not None
    assert timeline._hover_gap.scope == "track"
    assert timeline._hover_gap.track == "control"
    assert timeline._hover_gap.previous_items == (first_wait,)
    assert timeline._hover_gap.next_items == (second_wait,)
    assert timeline._hover_gap.duration_us == 200_000


def test_ruler_gaps_ignore_raw_movement_and_passthrough_events(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    first = _key(evdev.ecodes.KEY_A, 0, 100_000)
    second = _key(evdev.ecodes.KEY_B, 500_000, 600_000)
    dialog._events = [first, second]
    dialog._rel_events = [
        {
            "device_type": "mouse",
            "type": evdev.ecodes.EV_REL,
            "code": evdev.ecodes.REL_X,
            "value": 4,
            "t_us": 250_000,
        }
    ]
    dialog._passthrough_events = [
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_KEY,
            "code": evdev.ecodes.KEY_A,
            "value": 2,
            "t_us": 50_000,
        },
        {
            "device_type": "other",
            "type": evdev.ecodes.EV_MSC,
            "code": evdev.ecodes.MSC_SCAN,
            "value": 1,
            "t_us": 350_000,
        },
    ]
    dialog._duration_us = 600_000
    dialog._update_stats()
    timeline = dialog._timeline
    dialog._lock_btn.set_active(False)

    timeline._on_pointer_motion(None, timeline._time_to_x(300_000), 10.0)

    assert timeline._hover_gap is not None
    assert timeline._hover_gap.scope == "timeline"
    assert timeline._hover_gap.previous_items == (first,)
    assert timeline._hover_gap.next_items == (second,)
    assert timeline._hover_gap.duration_us == 400_000


def test_unlocked_move_mode_shows_gaps_and_cancel_clears_the_display(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    first = _key(evdev.ecodes.KEY_A, 0, 100_000)
    second = _key(evdev.ecodes.KEY_B, 300_000, 400_000)
    dialog._events = [first, second]
    dialog._duration_us = 400_000
    dialog._update_stats()
    timeline = dialog._timeline
    dialog._lock_btn.set_active(False)

    x = timeline._time_to_x(200_000)
    y = timeline._kb_y + 12
    timeline._on_pointer_motion(None, x, y)

    assert timeline._hover_gap is not None
    assert timeline._hover_gap.duration_us == 200_000

    timeline._on_drag_begin(None, x, y)
    timeline._on_drag_end(None, 0.0, 0.0)

    assert timeline._selected_gap is not None
    assert timeline._gap_cancel_button is not None
    timeline._gap_cancel_button.emit("clicked")

    assert timeline._selected_gap is None
    assert timeline._hover_gap is None
    timeline._on_pointer_motion(None, x, y)
    assert timeline._hover_gap is None

    dialog._lock_btn.set_active(True)
    assert timeline._gap_segments == []
    assert timeline._selected_gap is None


def test_closing_gap_popover_clears_selection_and_widget_references(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    dialog._events = [
        _key(evdev.ecodes.KEY_A, 0, 100_000),
        _key(evdev.ecodes.KEY_B, 300_000, 400_000),
    ]
    dialog._duration_us = 400_000
    dialog._update_stats()
    timeline = dialog._timeline

    timeline._on_gap_click_pressed(
        None,
        2,
        timeline._time_to_x(200_000),
        timeline._kb_y + 12,
    )

    popover = timeline._gap_popover
    assert popover is not None
    assert timeline._selected_gap is not None
    popover.emit("closed")

    assert timeline._gap_popover is None
    assert timeline._gap_spin is None
    assert timeline._gap_cancel_button is None
    assert timeline._gap_apply_button is None
    assert timeline._selected_gap is None
    assert timeline._hover_gap is None


def test_timeline_overlap_is_selectable_from_ruler(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    first = _key(evdev.ecodes.KEY_A, 0, 300_000)
    second = _key(evdev.ecodes.KEY_B, 100_000, 180_000)
    dialog._events = [first, second]
    dialog._duration_us = 300_000
    dialog._update_stats()
    timeline = dialog._timeline
    dialog._lock_btn.set_active(False)

    overlap = timeline._gap_at_position(timeline._time_to_x(200_000), 10.0)

    assert overlap is not None
    assert overlap.duration_us == -200_000
    assert (
        timeline._gap_at_position(
            timeline._time_to_x(200_000),
            timeline._kb_y + 12,
        )
        is None
    )
