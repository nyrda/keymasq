# ruff: noqa: E402, I001

import evdev
import pytest

pytest.importorskip("gi")

from keymasq.gui.widgets.macro_editor import timing_ops
from keymasq.gui.widgets.macro_editor.model import (
    EditableControl,
    EditableEvent,
    EditableMove,
    _describe_passthrough_event,
    _passthrough_track,
    parse_events,
    reconstruct_events,
)


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

    assert len(editable) == 2
    axis_event = editable[0]
    assert axis_event.device_type == "gamepad"
    assert axis_event.ev_type == evdev.ecodes.EV_ABS
    assert axis_event.code == evdev.ecodes.ABS_X
    assert axis_event.value == 123
    assert axis_event.press_t_us == 10
    assert axis_event.release_t_us == 11
    assert rebuilt == raw


def test_describe_passthrough_abs_event_uses_event_type_name() -> None:
    title, detail = _describe_passthrough_event(
        {
            "device_type": "gamepad",
            "type": evdev.ecodes.EV_ABS,
            "code": evdev.ecodes.ABS_X,
            "value": 123,
            "t_us": 10,
        }
    )

    assert title == "EV_ABS"
    assert "Raw gamepad EV_ABS ABS_X value 123 (code 0)" == detail
    assert "SYN_" not in title
    assert "SYN_" not in detail


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


def test_parse_reconstruct_macro_move_actions_separate_from_waveform() -> None:
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
            "device_type": "macro",
            "type": 0,
            "code": 0,
            "value": 0,
            "t_us": 200,
            "macro_action": "mouse_move_rel",
            "x": 300,
            "y": 200,
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
    move_actions = [e for e in rebuilt if e.get("macro_action") == "mouse_move_rel"]
    assert move_actions == [
        {
            "device_type": "macro",
            "type": 0,
            "code": 0,
            "value": 0,
            "t_us": 200,
            "macro_action": "mouse_move_rel",
            "x": 300,
            "y": 200,
        }
    ]


def test_parse_reconstruct_natural_mouse_move_action() -> None:
    raw = [
        {
            "device_type": "macro",
            "type": 0,
            "code": 0,
            "value": 0,
            "t_us": 200,
            "macro_action": "mouse_move_natural_abs",
            "x": 300,
            "y": 200,
            "speed": 1500.0,
            "jitter": 0.5,
            "curve": "natural",
            "tolerance": 3,
            "max_duration_ms": 2500,
            "stop_on_failure": True,
        },
    ]

    editable, rel_events, passthrough, synthetic_moves, control_events = parse_events(raw)
    assert editable == []
    assert rel_events == []
    assert passthrough == []
    assert control_events == []
    assert len(synthetic_moves) == 1
    move = synthetic_moves[0]
    assert move.mode == "natural"
    assert move.x == 300
    assert move.y == 200
    assert move.speed == 1500.0
    assert move.jitter == 0.5
    assert move.curve == "natural"
    assert move.tolerance == 3
    assert move.max_duration_ms == 2500
    assert move.stop_on_failure is True

    rebuilt = reconstruct_events(editable, rel_events, passthrough, synthetic_moves, control_events)
    assert rebuilt == raw


def test_parse_reconstruct_preserves_wait_controls() -> None:
    raw = [
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

    editable, rel_events, passthrough, synthetic_moves, control_events = parse_events(raw)
    rebuilt = reconstruct_events(editable, rel_events, passthrough, synthetic_moves, control_events)

    assert editable == []
    assert rel_events == []
    assert passthrough == []
    assert synthetic_moves == []
    assert rebuilt == raw


def test_parse_reconstruct_preserves_equal_timestamp_control_before_key_events() -> None:
    raw = [
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
            "t_us": 1000,
        },
    ]

    editable, rel_events, passthrough, synthetic_moves, control_events = parse_events(raw)
    rebuilt = reconstruct_events(editable, rel_events, passthrough, synthetic_moves, control_events)

    assert rebuilt == raw


