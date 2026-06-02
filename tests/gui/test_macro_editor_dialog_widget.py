# ruff: noqa: E402, I001
from collections.abc import Callable
from typing import cast

import pytest

gi = pytest.importorskip("gi")

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import evdev

from gi.repository import Gtk

from keymasq.common.models import ActionType, MappingAction
from keymasq.gui.widgets import macro_editor_dialog as macro_editor_dialog_module
from keymasq.gui.widgets.macro_editor_dialog import (
    EditableControl,
    EditableEvent,
    EditableMove,
    _passthrough_track,
    parse_events,
    reconstruct_events,
)
from tests.gui.macro_editor_dialog_support import _build_macro_dialog


def _install_delayed_cursor_position_capture_harness(
    monkeypatch,
) -> Callable[[Callable[[], None]], None]:
    callbacks: list[Callable[[], bool]] = []
    requests: list[Callable[[dict[str, object]], bool | None]] = []

    def fake_timeout_add(_delay, callback: Callable[[], bool]) -> int:
        callbacks.append(callback)
        return len(callbacks)

    def fake_source_remove(_source_id):
        return None

    def fake_session_request_async(
        payload: dict[str, object],
        callback: Callable[[dict[str, object]], bool | None],
        timeout: float = 5.0,
    ) -> None:
        _ = timeout
        assert payload == {"command": "get_cursor_position"}
        requests.append(callback)

    monkeypatch.setattr(macro_editor_dialog_module.GLib, "timeout_add", fake_timeout_add)
    monkeypatch.setattr(macro_editor_dialog_module.GLib, "source_remove", fake_source_remove)
    monkeypatch.setattr(
        macro_editor_dialog_module,
        "session_request_async",
        fake_session_request_async,
    )

    def run_stale_response_sequence(start_capture: Callable[[], None]) -> None:
        start_capture()
        timer1 = callbacks.pop(0)
        assert timer1() is False
        assert len(requests) == 1
        stale_response = requests.pop(0)

        start_capture()
        timer2 = callbacks.pop(0)
        stale_response({"status": "ok", "x": 100, "y": 200})

        assert timer2() is False
        assert len(requests) == 1
        fresh_response = requests.pop(0)
        fresh_response({"status": "ok", "x": 300, "y": 400})

    return run_stale_response_sequence


def test_macro_editor_dialog_size_propagates_parent_width_errors() -> None:
    class BrokenSizingParent:
        def get_width(self) -> int:
            raise RuntimeError("bad parent width")

        def get_height(self) -> int:
            return 800

    parent = cast(Gtk.Window, BrokenSizingParent())

    with pytest.raises(RuntimeError, match="bad parent width"):
        macro_editor_dialog_module._compute_macro_editor_dialog_size(parent)


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
            "duration_us": 25_000,
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


def test_macro_editor_gamepad_events_round_trip_output_id(monkeypatch) -> None:
    raw_events = [
        {
            "device_type": "gamepad",
            "type": evdev.ecodes.EV_KEY,
            "code": evdev.ecodes.BTN_SOUTH,
            "value": 1,
            "t_us": 1000,
            "output_id": "virtual-gamepad-2",
        },
        {
            "device_type": "gamepad",
            "type": evdev.ecodes.EV_KEY,
            "code": evdev.ecodes.BTN_SOUTH,
            "value": 0,
            "t_us": 5000,
            "output_id": "virtual-gamepad-2",
        },
    ]

    events, rel_events, passthrough_events, moves, controls = parse_events(raw_events)

    assert len(events) == 1
    assert events[0].device_type == "gamepad"
    assert events[0].output_id == "virtual-gamepad-2"
    assert _passthrough_track(raw_events[0]) == "gamepad"
    assert reconstruct_events(events, rel_events, passthrough_events, moves, controls) == raw_events


