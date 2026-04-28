# ruff: noqa: F403, F405, I001
from collections.abc import Callable

from keymasq.gui.widgets.macro_editor_dialog import EditableControl

from tests.gui.macro_editor_dialog_support import *

def test_macro_editor_initial_state_load_applies_macro_fields(monkeypatch) -> None:
    from keymasq.gui.session_client import GuiTaskResult

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
            "duration_ms": 25,
            "move_to_start": True,
            "start_x": 320,
            "start_y": 240,
            "block_mouse_movement": True,
            "loop_mode": "count",
            "loop_count": 3,
            "loop_stop_behavior": "cancel_run",
        },
    }

    assert dialog._on_initial_state_loaded(GuiTaskResult(value=result)) is False

    assert dialog._macro_exists is True
    assert dialog._macro_exec_timeout_max_ms == 45000
    assert len(dialog._events) == 1
    assert len(dialog._rel_events) == 1
    assert len(dialog._control_events) == 1
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
    assert dialog._macro_loop_finish_check.get_active() is False
    assert dialog._macro_loop_finish_check.get_visible() is False
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


def test_macro_editor_insert_wait_adds_control_without_rewriting_timeline(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    event = EditableEvent(
        device_type="keyboard",
        ev_type=evdev.ecodes.EV_KEY,
        code=evdev.ecodes.KEY_A,
        press_t_us=200000,
        release_t_us=202000,
    )
    dialog._events = [event]
    dialog._duration_us = 12000

    dialog._insert_control_event(EditableControl(mode="wait", t_us=5000, duration_ms=150))

    assert len(dialog._control_events) == 1
    assert dialog._control_events[0].mode == "wait"
    assert event.press_t_us == 200000
    assert event.release_t_us == 202000


def test_macro_editor_timing_tools_set_total_time(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    assert dialog._timing_extend_ms_spin is not None

    dialog._timing_extend_ms_spin.set_value(5000)
    dialog._on_set_total_time_clicked(None)

    assert dialog._duration_us == 5_000_000
    assert dialog._build_macro_payload("empty_space")["duration_ms"] == 5000

    event = EditableEvent(
        device_type="keyboard",
        ev_type=evdev.ecodes.EV_KEY,
        code=evdev.ecodes.KEY_A,
        press_t_us=6_000_000,
        release_t_us=6_002_000,
    )
    dialog._events = [event]
    dialog._timing_extend_ms_spin.set_value(1000)
    dialog._on_set_total_time_clicked(None)

    assert dialog._duration_us == 6_002_000
    assert dialog._build_macro_payload("with_event")["duration_ms"] == 6002


def test_macro_editor_payload_includes_wait_controls(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    dialog._control_events = [
        EditableControl(mode="wait", t_us=1000, duration_ms=75),
        EditableControl(mode="wait_random", t_us=2000, min_ms=10, max_ms=80),
    ]

    payload = dialog._build_macro_payload("timed_macro")

    assert payload["events"] == [
        {
            "device_type": "macro",
            "type": 0,
            "code": 0,
            "value": 0,
            "t_us": 1000,
            "macro_action": "wait",
            "duration_ms": 75,
        },
        {
            "device_type": "macro",
            "type": 0,
            "code": 0,
            "value": 0,
            "t_us": 2000,
            "macro_action": "wait_random",
            "min_ms": 10,
            "max_ms": 80,
        },
    ]


def test_macro_editor_wait_controls_show_edit_fields(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)

    fixed = EditableControl(mode="wait", t_us=12_000, duration_ms=75)
    dialog._timeline._selected = fixed
    dialog._on_selection_changed(fixed)

    assert dialog._press_spin.get_value_as_int() == 12
    assert dialog._control_ab_row.get_visible() is True
    assert dialog._control_a_label.get_visible() is True
    assert dialog._control_a_spin.get_visible() is True
    assert dialog._control_a_label.get_label() == "Duration (ms):"
    assert dialog._control_a_spin.get_value_as_int() == 75
    assert dialog._control_b_label.get_visible() is False
    assert dialog._control_b_spin.get_visible() is False

    random_wait = EditableControl(mode="wait_random", t_us=34_000, min_ms=10, max_ms=80)
    dialog._timeline._selected = random_wait
    dialog._on_selection_changed(random_wait)

    assert dialog._press_spin.get_value_as_int() == 34
    assert dialog._control_ab_row.get_visible() is True
    assert dialog._control_a_label.get_visible() is True
    assert dialog._control_a_spin.get_visible() is True
    assert dialog._control_b_label.get_visible() is True
    assert dialog._control_b_spin.get_visible() is True
    assert dialog._control_a_label.get_label() == "Min (ms):"
    assert dialog._control_b_label.get_label() == "Max (ms):"
    assert dialog._control_a_spin.get_value_as_int() == 10
    assert dialog._control_b_spin.get_value_as_int() == 80


def test_macro_editor_abs_move_capture_updates_selected_move(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch, slurp_available=True)
    move = EditableMove(mode="abs", t_us=5000, x=10, y=20)
    dialog._synthetic_moves = [move]
    dialog._timeline._selected = move

    dialog._on_selection_changed(move)
    assert dialog._move_capture_row.get_visible() is True

    dialog._on_capture_selected_move_clicked(dialog._move_capture_btn)
    assert dialog._test_slurp.capture_callback is not None

    class _Result:
        def __init__(self, x: int, y: int) -> None:
            self.x = x
            self.y = y

    dialog._test_slurp.capture_callback(_Result(640, 480))

    assert move.x == 640
    assert move.y == 480
    assert dialog._move_x_spin.get_value_as_int() == 640
    assert dialog._move_y_spin.get_value_as_int() == 480
    assert dialog._move_capture_status.get_text() == "Captured: 640, 480"


def test_macro_editor_repeated_abs_move_capture_updates_every_run(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch, slurp_available=True)
    move = EditableMove(mode="abs", t_us=5000, x=10, y=20)
    dialog._synthetic_moves = [move]
    dialog._timeline._selected = move
    dialog._on_selection_changed(move)

    class _Result:
        def __init__(self, x: int, y: int) -> None:
            self.x = x
            self.y = y

    for x, y in ((100, 200), (300, 400), (500, 600)):
        dialog._on_capture_selected_move_clicked(dialog._move_capture_btn)
        assert dialog._test_slurp.capture_callback is not None
        dialog._test_slurp.capture_callback(_Result(x, y))
        assert dialog._move_x_spin.get_value_as_int() == x
        assert dialog._move_y_spin.get_value_as_int() == y


def test_set_entry_text_if_needed_skips_redundant_updates() -> None:
    class _FakeEntry:
        def __init__(self, text: str) -> None:
            self.text = text
            self.set_calls: list[str] = []

        def get_text(self) -> str:
            return self.text

        def set_text(self, text: str) -> None:
            self.text = text
            self.set_calls.append(text)

    entry = _FakeEntry("echo hi")

    macro_editor_dialog_module._set_entry_text_if_needed(entry, "echo hi")
    assert entry.set_calls == []

    macro_editor_dialog_module._set_entry_text_if_needed(entry, "echo bye")
    assert entry.set_calls == ["echo bye"]


def test_macro_editor_exec_command_edit_does_not_refresh_control_panel(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)

    for mode, command_text in (("exec_sync", "echo hi"), ("exec_async", "printf 'x'")):
        control = EditableControl(mode=mode, t_us=5000, command="")
        dialog._control_events = [control]
        dialog._timeline._selected = control
        dialog._on_selection_changed(control)

        def fail_refresh(_control: EditableControl) -> None:
            raise AssertionError("command typing should not trigger full control refresh")

        original_refresh = dialog._refresh_after_control_change
        dialog._refresh_after_control_change = fail_refresh  # type: ignore[method-assign]
        try:
            dialog._control_cmd_entry.set_text(command_text)
        finally:
            dialog._refresh_after_control_change = original_refresh  # type: ignore[method-assign]

        assert control.command == command_text
        assert dialog._control_cmd_entry.get_text() == command_text


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

    dialog._on_capture_start_position_clicked(dialog._macro_capture_btn)
    assert dialog._test_slurp.capture_callback is not None
    dialog._test_slurp.capture_callback(_Result(640, 480))

    assert dialog._macro_loop_mode == "count"
    assert dialog._macro_loop_count == 4
    assert dialog._macro_loop_count_spin.get_visible() is True
    assert dialog._macro_loop_finish_check.get_visible() is False
    assert dialog._macro_start_x_spin.get_sensitive() is True
    assert dialog._macro_start_y_spin.get_sensitive() is True
    assert dialog._macro_start_x_spin.get_value_as_int() == 640
    assert dialog._macro_start_y_spin.get_value_as_int() == 480
    assert dialog._macro_capture_status.get_text() == "Captured: 640, 480"

    dialog._on_capture_start_position_response(
        dialog._capture_request_id,
        {"status": "error", "message": "Unknown command: get_cursor_position"}
    )

    assert (
        dialog._macro_capture_status.get_text()
        == "Please restart Keymasq Session, then try again"
    )


def test_macro_editor_repeated_start_capture_updates_every_run(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch, slurp_available=True)
    dialog._macro_move_to_start_check.set_active(True)
    dialog._on_macro_move_to_start_toggled(dialog._macro_move_to_start_check)

    class _Result:
        def __init__(self, x: int, y: int) -> None:
            self.x = x
            self.y = y

    for x, y in ((100, 200), (300, 400), (500, 600)):
        dialog._on_capture_start_position_clicked(dialog._macro_capture_btn)
        assert dialog._test_slurp.capture_callback is not None
        dialog._test_slurp.capture_callback(_Result(x, y))
        assert dialog._macro_start_x_spin.get_value_as_int() == x
        assert dialog._macro_start_y_spin.get_value_as_int() == y


def test_macro_editor_delayed_start_capture_ignores_stale_response(monkeypatch) -> None:
    callbacks: list[Callable[[], bool]] = []
    requests: list[Callable[[dict[str, object]], bool | None]] = []

    def fake_timeout_add(_delay, callback):
        callbacks.append(callback)
        return len(callbacks)

    def fake_source_remove(_source_id):
        return None

    def fake_session_request_async(payload, callback, timeout=5.0):
        assert payload == {"command": "get_cursor_position"}
        requests.append(callback)

    monkeypatch.setattr(macro_editor_dialog_module.GLib, "timeout_add", fake_timeout_add)
    monkeypatch.setattr(macro_editor_dialog_module.GLib, "source_remove", fake_source_remove)
    monkeypatch.setattr(
        macro_editor_dialog_module,
        "session_request_async",
        fake_session_request_async,
    )

    dialog = _build_macro_dialog(monkeypatch, slurp_available=False)
    dialog._macro_move_to_start_check.set_active(True)
    dialog._on_macro_move_to_start_toggled(dialog._macro_move_to_start_check)

    dialog._on_capture_start_position_clicked(dialog._macro_capture_btn)
    timer1 = callbacks.pop(0)
    assert timer1() is False
    assert len(requests) == 1
    stale_response = requests.pop(0)

    dialog._on_capture_start_position_clicked(dialog._macro_capture_btn)
    timer2 = callbacks.pop(0)
    stale_response({"status": "ok", "x": 100, "y": 200})

    assert timer2() is False
    assert len(requests) == 1
    fresh_response = requests.pop(0)
    fresh_response({"status": "ok", "x": 300, "y": 400})

    assert dialog._macro_start_x_spin.get_value_as_int() == 300
    assert dialog._macro_start_y_spin.get_value_as_int() == 400


def test_macro_editor_delayed_abs_move_capture_ignores_stale_response(monkeypatch) -> None:
    callbacks: list[Callable[[], bool]] = []
    requests: list[Callable[[dict[str, object]], bool | None]] = []

    def fake_timeout_add(_delay, callback):
        callbacks.append(callback)
        return len(callbacks)

    def fake_source_remove(_source_id):
        return None

    def fake_session_request_async(payload, callback, timeout=5.0):
        assert payload == {"command": "get_cursor_position"}
        requests.append(callback)

    monkeypatch.setattr(macro_editor_dialog_module.GLib, "timeout_add", fake_timeout_add)
    monkeypatch.setattr(macro_editor_dialog_module.GLib, "source_remove", fake_source_remove)
    monkeypatch.setattr(
        macro_editor_dialog_module,
        "session_request_async",
        fake_session_request_async,
    )

    dialog = _build_macro_dialog(monkeypatch, slurp_available=False)
    move = EditableMove(mode="abs", t_us=5000, x=10, y=20)
    dialog._synthetic_moves = [move]
    dialog._timeline._selected = move
    dialog._on_selection_changed(move)

    dialog._on_capture_selected_move_clicked(dialog._move_capture_btn)
    timer1 = callbacks.pop(0)
    assert timer1() is False
    assert len(requests) == 1
    stale_response = requests.pop(0)

    dialog._on_capture_selected_move_clicked(dialog._move_capture_btn)
    timer2 = callbacks.pop(0)
    stale_response({"status": "ok", "x": 100, "y": 200})

    assert timer2() is False
    assert len(requests) == 1
    fresh_response = requests.pop(0)
    fresh_response({"status": "ok", "x": 300, "y": 400})

    assert dialog._move_x_spin.get_value_as_int() == 300
    assert dialog._move_y_spin.get_value_as_int() == 400


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
    dialog._on_macro_loop_mode_changed(dialog._macro_loop_mode_combo)
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
    dialog._control_events = [EditableControl(mode="wait", t_us=5000, duration_ms=80)]

    payload = dialog._build_macro_payload("saved_macro")

    assert payload["name"] == "saved_macro"
    assert payload["loop_mode"] == "toggle"
    assert payload["loop_count"] == 2
    assert payload["loop_stop_behavior"] == "finish_run"
    assert dialog._macro_loop_finish_check.get_visible() is True
    assert payload["move_to_start"] is True
    assert payload["start_x"] == 10
    assert payload["start_y"] == 20
    assert payload["block_mouse_movement"] is True
    assert payload["device_types"] == ["keyboard", "mouse"]
    assert {
        "device_type": "macro",
        "type": 0,
        "code": 0,
        "value": 0,
        "t_us": 5000,
        "macro_action": "wait",
        "duration_ms": 80,
    } in payload["events"]


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
        "loop_stop_behavior": "cancel_run",
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
    dialog._macro_loop_finish_check.set_active(True)

    dialog._on_undo_all_changes(None)

    assert dialog._events[0].press_t_us == 1000
    assert macro_editor_dialog_module._get_dropdown_selected_id(
        dialog._macro_loop_mode_combo,
        macro_editor_dialog_module._LOOP_MODE_OPTIONS,
        "none",
    ) == "hold"
    assert dialog._macro_loop_count_spin.get_value_as_int() == 1
    assert dialog._macro_loop_finish_check.get_visible() is True
    assert dialog._macro_loop_finish_check.get_active() is False
    assert dialog._macro_move_to_start_check.get_active() is False
    assert dialog._macro_start_x_spin.get_value_as_int() == 0