def test_parse_reconstruct_preserves_unknown_macro_action_payload() -> None:
    raw = [
        {
            "macro_action": "custom_action",
            "t_us": 1000,
            "foo": "bar",
        }
    ]

    editable, rel_events, passthrough, synthetic_moves, control_events = parse_events(raw)
    rebuilt = reconstruct_events(editable, rel_events, passthrough, synthetic_moves, control_events)

    assert editable == []
    assert rel_events == []
    assert passthrough == raw
    assert synthetic_moves == []
    assert control_events == []
    assert rebuilt == raw


def test_parse_reconstruct_compositor_macro_action() -> None:
    raw = [
        {
            "device_type": "macro",
            "type": 0,
            "code": 0,
            "value": 0,
            "t_us": 123000,
            "macro_action": "compositor_dispatch",
            "compositor": "hyprland",
            "dispatcher": "workspace",
            "args": "e+1",
        }
    ]

    editable, rel_events, passthrough, synthetic_moves, control_events = parse_events(raw)
    rebuilt = reconstruct_events(editable, rel_events, passthrough, synthetic_moves, control_events)

    assert editable == []
    assert rel_events == []
    assert passthrough == []
    assert synthetic_moves == []
    assert len(control_events) == 1
    control = control_events[0]
    assert control.mode == "compositor_dispatch"
    assert control.compositor_id == "hyprland"
    assert control.compositor_dispatcher == "workspace"
    assert control.compositor_args == "e+1"
    assert rebuilt == raw


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


def test_timing_gap_limit_mapping_and_apply_updates_all_event_kinds() -> None:
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
    rel = {
        "device_type": "mouse",
        "type": evdev.ecodes.EV_REL,
        "code": evdev.ecodes.REL_X,
        "value": 4,
        "t_us": 4000,
    }
    passthrough = {
        "device_type": "keyboard",
        "type": evdev.ecodes.EV_KEY,
        "code": evdev.ecodes.KEY_B,
        "value": 2,
        "t_us": 6000,
    }
    move = EditableMove(mode="abs", t_us=7000, x=11, y=22)
    control = EditableControl(mode="wait", t_us=8000, duration_us=2000)
    events = [mouse, keyboard]
    rel_events = [rel]
    passthrough_events = [passthrough]
    synthetic_moves = [move]
    control_events = [control]

    mapping = timing_ops.build_time_mapping_with_gap_limits(
        events,
        rel_events,
        passthrough_events,
        synthetic_moves,
        control_events,
        scale=2.0,
        min_gap_us=500,
        max_gap_us=1500,
    )
    timing_ops.apply_time_map(
        events,
        rel_events,
        passthrough_events,
        synthetic_moves,
        control_events,
        mapping,
    )

    assert events == [keyboard, mouse]
    assert keyboard.press_t_us == 1000
    assert keyboard.release_t_us == 2500
    assert rel["t_us"] == 4000
    assert mouse.press_t_us == 5500
    assert passthrough["t_us"] == 7000
    assert move.t_us == 8500
    assert control.t_us == 10500
    assert (
        timing_ops.compute_duration_us(
            events,
            rel_events,
            passthrough_events,
            synthetic_moves,
            control_events,
        )
        == 12000
    )


def test_timing_trim_startpoint_removes_and_rebases_timeline() -> None:
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
    rel = {
        "device_type": "mouse",
        "type": evdev.ecodes.EV_REL,
        "code": evdev.ecodes.REL_X,
        "value": 3,
        "t_us": 6500,
    }
    passthrough = {
        "device_type": "mouse",
        "type": evdev.ecodes.EV_KEY,
        "code": evdev.ecodes.BTN_RIGHT,
        "value": 2,
        "t_us": 7500,
    }

    events, rel_events, passthrough_events, synthetic_moves, control_events = (
        timing_ops.trim_startpoint(
            [removed, kept],
            [rel],
            [passthrough],
            [move],
            [control],
            4000,
        )
    )

    assert events == [kept]
    assert rel_events == [rel]
    assert passthrough_events == [passthrough]
    assert synthetic_moves == [move]
    assert control_events == [control]
    assert kept.press_t_us == 1000
    assert kept.release_t_us == 5000
    assert rel["t_us"] == 2500
    assert passthrough["t_us"] == 3500
    assert move.t_us == 2000
    assert control.t_us == 3000


