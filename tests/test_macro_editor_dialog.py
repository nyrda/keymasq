"""Macro editor dialog tests."""

# ruff: noqa: E402, I001

import gi
import sys

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import evdev  # noqa: E402

from keyforge.common.models import ActionType  # noqa: E402
from keyforge.gui.widgets.macro_editor_dialog import EditableEvent  # noqa: E402
from keyforge.gui.widgets.macro_editor_dialog import EditableMove  # noqa: E402
from keyforge.gui.widgets.macro_editor_dialog import MacroEditorDialog  # noqa: E402
from keyforge.gui.widgets.macro_editor_dialog import _passthrough_track  # noqa: E402
from keyforge.gui.widgets.macro_editor_dialog import parse_events  # noqa: E402
from keyforge.gui.widgets.macro_editor_dialog import reconstruct_events  # noqa: E402

macro_editor_dialog_module = sys.modules["keyforge.gui.widgets.macro_editor_dialog"]


class _FakeSlurpCapture:
    def __init__(self, available: bool = False) -> None:
        self.available = available
        self.compositor: str | None = None
        self.capture_callback = None

    def set_compositor(self, compositor: str) -> None:
        self.compositor = compositor

    def capture_point(self, callback) -> None:
        self.capture_callback = callback


def _build_macro_dialog(monkeypatch, *, slurp_available: bool = False) -> MacroEditorDialog:
    from gi.repository import Gtk

    fake_slurp = _FakeSlurpCapture(available=slurp_available)
    monkeypatch.setattr(macro_editor_dialog_module, "get_slurp_capture", lambda: fake_slurp)
    monkeypatch.setattr(macro_editor_dialog_module, "detect_compositor_sync", lambda: "hyprland")
    monkeypatch.setattr(
        macro_editor_dialog_module,
        "_compute_macro_editor_dialog_size",
        lambda parent: (800, 600),
    )
    monkeypatch.setattr(MacroEditorDialog, "_load_initial_state_async", lambda self: None)
    dialog = MacroEditorDialog(Gtk.Window(), "demo_macro")
    dialog._test_slurp = fake_slurp  # type: ignore[attr-defined]
    return dialog


def test_parse_reconstruct_preserves_abs_and_repeat_events() -> None:
    raw = [
        {
            "device_type": "gamepad",
            "type": evdev.ecodes.EV_ABS,
            "code": evdev.ecodes.ABS_X,
            "value": 123,
            "t_us": 10,
        },
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_KEY,
            "code": evdev.ecodes.KEY_A,
            "value": 1,
            "t_us": 20,
        },
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_KEY,
            "code": evdev.ecodes.KEY_A,
            "value": 2,
            "t_us": 25,
        },
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_KEY,
            "code": evdev.ecodes.KEY_A,
            "value": 0,
            "t_us": 30,
        },
        {
            "device_type": "mouse",
            "type": evdev.ecodes.EV_REL,
            "code": evdev.ecodes.REL_X,
            "value": 5,
            "t_us": 40,
        },
    ]

    editable, rel_events, passthrough, synthetic_moves, control_events = parse_events(raw)
    rebuilt = reconstruct_events(editable, rel_events, passthrough, synthetic_moves, control_events)

    assert any(e["type"] == evdev.ecodes.EV_ABS for e in rebuilt)
    assert any(e["type"] == evdev.ecodes.EV_REL for e in rebuilt)
    assert any(e["type"] == evdev.ecodes.EV_KEY and e["value"] == 2 for e in rebuilt)


def test_parse_handles_overlapping_same_key_presses() -> None:
    raw = [
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_KEY,
            "code": evdev.ecodes.KEY_B,
            "value": 1,
            "t_us": 100,
        },
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_KEY,
            "code": evdev.ecodes.KEY_B,
            "value": 1,
            "t_us": 120,
        },
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_KEY,
            "code": evdev.ecodes.KEY_B,
            "value": 0,
            "t_us": 140,
        },
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_KEY,
            "code": evdev.ecodes.KEY_B,
            "value": 0,
            "t_us": 160,
        },
    ]

    editable, rel_events, passthrough, synthetic_moves, control_events = parse_events(raw)

    assert len(rel_events) == 0
    assert len(passthrough) == 0
    assert len(synthetic_moves) == 0
    assert len(control_events) == 0
    assert len(editable) == 2
    assert editable[0].press_t_us == 100
    assert editable[0].release_t_us == 160
    assert editable[1].press_t_us == 120
    assert editable[1].release_t_us == 140


