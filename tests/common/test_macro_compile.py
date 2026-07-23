import json

import evdev
import pytest

from keymasq.common.macro_compile import (
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


def _press_codes(events: list[dict[str, object]]) -> list[int]:
    return [
        int(event["code"])
        for event in events
        if event.get("type") == evdev.ecodes.EV_KEY and event.get("value") == 1
    ]


def test_type_macro_builder_normalizes_common_pasted_text() -> None:
    events = build_type_macro_events("A\u00a0\u201cHi\u201d\u2026\r\nx\u2014y", 10, 0)

    press_codes = _press_codes(events)
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

    press_codes = _press_codes(events)
    assert press_codes == [
        evdev.ecodes.KEY_A,
        evdev.ecodes.KEY_ENTER,
        evdev.ecodes.KEY_B,
        evdev.ecodes.KEY_TAB,
        evdev.ecodes.KEY_C,
    ]


def test_type_macro_builder_expands_named_key_controls() -> None:
    events = build_type_macro_events(
        "<space><esc><backspace><delete><up><down><left><right><home><end><pageup><pagedown>",
        10,
        0,
    )

    assert _press_codes(events) == [
        evdev.ecodes.KEY_SPACE,
        evdev.ecodes.KEY_ESC,
        evdev.ecodes.KEY_BACKSPACE,
        evdev.ecodes.KEY_DELETE,
        evdev.ecodes.KEY_UP,
        evdev.ecodes.KEY_DOWN,
        evdev.ecodes.KEY_LEFT,
        evdev.ecodes.KEY_RIGHT,
        evdev.ecodes.KEY_HOME,
        evdev.ecodes.KEY_END,
        evdev.ecodes.KEY_PAGEUP,
        evdev.ecodes.KEY_PAGEDOWN,
    ]


def test_type_macro_builder_repeats_named_key_controls() -> None:
    events = build_type_macro_events("<tab:3><backspace:2><down:5>", 10, 0)

    assert _press_codes(events) == [
        evdev.ecodes.KEY_TAB,
        evdev.ecodes.KEY_TAB,
        evdev.ecodes.KEY_TAB,
        evdev.ecodes.KEY_BACKSPACE,
        evdev.ecodes.KEY_BACKSPACE,
        evdev.ecodes.KEY_DOWN,
        evdev.ecodes.KEY_DOWN,
        evdev.ecodes.KEY_DOWN,
        evdev.ecodes.KEY_DOWN,
        evdev.ecodes.KEY_DOWN,
    ]


def test_type_macro_builder_allows_repeat_count_limit() -> None:
    events = build_type_macro_events("<tab:100>", 10, 0)

    assert _press_codes(events) == [evdev.ecodes.KEY_TAB] * 100


def test_type_macro_builder_expands_shortcut_controls() -> None:
    events = build_type_macro_events(
        "<shortcut:ctrl+l><shortcut:ctrl+a><shortcut:ctrl+shift+v>",
        10,
        0,
    )

    assert [
        (event["code"], event["value"]) for event in events if event["type"] == evdev.ecodes.EV_KEY
    ] == [
        (evdev.ecodes.KEY_LEFTCTRL, 1),
        (evdev.ecodes.KEY_L, 1),
        (evdev.ecodes.KEY_L, 0),
        (evdev.ecodes.KEY_LEFTCTRL, 0),
        (evdev.ecodes.KEY_LEFTCTRL, 1),
        (evdev.ecodes.KEY_A, 1),
        (evdev.ecodes.KEY_A, 0),
        (evdev.ecodes.KEY_LEFTCTRL, 0),
        (evdev.ecodes.KEY_LEFTCTRL, 1),
        (evdev.ecodes.KEY_LEFTSHIFT, 1),
        (evdev.ecodes.KEY_V, 1),
        (evdev.ecodes.KEY_V, 0),
        (evdev.ecodes.KEY_LEFTSHIFT, 0),
        (evdev.ecodes.KEY_LEFTCTRL, 0),
    ]


def test_type_macro_builder_expands_mouse_move_control() -> None:
    events = build_type_macro_events("<move:420:180>", 10, 0)

    assert events == [
        {
            "device_type": "macro",
            "type": 0,
            "code": 0,
            "value": 0,
            "t_us": 0,
            "macro_action": "mouse_move_natural_abs",
            "x": 420,
            "y": 180,
            "speed": 100000.0,
            "jitter": 0.0,
            "curve": "linear",
            "tolerance": 2,
            "max_duration_ms": 3000,
            "stop_on_failure": False,
        }
    ]


def test_type_macro_builder_expands_mouse_click_controls() -> None:
    events = build_type_macro_events("<click><lclick><leftclick><rclick><rightclick>", 10, 0)

    assert _key_values(events, evdev.ecodes.BTN_LEFT) == [1, 0, 1, 0, 1, 0]
    assert _key_values(events, evdev.ecodes.BTN_RIGHT) == [1, 0, 1, 0]
    assert all(event["device_type"] == "mouse" for event in events)


def test_type_macro_builder_expands_coordinate_click_control() -> None:
    events = build_type_macro_events("<click:420:180>", 10, 0)

    assert events[0]["macro_action"] == "mouse_move_natural_abs"
    assert events[0]["x"] == 420
    assert events[0]["y"] == 180
    assert events[0]["stop_on_failure"] is True
    assert [
        (event["device_type"], event["code"], event["value"], event["t_us"]) for event in events[1:]
    ] == [
        ("mouse", evdev.ecodes.BTN_LEFT, 1, 1),
        ("mouse", evdev.ecodes.BTN_LEFT, 0, 10001),
    ]


def test_type_macro_builder_expands_doubleclick_control_with_pause() -> None:
    events = build_type_macro_events("<doubleclick>", 10, 20)

    assert [
        (event["code"], event["value"], event["t_us"])
        for event in events
        if event["type"] == evdev.ecodes.EV_KEY
    ] == [
        (evdev.ecodes.BTN_LEFT, 1, 0),
        (evdev.ecodes.BTN_LEFT, 0, 10000),
        (evdev.ecodes.BTN_LEFT, 1, 30000),
        (evdev.ecodes.BTN_LEFT, 0, 40000),
    ]


def test_type_macro_builder_expands_coordinate_doubleclick_control() -> None:
    events = build_type_macro_events("<doubleclick:420:180>", 10, 20)

    assert events[0]["macro_action"] == "mouse_move_natural_abs"
    assert events[0]["x"] == 420
    assert events[0]["y"] == 180
    assert events[0]["stop_on_failure"] is True
    assert [
        (event["device_type"], event["code"], event["value"], event["t_us"]) for event in events[1:]
    ] == [
        ("mouse", evdev.ecodes.BTN_LEFT, 1, 1),
        ("mouse", evdev.ecodes.BTN_LEFT, 0, 10001),
        ("mouse", evdev.ecodes.BTN_LEFT, 1, 30001),
        ("mouse", evdev.ecodes.BTN_LEFT, 0, 40001),
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


def test_type_macro_builder_adds_settle_control() -> None:
    events = build_type_macro_events("a<settle>b", 10, 0)

    waits = [event for event in events if event.get("macro_action")]
    assert waits == [
        {
            "device_type": "macro",
            "type": 0,
            "code": 0,
            "value": 0,
            "t_us": 10_000,
            "macro_action": "wait",
            "duration_us": 300_000,
        }
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

    press_codes = _press_codes(events)
    assert press_codes == [
        evdev.ecodes.KEY_A,
        evdev.ecodes.KEY_BACKSLASH,
        evdev.ecodes.KEY_N,
        evdev.ecodes.KEY_B,
    ]


def test_type_macro_builder_escapes_literal_less_than_before_control() -> None:
    events = build_type_macro_events(r"a\<tab>b\\<tab>c", 10, 0)

    press_codes = _press_codes(events)
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


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("<tab:0>", "tab repeat count must be greater than 0"),
        ("<tab:soon>", "tab repeat count must be an integer"),
        ("<tab:1:2>", "tab repeat control accepts one count argument"),
        ("<tab:101>", "tab repeat count must be less than or equal to 100"),
        ("<shortcut:l>", "shortcut requires modifiers and a key"),
        ("<shortcut:ctrl+bogus>", "unknown shortcut key: bogus"),
        ("<shortcut:ctrl+shift>", "shortcut requires one non-modifier key"),
        ("<move:1>", "move control requires x and y arguments"),
        ("<move:1:2:3>", "move control requires x and y arguments"),
        ("<move:soon:2>", "move x must be an integer"),
        ("<click:1>", "click control accepts optional x and y arguments"),
        ("<click:1:2:3>", "click control accepts optional x and y arguments"),
        ("<rclick:1:soon>", "rclick y must be an integer"),
    ],
)
def test_type_macro_builder_rejects_invalid_extended_controls(
    text: str,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        build_type_macro_events(text, 10, 0)


def test_type_macro_builder_reports_unsupported_character_position() -> None:
    with pytest.raises(ValueError, match=r"position 2: 'é'"):
        build_type_macro_events("aé", 10, 0)


def test_type_macro_builder_unicode_input_holds_modifiers_until_confirmed() -> None:
    events = build_type_macro_events("é", 10, 0, use_unicode_input=True)

    assert [
        (event["code"], event["value"]) for event in events if event["type"] == evdev.ecodes.EV_KEY
    ] == [
        (evdev.ecodes.KEY_LEFTCTRL, 1),
        (evdev.ecodes.KEY_LEFTSHIFT, 1),
        (evdev.ecodes.KEY_U, 1),
        (evdev.ecodes.KEY_U, 0),
        (evdev.ecodes.KEY_E, 1),
        (evdev.ecodes.KEY_E, 0),
        (evdev.ecodes.KEY_9, 1),
        (evdev.ecodes.KEY_9, 0),
        (evdev.ecodes.KEY_ENTER, 1),
        (evdev.ecodes.KEY_ENTER, 0),
        (evdev.ecodes.KEY_LEFTSHIFT, 0),
        (evdev.ecodes.KEY_LEFTCTRL, 0),
    ]


def test_parse_macro_json_accepts_event_list_and_macro_object() -> None:
    events_json = json.dumps([{"t_us": 0}])
    macro_json = json.dumps({"name": "demo", "events": [{"t_us": 1}]})

    assert parse_macro_json(events_json)["events"] == [{"t_us": 0}]
    assert parse_macro_json(macro_json)["name"] == "demo"


def test_parse_macro_json_rejects_runtime_macro_events_payload() -> None:
    with pytest.raises(ValueError, match="event list or macro object"):
        parse_macro_json(json.dumps({"macro_events": [{"t_us": 0}]}))
