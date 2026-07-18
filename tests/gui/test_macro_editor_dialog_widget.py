# ruff: noqa: E402, I001
from collections.abc import Callable
from typing import cast

import pytest

gi = pytest.importorskip("gi")

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import evdev

from gi.repository import Gtk

from keymasq.common.model.core import ActionType
from keymasq.common.model.actions import MappingAction
from keymasq.gui.widgets.macro_editor import dialog as macro_editor_dialog_module
from keymasq.gui.widgets.macro_editor.model import (
    EditableControl,
    EditableEvent,
    EditableMove,
    _passthrough_track,
    parse_events,
    reconstruct_events,
)
from keymasq.gui.widgets.macro_editor.panel.controls import _set_entry_text_if_needed
from keymasq.gui.widgets.macro_editor.panel.settings import (
    _LOOP_MODE_OPTIONS,
    _get_dropdown_selected_id,
    _set_dropdown_selected_id,
)
import keymasq.gui.widgets.position_capture as position_capture_module
from tests.gui.macro_editor_dialog_support import _build_macro_dialog
from tests.gui.support import collect_widgets, iter_widget_children


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

    monkeypatch.setattr(position_capture_module.GLib, "timeout_add", fake_timeout_add)
    monkeypatch.setattr(position_capture_module.GLib, "source_remove", fake_source_remove)
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
        _get_dropdown_selected_id(
            dialog._macro_loop_mode_combo,
            _LOOP_MODE_OPTIONS,
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
    assert dialog._move_to_start_row.get_visible() is True
    assert dialog._move_to_start_capture_row.get_visible() is True
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


def test_macro_editor_preserves_type_metadata_when_events_are_unchanged(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    raw_events = [
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
            "t_us": 6000,
        },
    ]
    dialog._macro_data = {
        "name": "type_macro",
        "events": raw_events,
        "type_binding": True,
        "type_text": "a",
        "type_down_ms": 5,
        "type_pause_ms": 10,
        "type_use_unicode_input": False,
    }
    (
        dialog._events,
        dialog._rel_events,
        dialog._passthrough_events,
        dialog._synthetic_moves,
        dialog._control_events,
    ) = parse_events(raw_events)

    payload = dialog._build_macro_payload("type_macro")

    assert payload["type_binding"] is True
    assert payload["type_text"] == "a"


def test_macro_editor_clears_type_metadata_when_events_change(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    raw_events = [
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
            "t_us": 6000,
        },
    ]
    dialog._macro_data = {
        "name": "type_macro",
        "events": raw_events,
        "type_binding": True,
        "type_text": "a",
        "type_down_ms": 5,
        "type_pause_ms": 10,
        "type_use_unicode_input": False,
    }
    (
        dialog._events,
        dialog._rel_events,
        dialog._passthrough_events,
        dialog._synthetic_moves,
        dialog._control_events,
    ) = parse_events(raw_events)
    dialog._events[0].code = evdev.ecodes.KEY_B

    payload = dialog._build_macro_payload("type_macro")

    assert payload["type_binding"] is False
    assert "type_text" not in payload
    assert "type_down_ms" not in payload
    assert "type_pause_ms" not in payload
    assert "type_use_unicode_input" not in payload


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


def test_macro_editor_gamepad_axis_event_is_editable_and_serialized(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    event = EditableEvent(
        device_type="gamepad",
        ev_type=evdev.ecodes.EV_ABS,
        code=evdev.ecodes.ABS_X,
        press_t_us=1000,
        release_t_us=1001,
        value=321,
        output_id="virtual-gamepad-2",
    )
    dialog._events = [event]
    dialog._duration_us = 1001
    dialog._timeline._selected = event

    dialog._on_selection_changed(event)
    dialog._press_spin.set_value(5)
    dialog._on_press_changed(dialog._press_spin)
    dialog._move_x_spin.set_value(123)
    dialog._on_move_x_changed(dialog._move_x_spin)

    assert event.press_t_us == 5000
    assert event.release_t_us == 5001
    assert event.value == 123
    assert dialog._prop_title.get_label() == "Left Stick X"
    assert dialog._move_x_label.get_label() == "Value:"
    assert dialog._move_y_spin.get_visible() is False
    assert dialog._change_key_btn.get_label() == "Change Axis..."
    assert dialog._stats_label.get_label() == "0.005s · 1 events"
    assert dialog._build_macro_payload("axis_macro")["events"] == [
        {
            "device_type": "gamepad",
            "type": evdev.ecodes.EV_ABS,
            "code": evdev.ecodes.ABS_X,
            "value": 123,
            "t_us": 5000,
            "output_id": "virtual-gamepad-2",
        }
    ]


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
    assert dialog._move_capture_btn.get_visible() is True

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


def test_macro_editor_move_modify_button_only_for_natural(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch, slurp_available=True)

    rel_move = EditableMove(mode="rel", t_us=5000, x=10, y=20)
    dialog._timeline._selected = rel_move
    dialog._on_selection_changed(rel_move)
    assert dialog._change_key_btn.get_visible() is False
    assert dialog._move_capture_btn.get_visible() is False

    abs_move = EditableMove(mode="abs", t_us=5000, x=10, y=20)
    dialog._timeline._selected = abs_move
    dialog._on_selection_changed(abs_move)
    assert dialog._change_key_btn.get_visible() is False
    assert dialog._move_capture_btn.get_visible() is True

    natural_move = EditableMove(mode="natural", t_us=5000, x=10, y=20)
    dialog._timeline._selected = natural_move
    dialog._on_selection_changed(natural_move)
    assert dialog._change_key_btn.get_visible() is True
    assert dialog._change_key_btn.get_label() == "Modify Move"
    assert dialog._move_capture_btn.get_visible() is True


def test_macro_editor_absolute_move_controls_use_screen_coordinate_range(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    move = EditableMove(mode="natural", t_us=0, x=25_000, y=-24_000)
    dialog._synthetic_moves = [move]
    dialog._timeline._selected = move

    dialog._on_selection_changed(move)

    assert dialog._move_x_spin.get_value_as_int() == 25_000
    assert dialog._move_y_spin.get_value_as_int() == -24_000
    assert dialog._move_x_spin.get_adjustment().get_lower() == -100_000
    assert dialog._move_x_spin.get_adjustment().get_upper() == 100_000
    payload = dialog._build_macro_payload("demo_macro")
    assert payload["events"][0]["macro_action"] == "mouse_move_natural_abs"
    assert payload["events"][0]["x"] == 25_000
    assert payload["events"][0]["y"] == -24_000


def test_macro_editor_relative_move_controls_keep_compact_coordinate_range(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    move = EditableMove(mode="rel", t_us=0, x=100, y=-100)
    dialog._synthetic_moves = [move]
    dialog._timeline._selected = move

    dialog._on_selection_changed(move)

    assert dialog._move_x_spin.get_adjustment().get_lower() == -10_000
    assert dialog._move_x_spin.get_adjustment().get_upper() == 10_000


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

    _set_entry_text_if_needed(entry, "echo hi")
    assert entry.set_calls == []

    _set_entry_text_if_needed(entry, "echo bye")
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

    _set_dropdown_selected_id(
        dialog._macro_loop_mode_combo,
        _LOOP_MODE_OPTIONS,
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
        dialog._start_position_capture.request_id,
        {"status": "error", "message": "Unknown command: get_cursor_position"},
    )

    assert (
        dialog._macro_capture_status.get_text() == "Please restart Keymasq Session, then try again"
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
    run_stale_response_sequence = _install_delayed_cursor_position_capture_harness(monkeypatch)
    dialog = _build_macro_dialog(monkeypatch, slurp_available=False)
    dialog._macro_move_to_start_check.set_active(True)
    dialog._on_macro_move_to_start_toggled(dialog._macro_move_to_start_check)

    run_stale_response_sequence(
        lambda: dialog._on_capture_start_position_clicked(dialog._macro_capture_btn)
    )

    assert dialog._macro_start_x_spin.get_value_as_int() == 300
    assert dialog._macro_start_y_spin.get_value_as_int() == 400


def test_macro_editor_delayed_abs_move_capture_ignores_stale_response(monkeypatch) -> None:
    run_stale_response_sequence = _install_delayed_cursor_position_capture_harness(monkeypatch)
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
    _set_dropdown_selected_id(
        dialog._macro_loop_mode_combo,
        _LOOP_MODE_OPTIONS,
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
    assert "move_to_start" not in payload
    assert "start_x" not in payload
    assert "start_y" not in payload
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


def test_macro_editor_footer_is_pinned_and_includes_apply(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)

    overlay = dialog.get_child()
    assert isinstance(overlay, Gtk.Overlay)
    frame = overlay.get_child()
    assert isinstance(frame, Gtk.Frame)
    root = frame.get_child()
    children = list(iter_widget_children(root))
    footer_spacer = children[-2]
    footer = children[-1]

    assert footer_spacer.get_vexpand() is True
    assert footer.get_halign() == Gtk.Align.END
    assert [
        button.get_label() for button in collect_widgets(footer, Gtk.Button, include_self=True)
    ] == ["Cancel", "Save as Copy…", "Apply", "Save Changes"]


def test_macro_editor_apply_saves_without_closing(monkeypatch) -> None:
    from keymasq.gui.session_client import GuiTaskResult

    dialog = _build_macro_dialog(monkeypatch)
    dialog._macro_name = "demo_macro"
    dialog._macro_exists = True
    dialog._macro_data = {"name": "demo_macro", "revision": 2, "created_at": "old"}
    dialog._duration_us = 4000
    dialog._events = [
        EditableEvent(
            device_type="keyboard",
            ev_type=evdev.ecodes.EV_KEY,
            code=evdev.ecodes.KEY_A,
            press_t_us=1000,
            release_t_us=4000,
        )
    ]

    requests: list[dict] = []
    reloads: list[bool] = []
    closed: list[bool] = []

    def fake_session_request(payload):
        requests.append(payload)
        saved_macro = dict(payload["macro"])
        saved_macro["revision"] = 3
        return {"status": "ok", "macro": saved_macro}

    def fake_run_gui_task(worker, callback, *, on_start=None, on_done=None) -> None:
        if on_start is not None:
            on_start()
        callback(GuiTaskResult(value=worker()))
        if on_done is not None:
            on_done()

    monkeypatch.setattr(macro_editor_dialog_module, "session_request", fake_session_request)
    monkeypatch.setattr(macro_editor_dialog_module, "run_gui_task", fake_run_gui_task)
    monkeypatch.setattr(
        macro_editor_dialog_module,
        "notify_session_reload_async",
        lambda: reloads.append(True),
    )
    monkeypatch.setattr(
        macro_editor_dialog_module.MacroEditorDialog,
        "close",
        lambda self: closed.append(True),
    )

    apply_btn = Gtk.Button(label="Apply")
    dialog._on_apply(apply_btn)

    assert requests[0]["command"] == "update_macro"
    assert requests[0]["name"] == "demo_macro"
    assert requests[0]["expected_revision"] == 2
    assert dialog._macro_data["revision"] == 3
    assert dialog._initial_macro_data["revision"] == 3
    assert dialog._macro_name == "demo_macro"
    assert reloads == [True]
    assert closed == []
    assert apply_btn.get_sensitive() is True


def test_macro_editor_can_select_first_event_on_initial_load(monkeypatch) -> None:
    from keymasq.gui.session_client import GuiTaskResult

    dialog = _build_macro_dialog(monkeypatch)

    dialog._on_initial_state_loaded(
        GuiTaskResult(
            value={
                "timeout_max": 30000,
                "compositor_status": {},
                "macro": {
                    "name": "demo_macro",
                    "events": [
                        {
                            "device_type": "mouse",
                            "type": evdev.ecodes.EV_REL,
                            "code": evdev.ecodes.REL_X,
                            "value": 5,
                            "t_us": 0,
                        },
                        {
                            "device_type": "keyboard",
                            "type": evdev.ecodes.EV_KEY,
                            "code": evdev.ecodes.KEY_A,
                            "value": 1,
                            "t_us": 0,
                        },
                        {
                            "device_type": "macro",
                            "type": 0,
                            "code": 0,
                            "value": 0,
                            "t_us": 0,
                            "macro_action": "mouse_move_natural_abs",
                            "x": 123,
                            "y": 456,
                            "speed": 100_000.0,
                            "jitter": 0.0,
                            "curve": "linear",
                            "tolerance": 2,
                            "max_duration_ms": 3000,
                            "stop_on_failure": False,
                        },
                        {
                            "device_type": "keyboard",
                            "type": evdev.ecodes.EV_KEY,
                            "code": evdev.ecodes.KEY_A,
                            "value": 0,
                            "t_us": 1000,
                        },
                    ],
                    "duration_us": 1000,
                },
            }
        )
    )

    selected = dialog._timeline._selected
    assert isinstance(selected, dict)
    assert selected["type"] == evdev.ecodes.EV_REL
    assert selected["code"] == evdev.ecodes.REL_X


def test_macro_editor_clean_close_skips_unsaved_warning(monkeypatch) -> None:
    from keymasq.gui.session_client import GuiTaskResult

    dialog = _build_macro_dialog(monkeypatch)
    closed: list[bool] = []
    alerts: list[tuple[object, object]] = []
    monkeypatch.setattr(dialog, "force_close", lambda: closed.append(True))
    monkeypatch.setattr(
        macro_editor_dialog_module.Adw.AlertDialog,
        "present",
        lambda alert, parent: alerts.append((alert, parent)),
    )

    result = {
        "macro": {
            "name": "demo_macro",
            "revision": 2,
            "events": [],
            "duration_us": 0,
        },
    }
    assert dialog._on_initial_state_loaded(GuiTaskResult(value=result)) is False
    assert dialog.get_can_close() is True

    dialog._request_close()

    assert alerts == []
    assert closed == [True]


def test_macro_editor_failed_load_closes_without_unsaved_warning(monkeypatch) -> None:
    from keymasq.gui.session_client import GuiTaskResult

    dialog = _build_macro_dialog(monkeypatch)
    closed: list[bool] = []
    alerts: list[tuple[object, object]] = []
    monkeypatch.setattr(dialog, "force_close", lambda: closed.append(True))
    monkeypatch.setattr(
        macro_editor_dialog_module.Adw.AlertDialog,
        "present",
        lambda alert, parent: alerts.append((alert, parent)),
    )

    result = {
        "timeout_max": 30000,
        "macro": None,
    }
    assert dialog._on_initial_state_loaded(GuiTaskResult(value=result)) is False
    assert dialog.get_can_close() is True
    assert dialog._initial_macro_data == dialog._current_macro_payload()

    dialog._request_close()

    assert alerts == []
    assert closed == [True]


def test_macro_editor_content_is_read_only_until_initial_load_finishes(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)

    assert dialog._editor_content.get_sensitive() is False
    assert dialog._loading_overlay.get_visible() is True
    assert dialog._loading_spinner.get_property("spinning") is True

    dialog._exit_loading_state()

    assert dialog._editor_content.get_sensitive() is True
    assert dialog._loading_overlay.get_visible() is False
    assert dialog._loading_spinner.get_property("spinning") is False

    dialog._exit_loading_state()

    assert dialog._editor_content.get_sensitive() is True
    assert dialog._loading_overlay.get_visible() is False


def test_macro_editor_load_completion_unlocks_content(monkeypatch) -> None:
    from keymasq.gui.session_client import GuiTaskResult
    from keymasq.gui.widgets.macro_editor.controller.load import LoadControllerMixin

    dialog = _build_macro_dialog(monkeypatch)

    scheduled: list[
        tuple[
            Callable[[], dict[str, object]],
            Callable[[GuiTaskResult[dict[str, object]]], bool | None],
            Callable[[], None] | None,
        ]
    ] = []

    def fake_run_gui_task(worker, callback, *, on_start=None, on_done=None) -> None:
        if on_start is not None:
            on_start()
        scheduled.append((worker, callback, on_done))

    def fake_session_request(payload):
        if payload["command"] == "get_macro":
            return {
                "status": "ok",
                "macro": {
                    "name": "demo_macro",
                    "revision": 2,
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
                            "t_us": 2000,
                        },
                    ],
                    "duration_us": 2000,
                },
            }
        return {"status": "ok", "macro_exec_timeout_max_ms": 30000}

    monkeypatch.setattr(macro_editor_dialog_module, "run_gui_task", fake_run_gui_task)
    monkeypatch.setattr(macro_editor_dialog_module, "session_request", fake_session_request)

    LoadControllerMixin._load_initial_state_async(dialog)

    assert dialog._editor_content.get_sensitive() is False
    assert len(scheduled) == 1

    worker, callback, on_done = scheduled[0]
    assert callback(GuiTaskResult(value=worker())) is False
    if on_done is not None:
        on_done()

    assert dialog._editor_content.get_sensitive() is True
    assert dialog._loading_overlay.get_visible() is False
    assert len(dialog._events) == 1
    assert dialog._initial_state_loaded is True


def test_macro_editor_load_ignored_after_close_during_load(monkeypatch) -> None:
    from keymasq.gui.session_client import GuiTaskResult

    dialog = _build_macro_dialog(monkeypatch)
    closed: list[bool] = []
    monkeypatch.setattr(dialog, "force_close", lambda: closed.append(True))

    dialog._force_close_without_warning()

    assert closed == [True]
    assert dialog._load_aborted is True

    result = {
        "macro": {
            "name": "demo_macro",
            "revision": 2,
            "events": [
                {
                    "device_type": "keyboard",
                    "type": evdev.ecodes.EV_KEY,
                    "code": evdev.ecodes.KEY_A,
                    "value": 1,
                    "t_us": 1000,
                },
            ],
            "duration_us": 1000,
        },
    }
    assert dialog._on_initial_state_loaded(GuiTaskResult(value=result)) is False
    dialog._exit_loading_state()

    assert dialog._events == []
    assert dialog._initial_state_loaded is False
    assert dialog._editor_content.get_sensitive() is False
    assert dialog._loading_overlay.get_visible() is True


def test_macro_editor_unsaved_close_warns_and_can_discard(monkeypatch) -> None:
    from keymasq.gui.session_client import GuiTaskResult

    dialog = _build_macro_dialog(monkeypatch)
    closed: list[bool] = []
    alerts: list[tuple[object, object]] = []
    monkeypatch.setattr(dialog, "force_close", lambda: closed.append(True))
    monkeypatch.setattr(
        macro_editor_dialog_module.Adw.AlertDialog,
        "present",
        lambda alert, parent: alerts.append((alert, parent)),
    )

    result = {
        "macro": {
            "name": "demo_macro",
            "revision": 2,
            "events": [],
            "duration_us": 0,
        },
    }
    assert dialog._on_initial_state_loaded(GuiTaskResult(value=result)) is False

    dialog._name_entry.set_text("changed_macro")
    assert dialog.get_can_close() is False

    dialog._request_close()
    assert closed == []
    assert len(alerts) == 1
    assert alerts[0][1] is dialog

    dialog._on_unsaved_close_response(alerts[0][0], "cancel")
    assert closed == []

    dialog._request_close()
    assert len(alerts) == 2

    dialog._on_unsaved_close_response(alerts[1][0], "discard")
    assert closed == [True]
    assert dialog.get_can_close() is True


def test_macro_editor_save_failures_distinguish_conflict_from_generic_error(
    monkeypatch,
) -> None:
    from keymasq.gui.session_client import GuiTaskResult

    dialog = _build_macro_dialog(monkeypatch)
    alerts: list[tuple[str | None, str | None, object]] = []

    def fake_present(alert, parent) -> None:
        alerts.append((alert.get_heading(), alert.get_body(), parent))

    monkeypatch.setattr(macro_editor_dialog_module.Adw.AlertDialog, "present", fake_present)

    conflict = GuiTaskResult(
        value={"status": "error", "message": "Macro 'demo_macro' already exists"}
    )
    assert (
        dialog._on_save_finished(
            conflict,
            "demo_macro",
            dialog._current_macro_payload(),
            close_after_save=False,
        )
        is False
    )

    generic = GuiTaskResult(value={"status": "error", "message": "Revision conflict"})
    assert (
        dialog._on_save_finished(
            generic,
            "demo_macro",
            dialog._current_macro_payload(),
            close_after_save=False,
        )
        is False
    )

    worker_error = GuiTaskResult[dict](error=RuntimeError("worker failed"))
    assert (
        dialog._on_save_finished(
            worker_error,
            "demo_macro",
            dialog._current_macro_payload(),
            close_after_save=False,
        )
        is False
    )

    assert alerts == [
        (
            "Name Conflict",
            "A macro named 'demo_macro' already exists. Choose a different name.",
            dialog._parent,
        ),
        ("Unable To Save Macro", "Revision conflict", dialog._parent),
        ("Unable To Save Macro", "worker failed", dialog._parent),
    ]


def test_macro_editor_save_in_flight_disables_footer_and_blocks_duplicate(
    monkeypatch,
) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    dialog._macro_name = "demo_macro"
    dialog._macro_exists = True
    dialog._macro_data = {"name": "demo_macro", "revision": 2}

    scheduled: list[
        tuple[
            Callable[[], dict | None],
            Callable[[macro_editor_dialog_module.GuiTaskResult[dict | None]], bool | None],
            Callable[[], None] | None,
        ]
    ] = []
    requests: list[dict] = []

    def fake_session_request(payload):
        requests.append(payload)
        return {"status": "ok", "macro": {**payload["macro"], "revision": 3}}

    def fake_run_gui_task(worker, callback, *, on_start=None, on_done=None) -> None:
        if on_start is not None:
            on_start()
        scheduled.append((worker, callback, on_done))

    monkeypatch.setattr(macro_editor_dialog_module, "session_request", fake_session_request)
    monkeypatch.setattr(macro_editor_dialog_module, "run_gui_task", fake_run_gui_task)
    monkeypatch.setattr(macro_editor_dialog_module, "notify_session_reload_async", lambda: None)

    apply_btn = dialog._footer_action_buttons[2]
    save_btn = dialog._footer_action_buttons[3]

    dialog._on_apply(apply_btn)

    assert dialog._save_in_flight is True
    assert [button.get_sensitive() for button in dialog._footer_action_buttons] == [
        False,
        False,
        False,
        False,
    ]
    assert dialog.get_can_close() is False

    dialog._on_save(save_btn)
    dialog._on_unsaved_close_response(macro_editor_dialog_module.Adw.AlertDialog(), "save")

    assert len(scheduled) == 1
    assert requests == []

    worker, callback, on_done = scheduled[0]
    assert callback(macro_editor_dialog_module.GuiTaskResult(value=worker())) is False
    if on_done is not None:
        on_done()

    assert requests[0]["command"] == "update_macro"
    assert dialog._save_in_flight is False
    assert [button.get_sensitive() for button in dialog._footer_action_buttons] == [
        True,
        True,
        True,
        True,
    ]


def test_macro_editor_unsaved_close_save_response_saves_and_closes(monkeypatch) -> None:
    from keymasq.gui.session_client import GuiTaskResult

    dialog = _build_macro_dialog(monkeypatch)
    closed: list[bool] = []
    alerts: list[tuple[object, object]] = []
    requests: list[dict] = []
    reloads: list[bool] = []

    def fake_session_request(payload):
        requests.append(payload)
        saved_macro = dict(payload["macro"])
        saved_macro["revision"] = 3
        return {"status": "ok", "macro": saved_macro}

    def fake_run_gui_task(worker, callback, *, on_start=None, on_done=None) -> None:
        if on_start is not None:
            on_start()
        callback(GuiTaskResult(value=worker()))
        if on_done is not None:
            on_done()

    monkeypatch.setattr(dialog, "force_close", lambda: closed.append(True))
    monkeypatch.setattr(
        macro_editor_dialog_module.Adw.AlertDialog,
        "present",
        lambda alert, parent: alerts.append((alert, parent)),
    )
    monkeypatch.setattr(macro_editor_dialog_module, "session_request", fake_session_request)
    monkeypatch.setattr(macro_editor_dialog_module, "run_gui_task", fake_run_gui_task)
    monkeypatch.setattr(
        macro_editor_dialog_module,
        "notify_session_reload_async",
        lambda: reloads.append(True),
    )

    result = {
        "macro": {
            "name": "demo_macro",
            "revision": 2,
            "events": [],
            "duration_us": 0,
            "block_mouse_movement": False,
        },
    }
    assert dialog._on_initial_state_loaded(GuiTaskResult(value=result)) is False

    assert dialog._move_to_start_row.get_visible() is False
    assert dialog._move_to_start_capture_row.get_visible() is False
    dialog._macro_block_mouse_check.set_active(True)
    assert dialog.get_can_close() is False

    dialog._request_close()
    assert len(alerts) == 1

    dialog._on_unsaved_close_response(alerts[0][0], "save")

    assert requests[0]["command"] == "update_macro"
    assert requests[0]["name"] == "demo_macro"
    assert requests[0]["expected_revision"] == 2
    assert requests[0]["macro"]["block_mouse_movement"] is True
    assert dialog._macro_data["revision"] == 3
    assert reloads == [True]
    assert closed == [True]
    assert dialog.get_can_close() is True


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
    _set_dropdown_selected_id(
        dialog._macro_loop_mode_combo,
        _LOOP_MODE_OPTIONS,
        "count",
    )
    dialog._macro_loop_count_spin.set_value(5)
    dialog._macro_move_to_start_check.set_active(True)
    dialog._macro_start_x_spin.set_value(99)
    dialog._macro_loop_finish_check.set_active(True)

    dialog._on_undo_all_changes(None)

    assert dialog._events[0].press_t_us == 1000
    assert (
        _get_dropdown_selected_id(
            dialog._macro_loop_mode_combo,
            _LOOP_MODE_OPTIONS,
            "none",
        )
        == "hold"
    )
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


def test_macro_editor_add_move_selects_zeroed_event_for_editing(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    dialog._duration_us = 2_000_000

    dialog._on_mouse_move_selected_for_insert(
        Gtk.Box(),
        MappingAction(action_type=ActionType.MOUSE_MOVE_REL),
        1_000_000,
    )

    rel_move = dialog._synthetic_moves[0]
    assert rel_move.mode == "rel"
    assert rel_move.t_us == 1_000_000
    assert rel_move.x == 0
    assert rel_move.y == 0
    assert dialog._timeline._selected is rel_move
    assert dialog._revealer.get_reveal_child() is True
    assert dialog._move_x_spin.get_value_as_int() == 0
    assert dialog._move_y_spin.get_value_as_int() == 0

    dialog._insert_move_event("abs", default_t_us=250_000)

    abs_move = dialog._synthetic_moves[0]
    assert abs_move.mode == "abs"
    assert abs_move.t_us == 250_000
    assert abs_move.x == 0
    assert abs_move.y == 0
    assert dialog._timeline._selected is abs_move
    assert dialog._move_capture_btn.get_visible() is True


def test_macro_editor_add_key_dialog_starts_on_requested_device_type(monkeypatch) -> None:
    import keymasq.gui.widgets.key_selector.dialog as key_selector_dialog_module

    captured_actions: list[MappingAction] = []
    captured_times: list[int] = []

    class DummyDialog:
        def __init__(self, _parent, _label, current_action=None, **_kwargs):
            captured_actions.append(current_action)

        def connect(self, _signal_name, _callback, default_t_us):
            captured_times.append(default_t_us)

        def present(self, _parent):
            pass

    monkeypatch.setattr(key_selector_dialog_module, "KeySelectorDialog", DummyDialog)

    dialog = _build_macro_dialog(monkeypatch)
    dialog._duration_us = 2_000_000

    dialog._present_add_key_dialog()
    dialog._present_add_key_dialog(default_t_us=250_000, device_type="gamepad")

    assert [action.action_type for action in captured_actions] == [
        ActionType.KEYBOARD,
        ActionType.GAMEPAD,
    ]
    assert captured_times == [1_000_000, 250_000]


def test_macro_editor_inserts_gamepad_axis_action(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    action = MappingAction(
        action_type=ActionType.GAMEPAD_AXIS,
        target="abs_ry",
        axis_value=-12000,
        output_id="virtual-gamepad-3",
    )

    dialog._on_key_selected_for_insert(Gtk.Box(), action, 250_000)

    event = dialog._events[0]
    assert event.device_type == "gamepad"
    assert event.ev_type == evdev.ecodes.EV_ABS
    assert event.code == evdev.ecodes.ABS_RY
    assert event.value == -12000
    assert event.press_t_us == 250_000
    assert event.release_t_us == 250_001
    assert event.output_id == "virtual-gamepad-3"
    assert dialog._timeline._selected is event


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

    assert (
        dialog._shift_timeline_for_gap(
            at_us=1000,
            delta_us=500,
            scope="movement",
            exclude_control=excluded,
        )
        is True
    )
    assert keyboard.press_t_us == 1000
    assert mouse.press_t_us == 1000
    assert gamepad.press_t_us == 1000
    assert move.t_us == 1500
    assert dialog._rel_events[0]["t_us"] == 1500
    assert excluded.t_us == 1000
    assert shifted.t_us == 2500
    assert dialog._passthrough_events[0]["t_us"] == 1000
    assert dialog._passthrough_events[1]["t_us"] == 1500

    assert (
        dialog._shift_timeline_for_gap(
            at_us=1000,
            delta_us=-750,
            scope="keyboard",
            exclude_control=None,
        )
        is True
    )
    assert keyboard.press_t_us == 250
    assert keyboard.release_t_us == 1250
    assert mouse.press_t_us == 1000
    assert dialog._passthrough_events[0]["t_us"] == 250

    assert (
        dialog._shift_timeline_for_gap(
            at_us=1000,
            delta_us=500,
            scope="gamepad",
            exclude_control=None,
        )
        is True
    )
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
    assert (
        timeline._hit_test(
            timeline._time_to_x(keyboard.press_t_us) + 2,
            timeline._kb_y + 12,
        )
        is keyboard
    )
    assert (
        timeline._hit_test(
            timeline._time_to_x(mouse.press_t_us) + 2,
            timeline._m_y + 12,
        )
        is mouse
    )
    assert (
        timeline._hit_test(
            timeline._time_to_x(gamepad.press_t_us) + 2,
            timeline._g_y + 12,
        )
        is gamepad
    )
    assert (
        timeline._hit_test_move(
            timeline._time_to_x(move.t_us),
            timeline._wave_y + 14,
        )
        is move
    )
    assert (
        timeline._hit_test_control(
            timeline._time_to_x(compositor.t_us),
            timeline._wave_y + timeline.TRACK_HEIGHT - 14,
        )
        is compositor
    )


def _build_erase_mode_dialog(monkeypatch):
    dialog = _build_macro_dialog(monkeypatch)
    keyboard = EditableEvent(
        device_type="keyboard",
        ev_type=evdev.ecodes.EV_KEY,
        code=evdev.ecodes.KEY_A,
        press_t_us=50_000,
        release_t_us=150_000,
    )
    later_keyboard = EditableEvent(
        device_type="keyboard",
        ev_type=evdev.ecodes.EV_KEY,
        code=evdev.ecodes.KEY_B,
        press_t_us=300_000,
        release_t_us=400_000,
    )
    mouse = EditableEvent(
        device_type="mouse",
        ev_type=evdev.ecodes.EV_KEY,
        code=evdev.ecodes.BTN_LEFT,
        press_t_us=60_000,
        release_t_us=140_000,
    )
    keyboard_passthrough = {
        "device_type": "keyboard",
        "type": evdev.ecodes.EV_KEY,
        "code": evdev.ecodes.KEY_C,
        "value": 1,
        "t_us": 160_000,
    }
    movement_passthrough = {
        "device_type": "mouse",
        "type": evdev.ecodes.EV_REL,
        "code": evdev.ecodes.REL_Y,
        "value": -3,
        "t_us": 110_000,
    }
    move = EditableMove(mode="rel", t_us=100_000, x=8, y=-6)
    wait = EditableControl(mode="wait", t_us=120_000, duration_us=40_000)
    dialog._events = [keyboard, mouse, later_keyboard]
    dialog._passthrough_events = [keyboard_passthrough, movement_passthrough]
    dialog._synthetic_moves = [move]
    dialog._control_events = [wait]
    dialog._rel_events = [
        {
            "device_type": "mouse",
            "type": evdev.ecodes.EV_REL,
            "code": evdev.ecodes.REL_X,
            "value": 4,
            "t_us": 105_000,
        },
        {
            "device_type": "mouse",
            "type": evdev.ecodes.EV_REL,
            "code": evdev.ecodes.REL_X,
            "value": -2,
            "t_us": 400_000,
        },
    ]
    dialog._duration_us = 450_000
    dialog._timeline._recompute_lanes()
    dialog._erase_btn.set_active(True)
    return dialog, keyboard, later_keyboard, mouse, keyboard_passthrough, movement_passthrough


def test_macro_editor_erase_drag_deletes_band_touched_events_in_start_track(
    monkeypatch,
) -> None:
    (
        dialog,
        keyboard,
        later_keyboard,
        mouse,
        keyboard_passthrough,
        movement_passthrough,
    ) = _build_erase_mode_dialog(monkeypatch)
    assert dialog._erase_mode is True
    timeline = dialog._timeline

    # Band covers only the release edge of `keyboard` plus the raw marker at
    # 160ms; it must delete the whole press/release pair, leave the later
    # keyboard event alone, and never touch the mouse track.
    x_start = timeline._time_to_x(keyboard.release_t_us) - 2.0
    x_end = timeline._time_to_x(170_000)
    timeline._on_drag_begin(None, x_start, timeline._kb_y + 12)
    timeline._on_drag_update(None, x_end - x_start, 0.0)

    assert timeline._erase_track == "keyboard"
    assert keyboard in timeline._erase_pending
    assert keyboard_passthrough in timeline._erase_pending
    assert later_keyboard not in timeline._erase_pending
    assert mouse not in timeline._erase_pending

    timeline._on_drag_end(None, x_end - x_start, 0.0)

    assert keyboard not in dialog._events
    assert later_keyboard in dialog._events
    assert mouse in dialog._events
    assert keyboard_passthrough not in dialog._passthrough_events
    assert movement_passthrough in dialog._passthrough_events
    assert len(dialog._rel_events) == 2
    assert timeline._erase_track is None
    assert timeline._erase_pending == []


def test_macro_editor_erase_drag_on_movement_track_deletes_rel_in_span(monkeypatch) -> None:
    dialog, *_ = _build_erase_mode_dialog(monkeypatch)
    timeline = dialog._timeline

    x_start = timeline._time_to_x(90_000)
    x_end = timeline._time_to_x(170_000)
    timeline._on_drag_begin(None, x_start, timeline._wave_y + 5)
    timeline._on_drag_update(None, x_end - x_start, 0.0)
    timeline._on_drag_end(None, x_end - x_start, 0.0)

    assert dialog._synthetic_moves == []
    assert dialog._control_events == []
    assert all(_passthrough_track(ev) != "movement" for ev in dialog._passthrough_events)
    # Recorded EV_REL movement inside the span is erased; movement outside and
    # the keyboard/mouse lanes stay untouched.
    assert [int(ev["t_us"]) for ev in dialog._rel_events] == [400_000]
    assert len(dialog._events) == 3


def test_macro_editor_erase_drag_on_mouse_lane_deletes_clicks_only(monkeypatch) -> None:
    dialog, keyboard, later_keyboard, mouse, *_ = _build_erase_mode_dialog(monkeypatch)
    timeline = dialog._timeline

    x_start = timeline._time_to_x(mouse.press_t_us) - 2.0
    x_end = timeline._time_to_x(mouse.release_t_us) + 2.0
    timeline._on_drag_begin(None, x_start, timeline._m_y + 12)
    timeline._on_drag_update(None, x_end - x_start, 0.0)
    timeline._on_drag_end(None, x_end - x_start, 0.0)

    assert mouse not in dialog._events
    assert keyboard in dialog._events
    assert later_keyboard in dialog._events
    assert len(dialog._rel_events) == 2


def test_macro_editor_right_drag_ripple_deletes_span_and_collapses(monkeypatch) -> None:
    (
        dialog,
        keyboard,
        later_keyboard,
        mouse,
        keyboard_passthrough,
        movement_passthrough,
    ) = _build_erase_mode_dialog(monkeypatch)
    timeline = dialog._timeline

    x_start = timeline._time_to_x(90_000)
    x_end = timeline._time_to_x(170_000)
    timeline._on_right_drag_begin(None, x_start, timeline._kb_y + 12)
    timeline._on_right_drag_update(None, x_end - x_start, 0.0)

    assert timeline._erase_track == "all"
    assert keyboard in timeline._erase_pending
    assert mouse in timeline._erase_pending
    assert keyboard_passthrough in timeline._erase_pending
    assert movement_passthrough in timeline._erase_pending
    assert later_keyboard not in timeline._erase_pending

    timeline._on_right_drag_end(None, x_end - x_start, 0.0)

    # Everything in the 90-170ms span is gone across all lanes, and the span
    # itself is collapsed: later events are pulled 80ms left.
    assert dialog._events == [later_keyboard]
    assert later_keyboard.press_t_us == 220_000
    assert later_keyboard.release_t_us == 320_000
    assert dialog._passthrough_events == []
    assert dialog._synthetic_moves == []
    assert dialog._control_events == []
    assert [int(ev["t_us"]) for ev in dialog._rel_events] == [320_000]
    assert dialog._duration_us == 320_000
    assert timeline._erase_track is None


def test_macro_editor_erase_band_skips_events_spanning_the_range(monkeypatch) -> None:
    dialog, keyboard, *_ = _build_erase_mode_dialog(monkeypatch)
    timeline = dialog._timeline
    spanning = EditableEvent(
        device_type="keyboard",
        ev_type=evdev.ecodes.EV_KEY,
        code=evdev.ecodes.KEY_LEFTSHIFT,
        press_t_us=10_000,
        release_t_us=440_000,
    )
    dialog._events.append(spanning)
    timeline._recompute_lanes()

    # Band 40-160ms encloses `keyboard` (50-150ms) but is itself enclosed by
    # the held shift (10-440ms): the shift must survive the sweep.
    x_start = timeline._time_to_x(40_000)
    x_end = timeline._time_to_x(160_000)
    timeline._on_drag_begin(None, x_start, timeline._kb_y + 12)
    timeline._on_drag_update(None, x_end - x_start, 0.0)

    assert keyboard in timeline._erase_pending
    assert spanning not in timeline._erase_pending

    timeline._on_drag_end(None, x_end - x_start, 0.0)

    assert keyboard not in dialog._events
    assert spanning in dialog._events
    assert spanning.press_t_us == 10_000
    assert spanning.release_t_us == 440_000


def test_macro_editor_ripple_shortens_spanning_events_instead_of_deleting(
    monkeypatch,
) -> None:
    dialog, keyboard, later_keyboard, *_ = _build_erase_mode_dialog(monkeypatch)
    timeline = dialog._timeline
    spanning = EditableEvent(
        device_type="keyboard",
        ev_type=evdev.ecodes.EV_KEY,
        code=evdev.ecodes.KEY_LEFTSHIFT,
        press_t_us=10_000,
        release_t_us=440_000,
    )
    dialog._events.append(spanning)
    timeline._recompute_lanes()

    x_start = timeline._time_to_x(90_000)
    x_end = timeline._time_to_x(170_000)
    timeline._on_right_drag_begin(None, x_start, timeline._kb_y + 12)
    timeline._on_right_drag_update(None, x_end - x_start, 0.0)

    assert keyboard in timeline._erase_pending
    assert spanning not in timeline._erase_pending

    timeline._on_right_drag_end(None, x_end - x_start, 0.0)

    # The held shift keeps its press and is shortened by the collapsed 80ms;
    # later events shift left as usual.
    assert dialog._events == [spanning, later_keyboard]
    assert spanning.press_t_us == 10_000
    assert spanning.release_t_us == 360_000
    assert later_keyboard.press_t_us == 220_000
    assert later_keyboard.release_t_us == 320_000


def test_macro_editor_erase_mode_right_press_defers_context_menu(monkeypatch) -> None:
    dialog, *_ = _build_erase_mode_dialog(monkeypatch)
    timeline = dialog._timeline

    # The press-time handler must not open the menu while erase mode owns the
    # right button; a plain right-click reopens it from the drag-end fallback.
    timeline._on_right_click(object(), 1, 50.0, timeline._kb_y + 5.0)
    assert timeline._context_menu_x is None

    # Outside erase mode the ripple drag never arms.
    dialog._erase_btn.set_active(False)
    timeline._on_right_drag_begin(None, 40.0, timeline._kb_y + 5.0)
    assert timeline._erase_track is None


def test_macro_editor_erase_mode_click_still_selects_and_never_moves(monkeypatch) -> None:
    dialog, keyboard, *_ = _build_erase_mode_dialog(monkeypatch)
    timeline = dialog._timeline
    dialog._drag_locked = False

    # A press without crossing the drag threshold behaves as a plain click.
    x_press = timeline._time_to_x(keyboard.press_t_us) + 2.0
    timeline._on_drag_begin(None, x_press, timeline._kb_y + 12)
    timeline._on_drag_update(None, 1.0, 0.0)
    timeline._on_drag_end(None, 1.0, 0.0)
    assert timeline._selected is keyboard
    assert keyboard in dialog._events

    # A short drag fully inside the event's rect never moves it (even with
    # move-drag unlocked) and spares it via the spanning rule.
    press_before = keyboard.press_t_us
    timeline._on_drag_begin(None, x_press, timeline._kb_y + 12)
    timeline._on_drag_update(None, 6.0, 0.0)
    assert keyboard.press_t_us == press_before
    timeline._on_drag_end(None, 6.0, 0.0)
    assert keyboard in dialog._events

    # Dragging from inside the event past its release edge erases it.
    offset = timeline._time_to_x(keyboard.release_t_us) + 4.0 - x_press
    timeline._on_drag_begin(None, x_press, timeline._kb_y + 12)
    timeline._on_drag_update(None, offset, 0.0)
    timeline._on_drag_end(None, offset, 0.0)
    assert keyboard not in dialog._events


def test_macro_editor_edge_autoscroll_velocity_ramps_at_edges(monkeypatch) -> None:
    dialog, *_ = _build_erase_mode_dialog(monkeypatch)
    timeline = dialog._timeline
    timeline.get_width = lambda: 800  # pretend a realized 800px viewport

    margin = timeline.EDGE_SCROLL_MARGIN
    assert timeline._edge_autoscroll_velocity(400.0) == 0.0
    assert timeline._edge_autoscroll_velocity(timeline.LABEL_WIDTH + margin + 1.0) == 0.0
    assert timeline._edge_autoscroll_velocity(timeline.LABEL_WIDTH + 4.0) < 0.0
    assert timeline._edge_autoscroll_velocity(800.0 - margin + 4.0) > 0.0
    # Fully at (or past) the edge scrolls at the maximum speed.
    assert timeline._edge_autoscroll_velocity(0.0) == -timeline.EDGE_SCROLL_MAX_SPEED
    assert timeline._edge_autoscroll_velocity(820.0) == timeline.EDGE_SCROLL_MAX_SPEED

    # An unrealized (zero-width) widget never auto-scrolls.
    timeline.get_width = lambda: 0
    assert timeline._edge_autoscroll_velocity(820.0) == 0.0


def test_macro_editor_move_drag_folds_autoscroll_into_position(monkeypatch) -> None:
    dialog, keyboard, *_ = _build_erase_mode_dialog(monkeypatch)
    dialog._erase_btn.set_active(False)
    dialog._drag_locked = False
    timeline = dialog._timeline
    timeline.get_width = lambda: 800

    x_press = timeline._time_to_x(keyboard.press_t_us) + 2.0
    timeline._on_drag_begin(None, x_press, timeline._kb_y + 12)
    timeline._on_drag_update(None, 10.0, 0.0)
    assert timeline._in_drag
    press_after_drag = keyboard.press_t_us
    assert press_after_drag > 50_000

    # Simulate edge auto-scroll advancing the visible slice under a stationary
    # pointer: reapplying the drag must fold the scroll delta into the time.
    # Mirror the implementation's single combined-offset conversion rather
    # than summing separately truncated deltas.
    timeline.set_scroll_offset(timeline._scroll_offset + 20.0)
    timeline._apply_drag_position()
    combined_delta_us = int((10.0 + 20.0) / timeline._pps * 1e6)
    expected_press = 50_000 + combined_delta_us
    assert keyboard.press_t_us == expected_press

    timeline._on_drag_end(None, 10.0, 0.0)
    assert timeline._autoscroll_tick_id == 0
    assert keyboard.press_t_us == expected_press


def test_macro_editor_move_drag_arms_autoscroll_only_at_edges(monkeypatch) -> None:
    dialog, keyboard, *_ = _build_erase_mode_dialog(monkeypatch)
    dialog._erase_btn.set_active(False)
    dialog._drag_locked = False
    timeline = dialog._timeline
    timeline.get_width = lambda: 800

    x_press = timeline._time_to_x(keyboard.press_t_us) + 2.0
    timeline._on_drag_begin(None, x_press, timeline._kb_y + 12)

    # Pointer in the middle of the slice: no auto-scroll.
    timeline._on_drag_update(None, 300.0 - x_press, 0.0)
    assert timeline._autoscroll_tick_id == 0

    # Pointer parked at the right edge: the auto-scroll tick arms.
    timeline._on_drag_update(None, 799.0 - x_press, 0.0)
    assert timeline._autoscroll_tick_id != 0
    assert timeline._autoscroll_velocity > 0.0

    # Back inside the slice: the tick disarms.
    timeline._on_drag_update(None, 300.0 - x_press, 0.0)
    assert timeline._autoscroll_tick_id == 0

    timeline._on_drag_end(None, 300.0 - x_press, 0.0)


def test_macro_editor_erase_band_stays_time_anchored_under_autoscroll(monkeypatch) -> None:
    dialog, keyboard, *_ = _build_erase_mode_dialog(monkeypatch)
    timeline = dialog._timeline
    timeline.get_width = lambda: 800

    x_start = timeline._time_to_x(keyboard.press_t_us) - 4.0
    timeline._on_drag_begin(None, x_start, timeline._kb_y + 12)
    timeline._on_drag_update(None, 20.0, 0.0)
    x0_before = timeline._erase_x0
    x1_before = timeline._erase_x1
    assert x0_before is not None and x1_before is not None

    # Simulate edge auto-scroll shifting the slice: the anchored end follows
    # its time (moves left on screen) while the pointer end stays put.
    timeline.set_scroll_offset(timeline._scroll_offset + 30.0)
    timeline._apply_erase_band()
    assert timeline._erase_x0 == pytest.approx(x0_before - 30.0, abs=0.01)
    assert timeline._erase_x1 == pytest.approx(x1_before, abs=0.01)

    timeline._on_drag_end(None, 20.0, 0.0)
    assert timeline._autoscroll_tick_id == 0


def test_macro_editor_erase_drags_arm_autoscroll_at_edges(monkeypatch) -> None:
    dialog, *_ = _build_erase_mode_dialog(monkeypatch)
    timeline = dialog._timeline
    timeline.get_width = lambda: 800

    # Left-drag erase band parked at the right edge arms the auto-scroll tick.
    timeline._on_drag_begin(None, 100.0, timeline._kb_y + 12)
    timeline._on_drag_update(None, 699.0, 0.0)
    assert timeline._autoscroll_tick_id != 0
    assert timeline._autoscroll_velocity > 0.0
    timeline._on_drag_end(None, 699.0, 0.0)
    assert timeline._autoscroll_tick_id == 0

    # Right-drag ripple band behaves the same.
    timeline._on_right_drag_begin(None, 100.0, timeline._kb_y + 12)
    timeline._on_right_drag_update(None, 699.0, 0.0)
    assert timeline._erase_track == "all"
    assert timeline._autoscroll_tick_id != 0
    assert timeline._autoscroll_velocity > 0.0
    timeline._on_right_drag_end(None, 699.0, 0.0)
    assert timeline._autoscroll_tick_id == 0


def test_macro_editor_erase_band_draws_with_pending_highlights(monkeypatch) -> None:
    import cairo

    dialog, keyboard, *_ = _build_erase_mode_dialog(monkeypatch)
    timeline = dialog._timeline

    x_start = timeline._time_to_x(keyboard.press_t_us) - 4.0
    x_end = timeline._time_to_x(keyboard.release_t_us) + 4.0
    timeline._on_drag_begin(None, x_start, timeline._kb_y + 12)
    timeline._on_drag_update(None, x_end - x_start, 0.0)

    state = timeline._build_render_state()
    assert state._erase_band is not None
    assert state._erase_band[0] == "keyboard"
    assert id(keyboard) in state._erase_pending_ids

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1200, 520)
    context = cairo.Context(surface)
    timeline._draw(None, context, 1200, 520, None)

    timeline._on_drag_end(None, x_end - x_start, 0.0)


def test_macro_editor_erase_toggle_updates_mode_and_styling(monkeypatch) -> None:
    dialog = _build_macro_dialog(monkeypatch)
    assert dialog._erase_mode is False

    dialog._erase_btn.set_active(True)
    assert dialog._erase_mode is True
    assert dialog._erase_btn.has_css_class("destructive-action")

    dialog._erase_btn.set_active(False)
    assert dialog._erase_mode is False
    assert not dialog._erase_btn.has_css_class("destructive-action")