def test_parse_reconstruct_synthetic_moves_separate_from_waveform() -> None:
    raw = [
        {
            "device_type": "mouse",
            "type": evdev.ecodes.EV_REL,
            "code": evdev.ecodes.REL_X,
            "value": 11,
            "t_us": 100,
        },
        {
            "device_type": "mouse",
            "type": evdev.ecodes.EV_REL,
            "code": evdev.ecodes.REL_Y,
            "value": -7,
            "t_us": 100,
        },
        {
            "device_type": "mouse",
            "type": evdev.ecodes.EV_REL,
            "code": evdev.ecodes.REL_X,
            "value": 300,
            "t_us": 200,
            "synthetic_move": True,
            "move_id": "m1",
            "move_mode": "rel",
        },
        {
            "device_type": "mouse",
            "type": evdev.ecodes.EV_REL,
            "code": evdev.ecodes.REL_Y,
            "value": 200,
            "t_us": 200,
            "synthetic_move": True,
            "move_id": "m1",
            "move_mode": "rel",
        },
    ]

    editable, rel_events, passthrough, synthetic_moves, control_events = parse_events(raw)
    assert len(editable) == 0
    assert len(passthrough) == 0
    assert len(rel_events) == 2
    assert len(synthetic_moves) == 1
    assert len(control_events) == 0
    assert synthetic_moves[0].mode == "rel"
    assert synthetic_moves[0].x == 300
    assert synthetic_moves[0].y == 200

    rebuilt = reconstruct_events(editable, rel_events, passthrough, synthetic_moves, control_events)
    assert sum(1 for e in rebuilt if e.get("synthetic_move")) == 2


def test_unmatched_key_press_is_classified_for_keyboard_track() -> None:
    raw = [
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_KEY,
            "code": evdev.ecodes.KEY_V,
            "value": 1,
            "t_us": 227999,
        }
    ]

    editable, rel_events, passthrough, synthetic_moves, control_events = parse_events(raw)

    assert editable == []
    assert rel_events == []
    assert synthetic_moves == []
    assert control_events == []
    assert len(passthrough) == 1
    assert _passthrough_track(passthrough[0]) == "keyboard"


def test_macro_editor_initial_state_load_applies_macro_fields(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)

    result = {
        "timeout_max": 45000,
        "macro": {
            "name": "demo_macro",
            "revision": 4,
            "events": [
                {
                    "device_type": "keyboard",
                    "type": evdev.ecodes.EV_KEY,
                    "code": evdev.ecodes.KEY_A,
                    "value": 1,
                    "t_us": 1000,
                },
                {
                    "device_type": "keyboard",
                    "type": evdev.ecodes.EV_KEY,
                    "code": evdev.ecodes.KEY_A,
                    "value": 0,
                    "t_us": 5000,
                },
                {
                    "device_type": "mouse",
                    "type": evdev.ecodes.EV_REL,
                    "code": evdev.ecodes.REL_X,
                    "value": 5,
                    "t_us": 7000,
                },
                {
                    "macro_action": "exec_sync",
                    "t_us": 9000,
                    "command": "echo hi",
                    "timeout_ms": 1200,
                    "inhibit_mouse": True,
                },
            ],
            "gap_notes": [{"at_us": 11000, "gap_ms": 250, "scope": "keyboard"}],
            "duration_ms": 25,
            "move_to_start": True,
            "start_x": 320,
            "start_y": 240,
            "block_mouse_movement": True,
            "loop_mode": "count",
            "loop_count": 3,
        },
    }

    assert dialog._on_initial_state_loaded(result) is False

    assert dialog._macro_exists is True
    assert dialog._macro_exec_timeout_max_ms == 45000
    assert len(dialog._events) == 1
    assert len(dialog._rel_events) == 1
    assert len(dialog._control_events) == 1
    assert any(move.mode == "gap" and move.scope == "keyboard" for move in dialog._synthetic_moves)
    assert dialog._duration_us == 25000
    assert dialog._stats_label.get_label() == "0.025s · 4 events"
    assert dialog._exec_summary_label.get_label() == "Exec actions: 1 (sync 1, async 0)"
    assert dialog._control_timeout_spin.get_adjustment().get_upper() == 45000


