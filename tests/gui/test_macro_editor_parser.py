# ruff: noqa: F403, F405, I001
from tests.gui.macro_editor_dialog_support import *

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
