import json
import unicodedata
from typing import cast

import evdev

from keymasq.common.model.actions import (
    DEFAULT_NATURAL_MOUSE_MOVE_MAX_DURATION_MS,
    DEFAULT_NATURAL_MOUSE_MOVE_TOLERANCE,
)
from keymasq.common.types import JsonObject

IntLike = int | float | str | bytes
TypeMacroToken = tuple[str, str]
DEFAULT_TYPE_MACRO_DOWN_MS = 5
DEFAULT_TYPE_MACRO_PAUSE_MS = 10
_TYPE_MACRO_NATURAL_MOUSE_MOVE_SPEED = 100_000.0
_TYPE_MACRO_NATURAL_MOUSE_MOVE_JITTER = 0.0
_TYPE_MACRO_NATURAL_MOUSE_MOVE_CURVE = "linear"
_TYPE_MACRO_SETTLE_WAIT_MS = 300
_TYPE_MACRO_MAX_REPEAT_COUNT = 100

_TYPE_MACRO_TEXT_TRANSLATION = str.maketrans(
    {
        "\u00a0": " ",
        "\u00ad": "",
        "\u2007": " ",
        "\u200b": "",
        "\u200c": "",
        "\u200d": "",
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2026": "...",
        "\u202f": " ",
        "\u2212": "-",
        "\ufeff": "",
    }
)

_TYPE_MACRO_NAMED_KEY_CODES = {
    "space": evdev.ecodes.KEY_SPACE,
    "enter": evdev.ecodes.KEY_ENTER,
    "tab": evdev.ecodes.KEY_TAB,
    "esc": evdev.ecodes.KEY_ESC,
    "backspace": evdev.ecodes.KEY_BACKSPACE,
    "delete": evdev.ecodes.KEY_DELETE,
    "up": evdev.ecodes.KEY_UP,
    "down": evdev.ecodes.KEY_DOWN,
    "left": evdev.ecodes.KEY_LEFT,
    "right": evdev.ecodes.KEY_RIGHT,
    "home": evdev.ecodes.KEY_HOME,
    "end": evdev.ecodes.KEY_END,
    "pageup": evdev.ecodes.KEY_PAGEUP,
    "pagedown": evdev.ecodes.KEY_PAGEDOWN,
}

_TYPE_MACRO_SHORTCUT_MODIFIER_CODES = {
    "ctrl": evdev.ecodes.KEY_LEFTCTRL,
    "control": evdev.ecodes.KEY_LEFTCTRL,
    "shift": evdev.ecodes.KEY_LEFTSHIFT,
    "alt": evdev.ecodes.KEY_LEFTALT,
    "super": evdev.ecodes.KEY_LEFTMETA,
    "meta": evdev.ecodes.KEY_LEFTMETA,
    "win": evdev.ecodes.KEY_LEFTMETA,
}

_TYPE_MACRO_CLICK_BUTTON_CODES = {
    "click": evdev.ecodes.BTN_LEFT,
    "lclick": evdev.ecodes.BTN_LEFT,
    "leftclick": evdev.ecodes.BTN_LEFT,
    "rclick": evdev.ecodes.BTN_RIGHT,
    "rightclick": evdev.ecodes.BTN_RIGHT,
}


def normalize_type_macro_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.translate(_TYPE_MACRO_TEXT_TRANSLATION)


def normalize_unicode_type_macro_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalize_type_macro_binding_text(text: str, *, use_unicode_input: bool) -> str:
    if use_unicode_input:
        return normalize_unicode_type_macro_text(text)
    return normalize_type_macro_text(text)


