import json

import evdev
import pytest

from keymasq.common.macro_compile import (
    build_compact_macro_events,
    build_type_macro_events,
    macro_definition_from_events,
    parse_macro_json,
)


def _key_values(events: list[dict[str, object]], code: int) -> list[int]:
    return [
        int(event["value"])
        for event in events
        if event.get("type") == evdev.ecodes.EV_KEY and event.get("code") == code
    ]


def test_compact_key_token_emits_tap() -> None:
    events = build_compact_macro_events(["key_a"])

    assert _key_values(events, evdev.ecodes.KEY_A) == [1, 0]
    assert events[0]["device_type"] == "keyboard"


def test_compact_explicit_hold_release_and_waits() -> None:
    events = build_compact_macro_events(["key_leftctrl:1", "wait:20", "key_c", "key_leftctrl:0"])

    assert _key_values(events, evdev.ecodes.KEY_LEFTCTRL) == [1, 0]
    assert _key_values(events, evdev.ecodes.KEY_C) == [1, 0]
    wait = next(event for event in events if event.get("macro_action") == "wait")
    assert wait["duration_us"] == 20_000


def test_compact_random_wait() -> None:
    events = build_compact_macro_events(["wait:10:20"])

    assert events == [
        {
            "device_type": "macro",
            "type": 0,
            "code": 0,
            "value": 0,
            "t_us": 0,
            "macro_action": "wait_random",
            "min_us": 10_000,
            "max_us": 20_000,
        }
    ]


def test_compact_move_events_use_macro_action_shape() -> None:
    events = build_compact_macro_events(["move_abs:100:200", "move_rel:-5:2"])

    assert events == [
        {
            "device_type": "macro",
            "type": 0,
            "code": 0,
            "value": 0,
            "t_us": 0,
            "macro_action": "mouse_move_abs",
            "x": 100,
            "y": 200,
        },
        {
            "device_type": "macro",
            "type": 0,
            "code": 0,
            "value": 0,
            "t_us": 1,
            "macro_action": "mouse_move_rel",
            "x": -5,
            "y": 2,
        },
    ]


def test_compact_releases_held_keys_at_end_in_reverse_order() -> None:
    events = build_compact_macro_events(["key_leftctrl:1", "key_leftshift:1"])

    assert events[-2]["code"] == evdev.ecodes.KEY_LEFTSHIFT
    assert events[-2]["value"] == 0
    assert events[-1]["code"] == evdev.ecodes.KEY_LEFTCTRL
    assert events[-1]["value"] == 0


def test_compact_rejects_release_without_press() -> None:
    with pytest.raises(ValueError, match="release without matching press"):
        build_compact_macro_events(["key_a:0"])


def test_compact_rejects_duplicate_down() -> None:
    with pytest.raises(ValueError, match="already held"):
        build_compact_macro_events(["key_a:1", "key_a:down"])


def test_type_macro_builder_normalizes_common_pasted_text() -> None:
    events = build_type_macro_events("A\u00a0\u201cHi\u201d\u2026\r\nx\u2014y", 10, 0)

    press_codes = [
        event["code"]
        for event in events
        if event["type"] == evdev.ecodes.EV_KEY and event["value"] == 1
    ]
    assert evdev.ecodes.KEY_SPACE in press_codes
    assert evdev.ecodes.KEY_APOSTROPHE in press_codes
    assert press_codes.count(evdev.ecodes.KEY_DOT) == 3
    assert press_codes.count(evdev.ecodes.KEY_ENTER) == 1
    assert evdev.ecodes.KEY_MINUS in press_codes


def test_type_macro_builder_allows_zero_key_down_and_pause() -> None:
    events = build_type_macro_events("ab", 0, 0)

    assert [
        (event["code"], event["value"], event["t_us"])
        for event in events
        if event["type"] == evdev.ecodes.EV_KEY
    ] == [
        (evdev.ecodes.KEY_A, 1, 0),
        (evdev.ecodes.KEY_A, 0, 0),
        (evdev.ecodes.KEY_B, 1, 0),
        (evdev.ecodes.KEY_B, 0, 0),
    ]


