"""Bulk edits preserve input lifecycles, routing, and timing across macros."""

import json

import evdev
import pytest

from keymasq.gui.widgets.macro_editor import selection
from keymasq.gui.widgets.macro_editor.model import (
    EditableControl,
    EditableEvent,
    EditableMove,
    parse_events,
    reconstruct_events,
)
from keymasq.gui.widgets.macro_editor.timing_ops import TimelineLists


def key(start: int, end: int, code: int = evdev.ecodes.KEY_A) -> EditableEvent:
    return EditableEvent("keyboard", evdev.ecodes.EV_KEY, code, start, end)


def empty() -> TimelineLists:
    return [], [], [], [], []


def test_time_range_expands_overlapping_holds_without_adjacent_actions() -> None:
    before, held, nested, chained, after = (
        key(0, 100),
        key(100, 500),
        key(150, 200),
        key(400, 600),
        key(600, 700),
    )
    raw = {"t_us": 300, "value": 4}
    candidates: list[selection.Item] = [after, nested, before, raw, chained, held]
    selected, span = selection.select_time_range(candidates, 450, 460)
    assert span == (100, 600)
    assert selected == [nested, raw, chained, held]
    assert selection.select_time_range(candidates, 460, 450) == (selected, span)
    assert selection.select_time_range(candidates, 150, 150) == ([], (150, 150))


def test_time_range_copy_preserves_padding_and_action_copy_trims_it() -> None:
    lists = empty()
    tap = key(1_200_000, 1_250_000)
    lists[0].append(tap)
    fragment = selection.Fragment.capture(lists, [tap], time_range=(1_000_000, 2_000_000))
    decoded = selection.Fragment.from_clipboard(json.dumps(fragment.clipboard_payload()).encode())
    assert decoded.preserve_range
    assert decoded.duration_us == 1_000_000
    assert [event["t_us"] for event in decoded.events] == [200_000, 250_000]
    pasted = decoded.paste(empty(), 3_000_000)
    assert selection.bounds(pasted) == (3_200_000, 3_250_000)
    actions = selection.Fragment.capture(lists, [tap])
    assert not actions.preserve_range
    assert actions.duration_us == 50_000
    assert [event["t_us"] for event in actions.events] == [0, 50_000]


def test_copying_only_silence_can_insert_a_pause() -> None:
    fragment = selection.Fragment.capture(empty(), [], time_range=(50, 150))
    decoded = selection.Fragment.from_clipboard(json.dumps(fragment.clipboard_payload()).encode())
    assert decoded.preserve_range
    assert decoded.events == []
    assert decoded.duration_us == 100
    lists = empty()
    lists[0].extend([key(0, 100), key(200, 300)])
    assert decoded.paste(lists, 50, insert=True) == []
    assert [(event.press_t_us, event.release_t_us) for event in lists[0]] == [(0, 200), (300, 400)]


def test_move_clamps_whole_group_and_keeps_holds_overlaps_and_raw_events() -> None:
    held = key(100, 900)
    tap = key(300, 400)
    raw = {"t_us": 350, "value": 4}
    wait = EditableControl("wait", 500, duration_us=1234)
    selected: list[selection.Item] = [held, tap, raw, wait]
    assert selection.move(selected, -500) == -100
    assert (held.press_t_us, held.release_t_us) == (0, 800)
    assert (tap.press_t_us, tap.release_t_us) == (200, 300)
    assert raw["t_us"] == 250
    assert (wait.t_us, wait.duration_us) == (400, 1234)


def test_copy_mixed_fragment_roundtrips_routing_controls_and_order() -> None:
    raw = [
        {
            "t_us": 100,
            "device_type": "gamepad",
            "type": 1,
            "code": 304,
            "value": 1,
            "output_id": "pad-two",
        },
        {"t_us": 150, "device_type": "mouse", "type": 2, "code": 0, "value": 4},
        {
            "t_us": 200,
            "device_type": "macro",
            "type": 0,
            "code": 0,
            "value": 0,
            "macro_action": "wait_random",
            "min_us": 50,
            "max_us": 100,
        },
        {
            "t_us": 300,
            "device_type": "gamepad",
            "type": 1,
            "code": 304,
            "value": 0,
            "output_id": "pad-two",
        },
        {"t_us": 300, "device_type": "keyboard", "type": 1, "code": 30, "value": 1},
        {"t_us": 350, "device_type": "keyboard", "type": 1, "code": 30, "value": 0},
    ]
    source = parse_events(raw)
    fragment = selection.Fragment.capture(source, selection.items(source))
    target = empty()
    added = fragment.paste(target, 1000)
    result = reconstruct_events(*target)
    expected = [{**event, "t_us": int(event["t_us"]) + 900} for event in raw]
    assert [(e["t_us"], e.get("output_id"), e["value"]) for e in result] == [
        (e["t_us"], e.get("output_id"), e["value"]) for e in expected
    ]
    assert target[4][0].min_us == 50
    assert target[4][0].max_us == 100
    assert len(added) == 4
    # The clipboard owns its data independently of both documents.
    target[0][0].code = 999
    assert reconstruct_events(*source) == raw
    second = empty()
    fragment.paste(second, 0)
    assert second[0][0].code == 304