def build_type_macro_events(
    text: str,
    down_ms: int,
    pause_ms: int,
    *,
    use_unicode_input: bool = False,
) -> list[JsonObject]:
    events: list[JsonObject] = []
    t_us = 0
    down_ms = max(0, int(down_ms))
    pause_ms = max(0, int(pause_ms))
    modifier_settle_us = 1_000
    normalized = (
        normalize_unicode_type_macro_text(text)
        if use_unicode_input
        else normalize_type_macro_text(text)
    )
    tokens = _type_macro_tokens(normalized)

    for i, (kind, value) in enumerate(tokens):
        if kind == "wait":
            _append_wait_event(events, value.split(":"), t_us)
            continue

        if kind == "mouse_move":
            t_us = _append_type_macro_mouse_move_event(events, value, t_us)
        elif kind == "mouse_click":
            t_us = _append_type_macro_mouse_click_events(events, value, t_us, down_ms)
        elif kind == "shortcut":
            t_us = _append_type_macro_shortcut_events(
                events,
                value,
                t_us,
                down_ms,
                modifier_settle_us,
            )
        else:
            if kind == "key":
                code, needs_shift = _resolve_type_macro_key(value)
            else:
                ch = value
                try:
                    code, needs_shift = char_to_key(ch)
                except ValueError as exc:
                    if use_unicode_input:
                        t_us = _append_unicode_char_events(
                            events,
                            ch,
                            t_us,
                            down_ms,
                            modifier_settle_us,
                        )
                        if _should_add_type_pause(tokens, i, pause_ms):
                            t_us += pause_ms * 1000
                        continue

                    char_name = unicodedata.name(ch, "UNKNOWN")
                    raise ValueError(
                        f"Unsupported character at position {i + 1}: {ch!r} ({char_name})"
                    ) from exc

            t_us = _append_direct_key_events(
                events,
                code,
                needs_shift,
                t_us,
                down_ms,
                modifier_settle_us,
            )

        if _should_add_type_pause(tokens, i, pause_ms):
            t_us += pause_ms * 1000

    return events


def _type_macro_tokens(text: str) -> list[TypeMacroToken]:
    tokens: list[TypeMacroToken] = []
    index = 0
    while index < len(text):
        if text.startswith(r"\<", index):
            tokens.append(("char", "<"))
            index += 2
            continue

        if text[index] != "<":
            tokens.append(("char", text[index]))
            index += 1
            continue

        end = text.find(">", index + 1)
        if end < 0:
            tokens.append(("char", text[index]))
            index += 1
            continue

        raw_tag = text[index + 1 : end]
        tag = raw_tag.strip().lower()
        control_tokens = _type_macro_control_tokens(tag)
        if control_tokens is not None:
            tokens.extend(control_tokens)
            index = end + 1
            continue

        tokens.extend(("char", ch) for ch in text[index : end + 1])
        index = end + 1

    return tokens


def _type_macro_control_tokens(tag: str) -> list[TypeMacroToken] | None:
    if tag == "settle":
        return [("wait", str(_TYPE_MACRO_SETTLE_WAIT_MS))]

    if tag.startswith("wait:"):
        return [("wait", tag.removeprefix("wait:"))]

    if tag.startswith("shortcut:"):
        shortcut = tag.removeprefix("shortcut:").strip()
        if not shortcut:
            raise ValueError("shortcut requires modifiers and a key")
        return [("shortcut", shortcut)]

    move_token = _parse_type_macro_mouse_move_control(tag)
    if move_token is not None:
        return [move_token]

    click_tokens = _parse_type_macro_click_control(tag)
    if click_tokens is not None:
        return click_tokens

    named_key = _parse_type_macro_named_key_control(tag)
    if named_key is None:
        return None

    name, repeat_count = named_key
    return [("key", name) for _ in range(repeat_count)]


def _parse_type_macro_mouse_move_control(tag: str) -> TypeMacroToken | None:
    if not tag.startswith("move:"):
        return None

    args = [part.strip() for part in tag.removeprefix("move:").split(":")]
    if len(args) != 2 or any(not arg for arg in args):
        raise ValueError("move control requires x and y arguments")

    x = _parse_int(args[0], "move x")
    y = _parse_int(args[1], "move y")
    return "mouse_move", f"{x}:{y}"


def _parse_type_macro_click_control(tag: str) -> list[TypeMacroToken] | None:
    parts = [part.strip() for part in tag.split(":")]
    name = parts[0]
    if name == "doubleclick":
        button_code = evdev.ecodes.BTN_LEFT
        click_args = parts[1:]
        first_click = _type_macro_mouse_click_token(button_code, click_args, name)
        return [first_click, _format_type_macro_mouse_click_token(button_code)]

    button_code = _TYPE_MACRO_CLICK_BUTTON_CODES.get(name)
    if button_code is None:
        return None

    return [_type_macro_mouse_click_token(button_code, parts[1:], name)]


