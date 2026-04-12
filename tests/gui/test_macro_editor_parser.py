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