def test_paste_preserves_source_tie_order_after_destination_release() -> None:
    target = parse_events(
        [
            {"t_us": 0, "device_type": "keyboard", "type": 1, "code": 30, "value": 1},
            {"t_us": 100, "device_type": "keyboard", "type": 1, "code": 30, "value": 0},
        ]
    )
    source: TimelineLists = ([key(0, 50)], [], [], [], [])
    fragment = selection.Fragment.capture(source, selection.items(source))
    fragment.paste(target, 100)
    at_boundary = [e["value"] for e in reconstruct_events(*target) if e["t_us"] == 100]
    assert at_boundary == [0, 1]


def test_insert_makes_room_and_extends_a_held_modifier() -> None:
    held = key(0, 1000, evdev.ecodes.KEY_LEFTCTRL)
    later = key(800, 900)
    target: TimelineLists = ([held, later], [], [], [], [])
    source: TimelineLists = ([key(100, 300)], [], [], [], [])
    fragment = selection.Fragment.capture(source, selection.items(source))
    fragment.paste(target, 500, insert=True)
    assert (held.press_t_us, held.release_t_us) == (0, 1200)
    assert (later.press_t_us, later.release_t_us) == (1000, 1100)
    assert (target[0][-1].press_t_us, target[0][-1].release_t_us) == (500, 700)


@pytest.mark.parametrize("recorded", [False, True])
@pytest.mark.parametrize("at_us", [0, 100_000])
def test_insert_preserves_key_cycles_at_both_fragment_boundaries(recorded, at_us) -> None:
    target: TimelineLists = ([key(0, 100_000), key(100_000, 200_000)], [], [], [], [])
    if recorded:
        target = parse_events(reconstruct_events(*target))
    source: TimelineLists = ([key(0, 100_000)], [], [], [], [])
    fragment = selection.Fragment.capture(source, selection.items(source))
    fragment.paste(target, at_us, insert=True)
    raw = reconstruct_events(*target)
    assert [(event["t_us"], event["value"]) for event in raw] == [
        (0, 1),
        (100_000, 0),
        (100_000, 1),
        (200_000, 0),
        (200_000, 1),
        (300_000, 0),
    ]
    reloaded = parse_events(raw)
    assert [(event.press_t_us, event.release_t_us) for event in reloaded[0]] == [
        (0, 100_000),
        (100_000, 200_000),
        (200_000, 300_000),
    ]


def test_ordinary_paste_leaves_other_items_fixed() -> None:
    existing = key(0, 1000)
    target: TimelineLists = ([existing], [], [], [], [])
    source: TimelineLists = ([key(100, 300)], [], [], [], [])
    selection.Fragment.capture(source, selection.items(source)).paste(target, 500)
    assert (existing.press_t_us, existing.release_t_us) == (0, 1000)


def test_insert_keeps_mixed_destination_order_and_spanning_hold() -> None:
    target: TimelineLists = (
        [
            key(0, 200, evdev.ecodes.KEY_LEFTCTRL),
            key(0, 100),
            key(100, 200),
            EditableEvent("gamepad", evdev.ecodes.EV_ABS, 0, 100, 100, value=42),
        ],
        [{"t_us": 100, "device_type": "mouse", "type": 2, "code": 0, "value": 4}],
        [{"t_us": 100, "device_type": "keyboard", "type": 4, "code": 4, "value": 30}],
        [EditableMove("rel", 100, x=5, y=10)],
        [EditableControl("wait", 100, duration_us=500)],
    )
    original = reconstruct_events(*target)
    source: TimelineLists = ([key(0, 100)], [], [], [], [])
    fragment = selection.Fragment.capture(source, selection.items(source))
    prefix = [
        event
        for event in original
        if event["t_us"] < 100
        or (event["t_us"] == 100 and event["type"] == 1 and event["value"] == 0)
    ]
    suffix = [{**event, "t_us": event["t_us"] + 100} for event in original if event not in prefix]
    inserted = [{**event, "t_us": event["t_us"] + 100} for event in fragment.events]
    expected = sorted([*prefix, *inserted, *suffix], key=lambda event: event["t_us"])
    fragment.paste(target, 100, insert=True)
    assert reconstruct_events(*target) == expected
    assert reconstruct_events(*parse_events(expected)) == expected
    assert (target[0][0].press_t_us, target[0][0].release_t_us) == (0, 300)