def _type_macro_mouse_click_token(
    button_code: int,
    args: list[str],
    name: str,
) -> TypeMacroToken:
    if not args:
        return _format_type_macro_mouse_click_token(button_code)
    if len(args) != 2 or any(not arg for arg in args):
        raise ValueError(f"{name} control accepts optional x and y arguments")

    x = _parse_int(args[0], f"{name} x")
    y = _parse_int(args[1], f"{name} y")
    return _format_type_macro_mouse_click_token(button_code, x, y)


def _format_type_macro_mouse_click_token(
    button_code: int,
    x: int | None = None,
    y: int | None = None,
) -> TypeMacroToken:
    if x is None or y is None:
        return "mouse_click", str(button_code)
    return "mouse_click", f"{button_code}:{x}:{y}"


def _parse_type_macro_named_key_control(tag: str) -> tuple[str, int] | None:
    if ":" not in tag:
        if tag not in _TYPE_MACRO_NAMED_KEY_CODES:
            return None
        return tag, 1

    parts = [part.strip() for part in tag.split(":")]
    name = parts[0]
    if name not in _TYPE_MACRO_NAMED_KEY_CODES:
        return None
    if len(parts) != 2:
        raise ValueError(f"{name} repeat control accepts one count argument")

    repeat_count = _parse_positive_int(parts[1], f"{name} repeat count")
    if repeat_count > _TYPE_MACRO_MAX_REPEAT_COUNT:
        raise ValueError(
            f"{name} repeat count must be less than or equal to {_TYPE_MACRO_MAX_REPEAT_COUNT}"
        )
    return name, repeat_count


def _should_add_type_pause(tokens: list[TypeMacroToken], index: int, pause_ms: int) -> bool:
    return pause_ms > 0 and index < len(tokens) - 1 and tokens[index + 1][0] != "wait"


def can_type_directly(ch: str) -> bool:
    try:
        char_to_key(ch)
    except ValueError:
        return False
    return True


def char_to_key(ch: str) -> tuple[int, bool]:
    letters = "abcdefghijklmnopqrstuvwxyz"
    if ch.lower() in letters:
        return getattr(evdev.ecodes, f"KEY_{ch.upper()}"), ch.isupper()

    digits = {
        "1": evdev.ecodes.KEY_1,
        "2": evdev.ecodes.KEY_2,
        "3": evdev.ecodes.KEY_3,
        "4": evdev.ecodes.KEY_4,
        "5": evdev.ecodes.KEY_5,
        "6": evdev.ecodes.KEY_6,
        "7": evdev.ecodes.KEY_7,
        "8": evdev.ecodes.KEY_8,
        "9": evdev.ecodes.KEY_9,
        "0": evdev.ecodes.KEY_0,
    }
    if ch in digits:
        return digits[ch], False

    specials = {
        " ": (evdev.ecodes.KEY_SPACE, False),
        "\n": (evdev.ecodes.KEY_ENTER, False),
        "\t": (evdev.ecodes.KEY_TAB, False),
        "-": (evdev.ecodes.KEY_MINUS, False),
        "_": (evdev.ecodes.KEY_MINUS, True),
        "=": (evdev.ecodes.KEY_EQUAL, False),
        "+": (evdev.ecodes.KEY_EQUAL, True),
        "[": (evdev.ecodes.KEY_LEFTBRACE, False),
        "{": (evdev.ecodes.KEY_LEFTBRACE, True),
        "]": (evdev.ecodes.KEY_RIGHTBRACE, False),
        "}": (evdev.ecodes.KEY_RIGHTBRACE, True),
        "\\": (evdev.ecodes.KEY_BACKSLASH, False),
        "|": (evdev.ecodes.KEY_BACKSLASH, True),
        ";": (evdev.ecodes.KEY_SEMICOLON, False),
        ":": (evdev.ecodes.KEY_SEMICOLON, True),
        "'": (evdev.ecodes.KEY_APOSTROPHE, False),
        '"': (evdev.ecodes.KEY_APOSTROPHE, True),
        ",": (evdev.ecodes.KEY_COMMA, False),
        "<": (evdev.ecodes.KEY_COMMA, True),
        ".": (evdev.ecodes.KEY_DOT, False),
        ">": (evdev.ecodes.KEY_DOT, True),
        "/": (evdev.ecodes.KEY_SLASH, False),
        "?": (evdev.ecodes.KEY_SLASH, True),
        "`": (evdev.ecodes.KEY_GRAVE, False),
        "~": (evdev.ecodes.KEY_GRAVE, True),
        "!": (evdev.ecodes.KEY_1, True),
        "@": (evdev.ecodes.KEY_2, True),
        "#": (evdev.ecodes.KEY_3, True),
        "$": (evdev.ecodes.KEY_4, True),
        "%": (evdev.ecodes.KEY_5, True),
        "^": (evdev.ecodes.KEY_6, True),
        "&": (evdev.ecodes.KEY_7, True),
        "*": (evdev.ecodes.KEY_8, True),
        "(": (evdev.ecodes.KEY_9, True),
        ")": (evdev.ecodes.KEY_0, True),
    }
    if ch in specials:
        return specials[ch]

    raise ValueError(f"Unsupported character for typing macro: {ch!r}")


