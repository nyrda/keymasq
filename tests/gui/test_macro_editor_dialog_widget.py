# ruff: noqa: F403, F405, I001
from tests.gui.macro_editor_dialog_support import *

def test_macro_editor_initial_state_load_applies_macro_fields(monkeypatch) -> None:
    from keyforge.gui.session_client import GuiTaskResult

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

    assert dialog._on_initial_state_loaded(GuiTaskResult(value=result)) is False

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
    assert dialog._name_entry.get_text() == "demo_macro"
    assert (
        macro_editor_dialog_module._get_dropdown_selected_id(
            dialog._macro_loop_mode_combo,
            macro_editor_dialog_module._LOOP_MODE_OPTIONS,
            "none",
        )
        == "count"
    )
    assert dialog._macro_loop_count_spin.get_value_as_int() == 3
    assert dialog._macro_loop_count_spin.get_visible() is True
    assert dialog._macro_move_to_start_check.get_active() is True
    assert dialog._macro_start_x_spin.get_value_as_int() == 320
    assert dialog._macro_start_y_spin.get_value_as_int() == 240
    assert dialog._macro_start_x_spin.get_sensitive() is True
    assert dialog._macro_start_y_spin.get_sensitive() is True
    assert dialog._macro_block_mouse_check.get_active() is True


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