def test_pause_edit_preserves_overlaps_and_hold_durations() -> None:
    held = key(100, 500, evdev.ecodes.KEY_LEFTCTRL)
    first = key(200, 250)
    second = key(400, 450)
    later = key(800, 900)
    selection.set_pauses([held, first, second, later], 50)
    assert [(e.press_t_us, e.release_t_us) for e in [held, first, second, later]] == [
        (100, 500),
        (200, 250),
        (400, 450),
        (550, 650),
    ]


def test_scale_anchors_selection_and_wait_durations_are_explicit() -> None:
    first = key(100, 200)
    wait = EditableControl("wait", 300, duration_us=1000)
    random = EditableControl("wait_random", 500, min_us=200, max_us=800)
    move = EditableMove("rel", 600, 5, 10)
    selected: list[selection.Item] = [first, wait, random, move]
    selection.scale(selected, 0.5)
    assert (first.press_t_us, first.release_t_us, wait.t_us, move.t_us) == (100, 150, 200, 350)
    assert (wait.duration_us, random.min_us, random.max_us) == (1000, 200, 800)
    selection.scale(selected, 0.5, scale_waits=True)
    assert (wait.duration_us, random.min_us, random.max_us) == (500, 100, 400)


def test_clipboard_preserves_macro_calls() -> None:
    call = EditableControl("macro_sync", 100, macro_name="child", macro_loop_count=3)
    source: TimelineLists = ([key(150, 200)], [], [], [], [call])
    payload = selection.Fragment.capture(source, selection.items(source)).clipboard_payload()
    assert payload["duration_us"] == 100
    assert payload["events"][0]["macro_name"] == "child"
    assert "move_to_start" not in payload


def test_undo_redo_independence_branching_and_limit() -> None:
    history = selection.EditHistory(limit=2)
    for number in range(4):
        history.record({"events": [{"value": number}]})
    assert len(history.past) == 2
    undone = history.undo()
    assert undone == {"events": [{"value": 2}]}
    assert undone is not None
    undone["events"].clear()
    assert history.current == {"events": [{"value": 2}]}
    assert history.redo() == {"events": [{"value": 3}]}
    history.undo()
    history.record({"events": []})
    assert history.redo() is None


def test_pause_edit_moves_a_recorded_trajectory_without_resampling() -> None:
    before = key(0, 100)
    first = {"t_us": 500, "type": 2, "code": 0, "value": 3}
    last = {"t_us": 510, "type": 2, "code": 1, "value": -2}
    after = key(1000, 1100)
    selection.set_pauses([before, first, last, after], 50)
    assert (first["t_us"], last["t_us"]) == (150, 160)
    assert (after.press_t_us, after.release_t_us) == (210, 310)
    assert (first["value"], last["value"]) == (3, -2)


def test_pasting_a_manually_created_key_releases_before_pressing_again() -> None:
    source: TimelineLists = ([key(0, 100)], [], [], [], [])
    fragment = selection.Fragment.capture(source, selection.items(source))
    fragment.paste(source, 100)
    fragment.paste(source, 200)
    assert [(event["t_us"], event["value"]) for event in reconstruct_events(*source)] == [
        (0, 1),
        (100, 0),
        (100, 1),
        (200, 0),
        (200, 1),
        (300, 0),
    ]


def test_native_clipboard_fragment_roundtrip_preserves_mouse_actions() -> None:
    import json

    source: TimelineLists = ([EditableEvent("mouse", 1, 272, 50, 100)], [], [], [], [])
    fragment = selection.Fragment.capture(source, selection.items(source))
    decoded = selection.Fragment.from_clipboard(json.dumps(fragment.clipboard_payload()).encode())
    assert decoded == fragment


def test_native_clipboard_rejects_invalid_events_before_insertion() -> None:
    import json

    import pytest

    for payload in [
        None,
        {},
        {"events": [], "duration_us": 0},
        {"events": [{"t_us": "bad"}], "duration_us": 10},
    ]:
        with pytest.raises(ValueError):
            selection.Fragment.from_clipboard(json.dumps(payload).encode())