def test_timing_shift_timeline_for_gap_respects_scopes() -> None:
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
    rel_events = [
        {
            "device_type": "mouse",
            "type": evdev.ecodes.EV_REL,
            "code": evdev.ecodes.REL_Y,
            "value": -1,
            "t_us": 1000,
        }
    ]
    passthrough_events = [
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
        timing_ops.shift_timeline_for_gap(
            [keyboard, mouse, gamepad],
            rel_events,
            passthrough_events,
            [move],
            [excluded, shifted],
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
    assert rel_events[0]["t_us"] == 1500
    assert excluded.t_us == 1000
    assert shifted.t_us == 2500
    assert passthrough_events[0]["t_us"] == 1000
    assert passthrough_events[1]["t_us"] == 1500


def test_parse_discards_ev_syn_events() -> None:
    raw = [
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_KEY,
            "code": evdev.ecodes.KEY_A,
            "value": 1,
            "t_us": 10,
        },
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_SYN,
            "code": evdev.ecodes.SYN_REPORT,
            "value": 0,
            "t_us": 15,
        },
        {
            "device_type": "keyboard",
            "type": evdev.ecodes.EV_KEY,
            "code": evdev.ecodes.KEY_A,
            "value": 0,
            "t_us": 20,
        },
    ]

    editable, rel_events, passthrough, synthetic_moves, control_events = parse_events(raw)

    assert len(editable) == 1
    assert passthrough == []

    rebuilt = reconstruct_events(editable, rel_events, passthrough, synthetic_moves, control_events)
    assert all(int(ev["type"]) != evdev.ecodes.EV_SYN for ev in rebuilt)


def test_timing_map_time_keeps_out_of_range_timestamps_relative() -> None:
    # 2x stretch between anchors 1000 and 2000.
    mapping = {1000: 1000, 2000: 3000}

    # Inside the region: interpolated.
    assert timing_ops.map_time(mapping, 1500) == 2000
    # Before the region: unchanged, not snapped onto the first anchor.
    assert timing_ops.map_time(mapping, 400) == 400
    # After the region: moves rigidly with the region's end instead of
    # collapsing onto the last anchor.
    assert timing_ops.map_time(mapping, 2600) == 3600
    # Empty mapping leaves timestamps alone.
    assert timing_ops.map_time({}, 123) == 123


def test_timing_scale_excluding_passthrough_keeps_outlier_offsets() -> None:
    keyboard = EditableEvent(
        device_type="keyboard",
        ev_type=evdev.ecodes.EV_KEY,
        code=evdev.ecodes.KEY_A,
        press_t_us=1000,
        release_t_us=2000,
    )
    passthrough_before = {
        "device_type": "keyboard",
        "type": evdev.ecodes.EV_KEY,
        "code": evdev.ecodes.KEY_B,
        "value": 1,
        "t_us": 500,
    }
    passthrough_after = {
        "device_type": "keyboard",
        "type": evdev.ecodes.EV_KEY,
        "code": evdev.ecodes.KEY_C,
        "value": 0,
        "t_us": 2500,
    }
    events = [keyboard]
    passthrough_events = [passthrough_before, passthrough_after]

    mapping = timing_ops.build_time_mapping_with_gap_limits(
        events,
        [],
        passthrough_events,
        [],
        [],
        scale=2.0,
        include_passthrough=False,
    )
    timing_ops.apply_time_map(events, [], passthrough_events, [], [], mapping)

    assert keyboard.press_t_us == 1000
    assert keyboard.release_t_us == 3000
    # Passthrough timestamps outside the anchor range keep their offsets
    # relative to the remapped content instead of snapping onto its edges.
    assert passthrough_before["t_us"] == 500
    assert passthrough_after["t_us"] == 3500
