import json
import unicodedata
from typing import cast

import evdev

JsonObject = dict[str, object]
IntLike = int | float | str | bytes

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


def normalize_type_macro_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.translate(_TYPE_MACRO_TEXT_TRANSLATION)


def normalize_unicode_type_macro_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


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

        if kind == "key":
            code, needs_shift = char_to_key(value)
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


def _type_macro_tokens(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
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
        if tag == "enter":
            tokens.append(("key", "\n"))
            index = end + 1
            continue
        if tag == "tab":
            tokens.append(("key", "\t"))
            index = end + 1
            continue
        if tag.startswith("wait:"):
            tokens.append(("wait", tag.removeprefix("wait:")))
            index = end + 1
            continue

        tokens.extend(("char", ch) for ch in text[index : end + 1])
        index = end + 1

    return tokens


def _should_add_type_pause(tokens: list[tuple[str, str]], index: int, pause_ms: int) -> bool:
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


def build_compact_macro_events(tokens: list[str]) -> list[JsonObject]:
    events: list[JsonObject] = []
    held: dict[tuple[str, int], int] = {}
    held_order: list[tuple[str, int]] = []
    t_us = 0
    sequence_step_us = 1

    def advance_sequence() -> None:
        nonlocal t_us
        t_us += sequence_step_us

    for token in tokens:
        token = token.strip()
        if not token:
            continue

        parts = token.split(":")
        name = parts[0].strip().lower()
        args = [part.strip() for part in parts[1:]]
        if not name or any(arg == "" for arg in args):
            raise ValueError(f"invalid macro token: {token!r}")

        if name == "wait":
            _append_wait_event(events, args, t_us)
            advance_sequence()
            continue

        if name in {"move_abs", "move_rel"}:
            if len(args) != 2:
                raise ValueError(f"{name} requires x and y arguments")
            x = _parse_int(args[0], f"{name} x")
            y = _parse_int(args[1], f"{name} y")
            if name == "move_abs":
                _append_mouse_move_event(events, "mouse_move_abs", x, y, t_us)
            else:
                _append_mouse_move_event(events, "mouse_move_rel", x, y, t_us)
            advance_sequence()
            continue

        device_type, code = resolve_key_or_button(name)
        if len(args) > 1:
            raise ValueError(f"{name} accepts at most one state argument")

        state = args[0].lower() if args else ""
        held_key = (device_type, code)
        if not state:
            _append_key_event(events, device_type, code, 1, t_us)
            _append_key_event(events, device_type, code, 0, t_us)
            advance_sequence()
            continue

        if state in {"1", "down"}:
            if held_key in held:
                raise ValueError(f"{name} is already held")
            _append_key_event(events, device_type, code, 1, t_us)
            held[held_key] = 1
            held_order.append(held_key)
            advance_sequence()
            continue

        if state in {"0", "up"}:
            if held_key not in held:
                raise ValueError(f"{name} release without matching press")
            _append_key_event(events, device_type, code, 0, t_us)
            held.pop(held_key, None)
            held_order.remove(held_key)
            advance_sequence()
            continue

        raise ValueError(f"invalid state for {name}: {state!r}")

    for device_type, code in reversed(held_order):
        _append_key_event(events, device_type, code, 0, t_us)
        t_us += sequence_step_us

    return events


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
        if "macro_events" in raw:
            payload = dict(raw)
            payload["events"] = payload.pop("macro_events")
            return payload
    raise ValueError("macro JSON must be an event list or macro object")


def build_macro_payload(
    events: list[JsonObject],
    *,
    name: str = "",
    speed: float = 1.0,
    loop_mode: str = "none",
    loop_count: int = 1,
    loop_stop_behavior: str = "finish_run",
    move_to_start: bool = False,
    start_x: int = 0,
    start_y: int = 0,
    block_mouse_movement: bool = False,
) -> JsonObject:
    return {
        "macro_name": name,
        "macro_events": events,
        "speed": float(speed),
        "loop_mode": loop_mode,
        "loop_count": int(loop_count),
        "loop_stop_behavior": loop_stop_behavior,
        "move_to_start": bool(move_to_start),
        "start_x": int(start_x),
        "start_y": int(start_y),
        "block_mouse_movement": bool(block_mouse_movement),
    }


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
    _append_key_event(events, "keyboard", evdev.ecodes.KEY_LEFTSHIFT, 0, t_us)
    t_us += modifier_settle_us
    _append_key_event(events, "keyboard", evdev.ecodes.KEY_LEFTCTRL, 0, t_us)
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
    return _append_direct_key_events(
        events,
        code,
        needs_shift,
        t_us,
        down_ms,
        modifier_settle_us,
    )


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
) -> None:
    events.append(
        {
            "device_type": "macro",
            "type": 0,
            "code": 0,
            "value": 0,
            "t_us": int(t_us),
            "macro_action": action,
            "x": int(x),
            "y": int(y),
        }
    )


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


def _infer_device_types(events: list[JsonObject]) -> list[str]:
    found: list[str] = []
    for event in events:
        if str(event.get("macro_action", "") or "") in {"mouse_move_abs", "mouse_move_rel"}:
            if "mouse" not in found:
                found.append("mouse")
            continue
        device_type = str(event.get("device_type", "") or "")
        if device_type and device_type != "macro" and device_type not in found:
            found.append(device_type)
    return found