def _resolve_type_macro_key(value: str) -> tuple[int, bool]:
    if value in _TYPE_MACRO_NAMED_KEY_CODES:
        return _TYPE_MACRO_NAMED_KEY_CODES[value], False
    return char_to_key(value)


def resolve_key_or_button(name: str) -> tuple[str, int]:
    normalized = name.strip().upper()
    if not normalized:
        raise ValueError("empty key/button token")
    if not (normalized.startswith("KEY_") or normalized.startswith("BTN_")):
        if normalized.startswith("BTN"):
            normalized = f"BTN_{normalized[3:]}"
        else:
            normalized = f"KEY_{normalized}"
    normalized = normalized.replace("-", "_")
    code = evdev.ecodes.ecodes.get(normalized)
    if not isinstance(code, int):
        raise ValueError(f"unknown key/button: {name}")
    if code not in evdev.ecodes.bytype.get(evdev.ecodes.EV_KEY, {}):
        raise ValueError(f"not an EV_KEY code: {name}")
    device_type = "mouse" if normalized.startswith("BTN_") else "keyboard"
    return device_type, int(code)


def parse_macro_json(value: str) -> JsonObject:
    decoded = json.loads(value)
    if isinstance(decoded, list):
        return {"events": cast(list[object], decoded)}
    if isinstance(decoded, dict):
        raw = cast(JsonObject, decoded)
        if "events" in raw:
            return raw
    raise ValueError("macro JSON must be an event list or macro object")


def macro_definition_from_events(
    events: list[JsonObject],
    *,
    name: str = "",
    device_types: list[str] | None = None,
) -> JsonObject:
    inferred_device_types = device_types or _infer_device_types(events)
    duration_us = max((_event_end_us(event) for event in events), default=0)
    data: JsonObject = {
        "duration_us": duration_us,
        "device_types": inferred_device_types,
        "events": events,
    }
    if name:
        data["name"] = name
    return data


def _event_end_us(event: JsonObject) -> int:
    return int(cast(IntLike, event.get("t_us", 0)))


def _append_key_event(
    events: list[JsonObject],
    device_type: str,
    code: int,
    value: int,
    t_us: int,
) -> None:
    events.append(
        {
            "device_type": device_type,
            "type": evdev.ecodes.EV_KEY,
            "code": int(code),
            "value": int(value),
            "t_us": int(t_us),
        }
    )


def _append_direct_key_events(
    events: list[JsonObject],
    code: int,
    needs_shift: bool,
    t_us: int,
    down_ms: int,
    modifier_settle_us: int,
) -> int:
    if needs_shift:
        _append_key_event(events, "keyboard", evdev.ecodes.KEY_LEFTSHIFT, 1, t_us)
        t_us += modifier_settle_us

    _append_key_event(events, "keyboard", code, 1, t_us)
    t_us += down_ms * 1000
    _append_key_event(events, "keyboard", code, 0, t_us)

    if needs_shift:
        t_us += modifier_settle_us
        _append_key_event(events, "keyboard", evdev.ecodes.KEY_LEFTSHIFT, 0, t_us)

    return t_us