def test_macro_editor_event_selection_and_timing_edits_refresh_event(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    event = EditableEvent(
        device_type="keyboard",
        ev_type=evdev.ecodes.EV_KEY,
        code=evdev.ecodes.KEY_A,
        press_t_us=1000,
        release_t_us=6000,
    )
    dialog._events = [event]
    dialog._duration_us = 6000
    dialog._timeline._selected = event

    dialog._on_selection_changed(event)
    dialog._press_spin.set_value(3)
    dialog._on_press_changed(dialog._press_spin)
    dialog._duration_spin.set_value(9)
    dialog._on_duration_changed(dialog._duration_spin)
    dialog._release_spin.set_value(20)
    dialog._on_release_changed(dialog._release_spin)

    assert event.press_t_us == 3000
    assert event.release_t_us == 20000
    assert dialog._duration_us == 20000
    assert dialog._prop_title.get_label() == "KEY_A"
    assert dialog._revealer.get_reveal_child() is True
    assert dialog._stats_label.get_label() == "0.020s · 2 events"


def test_macro_editor_gap_note_controls_shift_timeline(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    event = EditableEvent(
        device_type="keyboard",
        ev_type=evdev.ecodes.EV_KEY,
        code=evdev.ecodes.KEY_A,
        press_t_us=200000,
        release_t_us=202000,
    )
    gap = EditableMove(mode="gap", t_us=5000, x=100, y=0, scope="all")
    dialog._events = [event]
    dialog._synthetic_moves = [gap]
    dialog._duration_us = 12000
    dialog._timeline._selected = gap

    dialog._on_selection_changed(gap)
    dialog._move_x_spin.set_value(150)
    dialog._on_move_x_changed(dialog._move_x_spin)

    assert gap.x == 150
    assert event.press_t_us == 250000
    assert event.release_t_us == 252000

    macro_editor_dialog_module._set_dropdown_selected_id(
        dialog._gap_scope_combo,
        macro_editor_dialog_module._SCOPE_OPTIONS,
        "keyboard",
    )
    dialog._on_gap_scope_changed(dialog._gap_scope_combo)

    assert gap.scope == "keyboard"


def test_macro_editor_loop_and_capture_start_position_controls(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch, slurp_available=True)

    macro_editor_dialog_module._set_dropdown_selected_id(
        dialog._macro_loop_mode_combo,
        macro_editor_dialog_module._LOOP_MODE_OPTIONS,
        "count",
    )
    dialog._on_macro_loop_mode_changed(dialog._macro_loop_mode_combo)
    dialog._macro_loop_count_spin.set_value(4)
    dialog._on_macro_loop_count_changed(dialog._macro_loop_count_spin)
    dialog._macro_move_to_start_check.set_active(True)
    dialog._on_macro_move_to_start_toggled(dialog._macro_move_to_start_check)

    class _Result:
        def __init__(self, x: int, y: int) -> None:
            self.x = x
            self.y = y

    dialog._on_slurp_capture_result(_Result(640, 480))

    assert dialog._macro_loop_mode == "count"
    assert dialog._macro_loop_count == 4
    assert dialog._macro_loop_count_spin.get_visible() is True
    assert dialog._macro_start_x_spin.get_sensitive() is True
    assert dialog._macro_start_y_spin.get_sensitive() is True
    assert dialog._macro_start_x_spin.get_value_as_int() == 640
    assert dialog._macro_start_y_spin.get_value_as_int() == 480
    assert dialog._macro_capture_status.get_text() == "Captured: 640, 480"

    dialog._on_capture_start_position_response(
        {"status": "error", "message": "Unknown command: get_cursor_position"}
    )

    assert (
        dialog._macro_capture_status.get_text()
        == "Please restart Keyforge Session, then try again"
    )


def test_macro_editor_insert_delete_and_save_payload(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    dialog._timeline._selected = None
    dialog._rel_events = [
        {
            "device_type": "mouse",
            "type": evdev.ecodes.EV_REL,
            "code": evdev.ecodes.REL_X,
            "value": 4,
            "t_us": 15000,
        }
    ]
    dialog._macro_data = {"revision": 2}
    dialog._duration_us = 15000
    macro_editor_dialog_module._set_dropdown_selected_id(
        dialog._macro_loop_mode_combo,
        macro_editor_dialog_module._LOOP_MODE_OPTIONS,
        "toggle",
    )
    dialog._macro_loop_count_spin.set_value(2)
    dialog._macro_move_to_start_check.set_active(True)
    dialog._macro_start_x_spin.set_value(10)
    dialog._macro_start_y_spin.set_value(20)
    dialog._macro_block_mouse_check.set_active(True)

    dialog._on_key_selected_for_insert(
        None,
        type("Action", (), {"action_type": ActionType.KEYBOARD, "target": "key_b"})(),
        12000,
    )

    inserted = dialog._events[0]
    dialog._delete_event(inserted)
    assert dialog._events == []

    dialog._on_key_selected_for_insert(
        None,
        type("Action", (), {"action_type": ActionType.KEYBOARD, "target": "key_b"})(),
        12000,
    )
    gap = EditableMove(mode="gap", t_us=5000, x=80, y=0, scope="movement")
    dialog._synthetic_moves = [gap]

    payload = dialog._build_macro_payload("saved_macro")

    assert payload["name"] == "saved_macro"
    assert payload["loop_mode"] == "toggle"
    assert payload["loop_count"] == 2
    assert payload["move_to_start"] is True
    assert payload["start_x"] == 10
    assert payload["start_y"] == 20
    assert payload["block_mouse_movement"] is True
    assert payload["device_types"] == ["keyboard", "mouse"]
    assert payload["gap_notes"] == [{"at_us": 5000, "gap_ms": 80, "scope": "movement"}]


def test_macro_editor_save_request_paths_and_undo(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    dialog._macro_name = "original"
    dialog._name_entry.set_text("changed")
    dialog._macro_data = {"revision": 7}
    dialog._macro_exists = True
    dialog._initial_macro_data = {
        "name": "original",
        "events": [
            {
                "device_type": "keyboard",
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_A,
                "value": 1,
                "t_us": 1000,
            },
            {
                "device_type": "keyboard",
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.KEY_A,
                "value": 0,
                "t_us": 3000,
            },
        ],
        "duration_ms": 3,
        "loop_mode": "hold",
        "loop_count": 1,
        "move_to_start": False,
        "start_x": 0,
        "start_y": 0,
        "block_mouse_movement": False,
    }

    requests: list[dict] = []

    def fake_session_request(payload):
        requests.append(payload)
        if payload["command"] in {"create_macro", "update_macro"}:
            return {"status": "ok"}
        if payload["command"] == "delete_macro":
            return {"status": "ok"}
        return {}

    monkeypatch.setattr(macro_editor_dialog_module, "session_request", fake_session_request)

    create_result = dialog._save_macro_request("renamed", {"name": "renamed"}, 7)

    assert create_result == {"status": "ok"}
    assert requests[0]["command"] == "create_macro"
    assert requests[1]["command"] == "delete_macro"
    assert requests[1]["expected_revision"] == 7

    dialog._apply_macro_state(dialog._initial_macro_data)
    dialog._events[0].press_t_us = 9000
    macro_editor_dialog_module._set_dropdown_selected_id(
        dialog._macro_loop_mode_combo,
        macro_editor_dialog_module._LOOP_MODE_OPTIONS,
        "count",
    )
    dialog._macro_loop_count_spin.set_value(5)
    dialog._macro_move_to_start_check.set_active(True)
    dialog._macro_start_x_spin.set_value(99)

    dialog._on_undo_all_changes(None)

    assert dialog._events[0].press_t_us == 1000
    assert macro_editor_dialog_module._get_dropdown_selected_id(
        dialog._macro_loop_mode_combo,
        macro_editor_dialog_module._LOOP_MODE_OPTIONS,
        "none",
    ) == "hold"
    assert dialog._macro_loop_count_spin.get_value_as_int() == 1
    assert dialog._macro_move_to_start_check.get_active() is False
    assert dialog._macro_start_x_spin.get_value_as_int() == 0