def test_macro_editor_insert_gamepad_action_adds_timeline_event(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    action = MappingAction(
        action_type=ActionType.GAMEPAD,
        target="btn_south",
        output_id="virtual-gamepad-2",
    )

    dialog._on_key_selected_for_insert(Gtk.Window(), action, 12000)

    assert len(dialog._events) == 1
    event = dialog._events[0]
    assert event.device_type == "gamepad"
    assert event.code == evdev.ecodes.BTN_SOUTH
    assert event.output_id == "virtual-gamepad-2"
    payload = dialog._build_macro_payload("demo_macro")
    gamepad_events = [ev for ev in payload["events"] if ev.get("device_type") == "gamepad"]
    assert [ev.get("output_id") for ev in gamepad_events] == [
        "virtual-gamepad-2",
        "virtual-gamepad-2",
    ]


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

    dialog._insert_control_event(EditableControl(mode="wait", t_us=5000, duration_us=150_000))

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
    assert dialog._build_macro_payload("empty_space")["duration_us"] == 5_000_000

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
    assert dialog._build_macro_payload("with_event")["duration_us"] == 6_002_000


def test_macro_editor_payload_includes_wait_controls(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    dialog._control_events = [
        EditableControl(mode="wait", t_us=1000, duration_us=75_000),
        EditableControl(mode="wait_random", t_us=2000, min_us=10_000, max_us=80_000),
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
            "duration_us": 75_000,
        },
        {
            "device_type": "macro",
            "type": 0,
            "code": 0,
            "value": 0,
            "t_us": 2000,
            "macro_action": "wait_random",
            "min_us": 10_000,
            "max_us": 80_000,
        },
    ]


def test_macro_editor_payload_includes_compositor_control(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    dialog._control_events = [
        EditableControl(
            mode="compositor_dispatch",
            t_us=3000,
            compositor_id="hyprland",
            compositor_dispatcher="workspace",
            compositor_args="e+1",
        )
    ]

    payload = dialog._build_macro_payload("compositor_macro")

    assert payload["events"] == [
        {
            "device_type": "macro",
            "type": 0,
            "code": 0,
            "value": 0,
            "t_us": 3000,
            "macro_action": "compositor_dispatch",
            "compositor": "hyprland",
            "dispatcher": "workspace",
            "args": "e+1",
        }
    ]


def test_macro_editor_wait_controls_show_edit_fields(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)

    fixed = EditableControl(mode="wait", t_us=12_000, duration_us=75_000)
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

    random_wait = EditableControl(mode="wait_random", t_us=34_000, min_us=10_000, max_us=80_000)
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


def test_macro_editor_compositor_control_selection_shows_action(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    control = EditableControl(
        mode="compositor_dispatch",
        t_us=12_000,
        compositor_id="hyprland",
        compositor_dispatcher="workspace",
        compositor_args="e+1",
    )
    dialog._timeline._selected = control

    dialog._on_selection_changed(control)

    assert dialog._prop_title.get_label() == "Compositor Action"
    assert dialog._press_spin.get_value_as_int() == 12
    assert "workspace e+1" in dialog._key_info_label.get_label()
    assert dialog._change_key_btn.get_visible() is True
    assert dialog._change_key_btn.get_label() == "Change Action..."
    assert dialog._control_cmd_row.get_visible() is False
    assert dialog._control_sync_row.get_visible() is False


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
    run_stale_response_sequence = _install_delayed_cursor_position_capture_harness(
        monkeypatch
    )
    dialog = _build_macro_dialog(monkeypatch, slurp_available=False)
    dialog._macro_move_to_start_check.set_active(True)
    dialog._on_macro_move_to_start_toggled(dialog._macro_move_to_start_check)

    run_stale_response_sequence(
        lambda: dialog._on_capture_start_position_clicked(dialog._macro_capture_btn)
    )

    assert dialog._macro_start_x_spin.get_value_as_int() == 300
    assert dialog._macro_start_y_spin.get_value_as_int() == 400


def test_macro_editor_delayed_abs_move_capture_ignores_stale_response(monkeypatch) -> None:
    run_stale_response_sequence = _install_delayed_cursor_position_capture_harness(
        monkeypatch
    )
    dialog = _build_macro_dialog(monkeypatch, slurp_available=False)
    move = EditableMove(mode="abs", t_us=5000, x=10, y=20)
    dialog._synthetic_moves = [move]
    dialog._timeline._selected = move
    dialog._on_selection_changed(move)

    run_stale_response_sequence(
        lambda: dialog._on_capture_selected_move_clicked(dialog._move_capture_btn)
    )

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
    dialog._control_events = [EditableControl(mode="wait", t_us=5000, duration_us=80_000)]

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
        "duration_us": 80_000,
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
        "duration_us": 3000,
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


def test_macro_editor_time_mapping_updates_all_event_kinds(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    keyboard = EditableEvent(
        device_type="keyboard",
        ev_type=evdev.ecodes.EV_KEY,
        code=evdev.ecodes.KEY_A,
        press_t_us=1000,
        release_t_us=3000,
    )
    mouse = EditableEvent(
        device_type="mouse",
        ev_type=evdev.ecodes.EV_KEY,
        code=evdev.ecodes.BTN_LEFT,
        press_t_us=5000,
        release_t_us=9000,
    )
    move = EditableMove(mode="abs", t_us=7000, x=11, y=22)
    control = EditableControl(mode="wait", t_us=8000, duration_us=2000)
    passthrough = {
        "device_type": "keyboard",
        "type": evdev.ecodes.EV_KEY,
        "code": evdev.ecodes.KEY_B,
        "value": 2,
        "t_us": 6000,
    }
    rel = {
        "device_type": "mouse",
        "type": evdev.ecodes.EV_REL,
        "code": evdev.ecodes.REL_X,
        "value": 4,
        "t_us": 4000,
    }
    dialog._events = [mouse, keyboard]
    dialog._rel_events = [rel]
    dialog._passthrough_events = [passthrough]
    dialog._synthetic_moves = [move]
    dialog._control_events = [control]

    mapping = dialog._build_time_mapping_with_gap_limits(
        scale=2.0,
        min_gap_us=500,
        max_gap_us=1500,
    )
    dialog._apply_time_map(mapping)
    dialog._recompute_duration()

    assert dialog._events == [keyboard, mouse]
    assert keyboard.press_t_us == 1000
    assert keyboard.release_t_us == 2500
    assert rel["t_us"] == 4000
    assert mouse.press_t_us == 5500
    assert passthrough["t_us"] == 7000
    assert move.t_us == 8500
    assert control.t_us == 10500
    assert dialog._duration_us == 12000


def test_macro_editor_trim_and_gap_helpers_keep_selection_consistent(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    removed = EditableEvent(
        device_type="keyboard",
        ev_type=evdev.ecodes.EV_KEY,
        code=evdev.ecodes.KEY_A,
        press_t_us=1000,
        release_t_us=2000,
    )
    kept = EditableEvent(
        device_type="keyboard",
        ev_type=evdev.ecodes.EV_KEY,
        code=evdev.ecodes.KEY_B,
        press_t_us=5000,
        release_t_us=9000,
    )
    move = EditableMove(mode="rel", t_us=6000, x=1, y=2)
    control = EditableControl(mode="exec_sync", t_us=7000, command="echo hi")
    dialog._events = [removed, kept]
    dialog._rel_events = [
        {
            "device_type": "mouse",
            "type": evdev.ecodes.EV_REL,
            "code": evdev.ecodes.REL_X,
            "value": 3,
            "t_us": 6500,
        }
    ]
    dialog._passthrough_events = [
        {
            "device_type": "mouse",
            "type": evdev.ecodes.EV_KEY,
            "code": evdev.ecodes.BTN_RIGHT,
            "value": 2,
            "t_us": 7500,
        }
    ]
    dialog._synthetic_moves = [move]
    dialog._control_events = [control]
    dialog._timeline._selected = removed

    dialog._set_startpoint(4000)

    assert dialog._timeline._selected is None
    assert dialog._revealer.get_reveal_child() is False
    assert dialog._events == [kept]
    assert kept.press_t_us == 1000
    assert kept.release_t_us == 5000
    assert move.t_us == 2000
    assert control.t_us == 3000

    dialog._timeline._selected = kept
    dialog._set_endpoint(2500)

    assert dialog._events == [kept]
    assert kept.release_t_us == 2500
    assert dialog._synthetic_moves == [move]
    assert dialog._control_events == []
    assert dialog._timeline._selected is kept


def test_macro_editor_shift_timeline_for_gap_respects_scopes(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    keyboard = EditableEvent(
        device_type="keyboard",
        ev_type=evdev.ecodes.EV_KEY,
        code=evdev.ecodes.KEY_A,
        press_t_us=1000,
        release_t_us=2000,
    )
    mouse = EditableEvent(
        device_type="mouse",
        ev_type=evdev.ecodes.EV_KEY,
        code=evdev.ecodes.BTN_LEFT,
        press_t_us=1000,
        release_t_us=2000,
    )
    gamepad = EditableEvent(
        device_type="gamepad",
        ev_type=evdev.ecodes.EV_KEY,
        code=evdev.ecodes.BTN_SOUTH,
        press_t_us=1000,
        release_t_us=2000,
        output_id="virtual-gamepad-2",
    )
    excluded = EditableControl(mode="wait", t_us=1000, duration_us=500)
    shifted = EditableControl(mode="wait_random", t_us=2000, min_us=500, max_us=1500)
    move = EditableMove(mode="rel", t_us=1000, x=1, y=1)
    dialog._events = [keyboard, mouse, gamepad]
    dialog._synthetic_moves = [move]
    dialog._control_events = [excluded, shifted]
    dialog._rel_events = [
        {
            "device_type": "mouse",
            "type": evdev.ecodes.EV_REL,
            "code": evdev.ecodes.REL_Y,
            "value": -1,
            "t_us": 1000,
        }
    ]
    dialog._passthrough_events = [
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_KEY,
            "code": evdev.ecodes.KEY_C,
            "value": 2,
            "t_us": 1000,
        },
        {
            "device_type": "mouse",
            "type": evdev.ecodes.EV_REL,
            "code": evdev.ecodes.REL_X,
            "value": 5,
            "t_us": 1000,
        },
    ]

    assert dialog._shift_timeline_for_gap(
        at_us=1000,
        delta_us=500,
        scope="movement",
        exclude_control=excluded,
    ) is True
    assert keyboard.press_t_us == 1000
    assert mouse.press_t_us == 1000
    assert gamepad.press_t_us == 1000
    assert move.t_us == 1500
    assert dialog._rel_events[0]["t_us"] == 1500
    assert excluded.t_us == 1000
    assert shifted.t_us == 2500
    assert dialog._passthrough_events[0]["t_us"] == 1000
    assert dialog._passthrough_events[1]["t_us"] == 1500

    assert dialog._shift_timeline_for_gap(
        at_us=1000,
        delta_us=-750,
        scope="keyboard",
        exclude_control=None,
    ) is True
    assert keyboard.press_t_us == 250
    assert keyboard.release_t_us == 1250
    assert mouse.press_t_us == 1000
    assert dialog._passthrough_events[0]["t_us"] == 250

    assert dialog._shift_timeline_for_gap(
        at_us=1000,
        delta_us=500,
        scope="gamepad",
        exclude_control=None,
    ) is True
    assert keyboard.press_t_us == 250
    assert mouse.press_t_us == 1000
    assert gamepad.press_t_us == 1500


def test_macro_editor_timeline_draws_and_hit_tests_all_tracks(monkeypatch) -> None:
    import cairo

    dialog = _build_macro_dialog(monkeypatch)
    keyboard = EditableEvent(
        device_type="keyboard",
        ev_type=evdev.ecodes.EV_KEY,
        code=evdev.ecodes.KEY_A,
        press_t_us=10_000,
        release_t_us=180_000,
    )
    mouse = EditableEvent(
        device_type="mouse",
        ev_type=evdev.ecodes.EV_KEY,
        code=evdev.ecodes.BTN_LEFT,
        press_t_us=60_000,
        release_t_us=220_000,
    )
    gamepad = EditableEvent(
        device_type="gamepad",
        ev_type=evdev.ecodes.EV_KEY,
        code=evdev.ecodes.BTN_SOUTH,
        press_t_us=80_000,
        release_t_us=200_000,
        output_id="virtual-gamepad-2",
    )
    move = EditableMove(mode="rel", t_us=40_000, x=8, y=-6)
    absolute_move = EditableMove(mode="abs", t_us=280_000, x=640, y=360)
    wait = EditableControl(mode="wait", t_us=150_000, duration_us=40_000)
    command = EditableControl(mode="exec_sync", t_us=260_000, command="echo hi")
    compositor = EditableControl(
        mode="compositor_dispatch",
        t_us=300_000,
        compositor_dispatcher="workspace",
        compositor_args="1",
    )
    keyboard_passthrough = {
        "device_type": "keyboard",
        "type": evdev.ecodes.EV_KEY,
        "code": evdev.ecodes.KEY_B,
        "value": 2,
        "t_us": 120_000,
    }
    movement_passthrough = {
        "device_type": "mouse",
        "type": evdev.ecodes.EV_REL,
        "code": evdev.ecodes.REL_Y,
        "value": -3,
        "t_us": 210_000,
    }
    dialog._events = [keyboard, mouse, gamepad]
    dialog._rel_events = [
        {
            "device_type": "mouse",
            "type": evdev.ecodes.EV_REL,
            "code": evdev.ecodes.REL_X,
            "value": 4,
            "t_us": 90_000,
        }
    ]
    dialog._passthrough_events = [keyboard_passthrough, movement_passthrough]
    dialog._synthetic_moves = [move, absolute_move]
    dialog._control_events = [wait, command, compositor]
    dialog._duration_us = 350_000
    timeline = dialog._timeline
    timeline._recompute_lanes()

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1200, 520)
    context = cairo.Context(surface)
    timeline._draw(None, context, 1200, 520, None)

    assert timeline._get_track_at_y(timeline._kb_y + 5) == "keyboard"
    assert timeline._get_track_at_y(timeline._m_y + 5) == "mouse"
    assert timeline._get_track_at_y(timeline._g_y + 5) == "gamepad"
    assert timeline._get_track_at_y(timeline._wave_y + 5) == "movement"
    assert timeline._hit_test(
        timeline._time_to_x(keyboard.press_t_us) + 2,
        timeline._kb_y + 12,
    ) is keyboard
    assert timeline._hit_test(
        timeline._time_to_x(mouse.press_t_us) + 2,
        timeline._m_y + 12,
    ) is mouse
    assert timeline._hit_test(
        timeline._time_to_x(gamepad.press_t_us) + 2,
        timeline._g_y + 12,
    ) is gamepad
    assert timeline._hit_test_move(
        timeline._time_to_x(move.t_us),
        timeline._wave_y + 14,
    ) is move
    assert timeline._hit_test_control(
        timeline._time_to_x(compositor.t_us),
        timeline._wave_y + timeline.TRACK_HEIGHT - 14,
    ) is compositor