def _append_type_macro_shortcut_events(
    events: list[JsonObject],
    shortcut: str,
    t_us: int,
    down_ms: int,
    modifier_settle_us: int,
) -> int:
    modifier_codes, key_code = _parse_type_macro_shortcut(shortcut)

    for modifier_code in modifier_codes:
        _append_key_event(events, "keyboard", modifier_code, 1, t_us)
        t_us += modifier_settle_us

    _append_key_event(events, "keyboard", key_code, 1, t_us)
    t_us += down_ms * 1000
    _append_key_event(events, "keyboard", key_code, 0, t_us)

    for modifier_code in reversed(modifier_codes):
        t_us += modifier_settle_us
        _append_key_event(events, "keyboard", modifier_code, 0, t_us)

    return t_us


def _parse_type_macro_shortcut(shortcut: str) -> tuple[list[int], int]:
    parts = [part.strip() for part in shortcut.split("+")]
    if len(parts) < 2 or any(not part for part in parts):
        raise ValueError("shortcut requires modifiers and a key")

    key_part = parts[-1]
    if key_part in _TYPE_MACRO_SHORTCUT_MODIFIER_CODES:
        raise ValueError("shortcut requires one non-modifier key")

    modifier_codes: list[int] = []
    for modifier_name in parts[:-1]:
        modifier_code = _TYPE_MACRO_SHORTCUT_MODIFIER_CODES.get(modifier_name)
        if modifier_code is None:
            raise ValueError(f"unknown shortcut modifier: {modifier_name}")
        if modifier_code in modifier_codes:
            raise ValueError(f"duplicate shortcut modifier: {modifier_name}")
        modifier_codes.append(modifier_code)

    key_code, needs_shift = _resolve_type_macro_shortcut_key(key_part)
    shift_code = evdev.ecodes.KEY_LEFTSHIFT
    if needs_shift and shift_code not in modifier_codes:
        modifier_codes.append(shift_code)
    return modifier_codes, key_code


def _resolve_type_macro_shortcut_key(value: str) -> tuple[int, bool]:
    if value in _TYPE_MACRO_NAMED_KEY_CODES:
        return _TYPE_MACRO_NAMED_KEY_CODES[value], False
    if len(value) == 1:
        return char_to_key(value)

    try:
        device_type, code = resolve_key_or_button(value)
    except ValueError as exc:
        raise ValueError(f"unknown shortcut key: {value}") from exc
    if device_type != "keyboard":
        raise ValueError(f"shortcut key must be a keyboard key: {value}")
    return code, False


def _append_type_macro_mouse_move_event(
    events: list[JsonObject],
    value: str,
    t_us: int,
) -> int:
    x, y = _parse_type_macro_coordinate_value(value)
    _append_type_macro_natural_move_event(events, x, y, t_us, stop_on_failure=False)
    return t_us + 1


def _append_type_macro_mouse_click_events(
    events: list[JsonObject],
    value: str,
    t_us: int,
    down_ms: int,
) -> int:
    parts = value.split(":")
    button_code = _parse_int(parts[0], "click button")
    if len(parts) == 3:
        x = _parse_int(parts[1], "click x")
        y = _parse_int(parts[2], "click y")
        _append_type_macro_natural_move_event(events, x, y, t_us, stop_on_failure=True)
        t_us += 1
    elif len(parts) != 1:
        raise ValueError("click control accepts optional x and y arguments")

    _append_key_event(events, "mouse", button_code, 1, t_us)
    t_us += down_ms * 1000
    _append_key_event(events, "mouse", button_code, 0, t_us)
    return t_us


def _append_type_macro_natural_move_event(
    events: list[JsonObject],
    x: int,
    y: int,
    t_us: int,
    *,
    stop_on_failure: bool,
) -> None:
    _append_mouse_move_event(
        events,
        "mouse_move_natural_abs",
        x,
        y,
        t_us,
        options={
            "speed": _TYPE_MACRO_NATURAL_MOUSE_MOVE_SPEED,
            "jitter": _TYPE_MACRO_NATURAL_MOUSE_MOVE_JITTER,
            "curve": _TYPE_MACRO_NATURAL_MOUSE_MOVE_CURVE,
            "tolerance": DEFAULT_NATURAL_MOUSE_MOVE_TOLERANCE,
            "max_duration_ms": DEFAULT_NATURAL_MOUSE_MOVE_MAX_DURATION_MS,
            "stop_on_failure": stop_on_failure,
        },
    )