def test_type_macro_builder_expands_enter_and_tab_controls() -> None:
    events = build_type_macro_events("a<enter>b<tab>c", 10, 0)

    press_codes = [
        event["code"]
        for event in events
        if event["type"] == evdev.ecodes.EV_KEY and event["value"] == 1
    ]
    assert press_codes == [
        evdev.ecodes.KEY_A,
        evdev.ecodes.KEY_ENTER,
        evdev.ecodes.KEY_B,
        evdev.ecodes.KEY_TAB,
        evdev.ecodes.KEY_C,
    ]


def test_type_macro_builder_adds_fixed_and_random_wait_controls() -> None:
    events = build_type_macro_events("a<wait:10>b<wait:20:30>c", 10, 0)

    waits = [event for event in events if event.get("macro_action")]
    assert waits == [
        {
            "device_type": "macro",
            "type": 0,
            "code": 0,
            "value": 0,
            "t_us": 10_000,
            "macro_action": "wait",
            "duration_us": 10_000,
        },
        {
            "device_type": "macro",
            "type": 0,
            "code": 0,
            "value": 0,
            "t_us": 20_000,
            "macro_action": "wait_random",
            "min_us": 20_000,
            "max_us": 30_000,
        },
    ]


def test_macro_definition_duration_uses_wait_timestamp_not_wait_end() -> None:
    macro = macro_definition_from_events(
        [
            {
                "device_type": "macro",
                "type": 0,
                "code": 0,
                "value": 0,
                "t_us": 10_000,
                "macro_action": "wait",
                "duration_us": 5_000_000,
            }
        ]
    )

    assert macro["duration_us"] == 10_000


def test_type_macro_builder_keeps_backslash_sequences_literal() -> None:
    events = build_type_macro_events(r"a\nb", 10, 0)

    press_codes = [
        event["code"]
        for event in events
        if event["type"] == evdev.ecodes.EV_KEY and event["value"] == 1
    ]
    assert press_codes == [
        evdev.ecodes.KEY_A,
        evdev.ecodes.KEY_BACKSLASH,
        evdev.ecodes.KEY_N,
        evdev.ecodes.KEY_B,
    ]


def test_type_macro_builder_escapes_literal_less_than_before_control() -> None:
    events = build_type_macro_events(r"a\<tab>b\\<tab>c", 10, 0)

    press_codes = [
        event["code"]
        for event in events
        if event["type"] == evdev.ecodes.EV_KEY and event["value"] == 1
    ]
    assert press_codes == [
        evdev.ecodes.KEY_A,
        evdev.ecodes.KEY_LEFTSHIFT,
        evdev.ecodes.KEY_COMMA,
        evdev.ecodes.KEY_T,
        evdev.ecodes.KEY_A,
        evdev.ecodes.KEY_B,
        evdev.ecodes.KEY_LEFTSHIFT,
        evdev.ecodes.KEY_DOT,
        evdev.ecodes.KEY_B,
        evdev.ecodes.KEY_BACKSLASH,
        evdev.ecodes.KEY_LEFTSHIFT,
        evdev.ecodes.KEY_COMMA,
        evdev.ecodes.KEY_T,
        evdev.ecodes.KEY_A,
        evdev.ecodes.KEY_B,
        evdev.ecodes.KEY_LEFTSHIFT,
        evdev.ecodes.KEY_DOT,
        evdev.ecodes.KEY_C,
    ]


def test_type_macro_builder_rejects_invalid_wait_control() -> None:
    with pytest.raises(ValueError, match="wait duration must be an integer"):
        build_type_macro_events("a<wait:soon>b", 10, 0)


def test_type_macro_builder_reports_unsupported_character_position() -> None:
    with pytest.raises(ValueError, match=r"position 2: 'é'"):
        build_type_macro_events("aé", 10, 0)


def test_parse_macro_json_accepts_event_list_and_macro_object() -> None:
    events_json = json.dumps([{"t_us": 0}])
    macro_json = json.dumps({"name": "demo", "events": [{"t_us": 1}]})

    assert parse_macro_json(events_json)["events"] == [{"t_us": 0}]
    assert parse_macro_json(macro_json)["name"] == "demo"