def _parse_type_macro_coordinate_value(value: str) -> tuple[int, int]:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError("move control requires x and y arguments")
    return _parse_int(parts[0], "move x"), _parse_int(parts[1], "move y")


def _append_unicode_char_events(
    events: list[JsonObject],
    ch: str,
    t_us: int,
    down_ms: int,
    modifier_settle_us: int,
) -> int:
    _append_key_event(events, "keyboard", evdev.ecodes.KEY_LEFTCTRL, 1, t_us)
    t_us += modifier_settle_us
    _append_key_event(events, "keyboard", evdev.ecodes.KEY_LEFTSHIFT, 1, t_us)
    t_us += modifier_settle_us

    _append_key_event(events, "keyboard", evdev.ecodes.KEY_U, 1, t_us)
    t_us += down_ms * 1000
    _append_key_event(events, "keyboard", evdev.ecodes.KEY_U, 0, t_us)
    t_us += modifier_settle_us

    for hex_digit in f"{ord(ch):x}":
        code, needs_shift = char_to_key(hex_digit)
        t_us = _append_direct_key_events(
            events,
            code,
            needs_shift,
            t_us,
            down_ms,
            modifier_settle_us,
        )

    code, needs_shift = char_to_key("\n")
    t_us = _append_direct_key_events(
        events,
        code,
        needs_shift,
        t_us,
        down_ms,
        modifier_settle_us,
    )
    t_us += modifier_settle_us
    _append_key_event(events, "keyboard", evdev.ecodes.KEY_LEFTSHIFT, 0, t_us)
    t_us += modifier_settle_us
    _append_key_event(events, "keyboard", evdev.ecodes.KEY_LEFTCTRL, 0, t_us)
    return t_us


def _append_wait_event(events: list[JsonObject], args: list[str], t_us: int) -> None:
    if len(args) == 1:
        duration_ms = _parse_non_negative_int(args[0], "wait duration")
        duration_us = duration_ms * 1000
        events.append(
            {
                "device_type": "macro",
                "type": 0,
                "code": 0,
                "value": 0,
                "t_us": int(t_us),
                "macro_action": "wait",
                "duration_us": duration_us,
            }
        )
        return

    if len(args) == 2:
        min_ms = _parse_non_negative_int(args[0], "wait min")
        max_ms = _parse_non_negative_int(args[1], "wait max")
        if max_ms < min_ms:
            raise ValueError("wait max must be greater than or equal to wait min")
        min_us = min_ms * 1000
        max_us = max_ms * 1000
        events.append(
            {
                "device_type": "macro",
                "type": 0,
                "code": 0,
                "value": 0,
                "t_us": int(t_us),
                "macro_action": "wait_random",
                "min_us": min_us,
                "max_us": max_us,
            }
        )
        return

    raise ValueError("wait requires one duration or min:max arguments")


def _append_mouse_move_event(
    events: list[JsonObject],
    action: str,
    x: int,
    y: int,
    t_us: int,
    *,
    options: JsonObject | None = None,
) -> None:
    event: JsonObject = {
        "device_type": "macro",
        "type": 0,
        "code": 0,
        "value": 0,
        "t_us": int(t_us),
        "macro_action": action,
        "x": int(x),
        "y": int(y),
    }
    if options:
        event.update(options)
    events.append(event)


def _parse_int(value: str, label: str) -> int:
    try:
        return int(value, 10)
    except ValueError as exc:
        raise ValueError(f"{label} must be an integer") from exc


def _parse_non_negative_int(value: str, label: str) -> int:
    parsed = _parse_int(value, label)
    if parsed < 0:
        raise ValueError(f"{label} must be non-negative")
    return parsed


def _parse_positive_int(value: str, label: str) -> int:
    parsed = _parse_int(value, label)
    if parsed <= 0:
        raise ValueError(f"{label} must be greater than 0")
    return parsed


def _infer_device_types(events: list[JsonObject]) -> list[str]:
    found: list[str] = []
    for event in events:
        if str(event.get("macro_action", "") or "") in {
            "mouse_move_abs",
            "mouse_move_rel",
            "mouse_move_natural_abs",
        }:
            if "mouse" not in found:
                found.append("mouse")
            continue
        device_type = str(event.get("device_type", "") or "")
        if device_type and device_type != "macro" and device_type not in found:
            found.append(device_type)
    return found
