"""Keyboard layouts for turning text into physical key presses.

Keymasq emits evdev scancodes and the compositor turns them into characters
with the XKB layout the user configured, so typing "z" needs KEY_Y on a German
keyboard and KEY_W on a French one. The character tables come from the
system's own XKB data through libxkbcommon (see ``keymasq.common.xkb``), so
any layout and variant that xkeyboard-config knows is supported without a
table in Keymasq.

A layout id is the XKB layout name with an optional variant in parentheses:
``us``, ``de``, ``de(nodeadkeys)``, ``us(dvorak)``. Names are case-sensitive,
like the XKB files they select (``de(T3)``, ``ua(macOS)``).
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path

import evdev

from keymasq.common import xkb

log = logging.getLogger("keymasq.keyboard_layouts")

DEFAULT_KEYBOARD_LAYOUT = "us"

_LAYOUT_ID_RE = re.compile(r"^([A-Za-z0-9_-]+)(?:\(([A-Za-z0-9_-]+)\))?$")
# Layouts the XKB rules list but that only compile with user-provided files.
_PLACEHOLDER_LAYOUTS = frozenset({"custom"})
_WHITESPACE_KEYS = {
    " ": evdev.ecodes.KEY_SPACE,
    "\n": evdev.ecodes.KEY_ENTER,
    "\t": evdev.ecodes.KEY_TAB,
}
# Keys every physical keyboard has. Keymaps also assign characters to keypad,
# ISO-only (KEY_102ND), and extended keys (KEY_EURO), which only win when no
# main-block key produces the character.
_MAIN_BLOCK_MAX_CODE = evdev.ecodes.KEY_COMPOSE
_OPTIONAL_CODES = frozenset(
    {evdev.ecodes.KEY_102ND}
    | {code for name, code in evdev.ecodes.ecodes.items() if name.startswith("KEY_KP")}
)


class KeyboardLayoutError(ValueError):
    """The layout id is malformed, unknown, or libxkbcommon is unavailable."""


@dataclass(frozen=True)
class TypedKey:
    """The key presses that produce one character on a layout.

    ``code`` and ``modifiers`` are the final press. ``dead_keys`` are the dead
    key presses typed before it, in order, when the character needs a compose
    sequence (``ê`` is dead_circumflex then ``e``).
    """

    code: int
    modifiers: tuple[int, ...] = ()
    dead_keys: tuple[TypedKey, ...] = ()

    def _rank(self, level: int) -> _Rank:
        off_main_block = self.code > _MAIN_BLOCK_MAX_CODE or self.code in _OPTIONAL_CODES
        return (0, off_main_block, len(self.modifiers), level, self.code)


# (dead keys, uses a key outside the main block, modifier count, level, code):
# fewer and simpler presses win when several produce the same character.
_Rank = tuple[int, bool, int, int, int]


@dataclass(frozen=True)
class KeyboardLayout:
    id: str
    name: str
    chars: Mapping[str, TypedKey]

    def key_for(self, char: str) -> TypedKey | None:
        return self.chars.get(char)


def parse_keyboard_layout_id(value: object) -> tuple[str, str]:
    """Split ``layout(variant)`` into its parts, raising on malformed input."""
    if not isinstance(value, str):
        raise KeyboardLayoutError(f"keyboard layout must be a string, not {type(value).__name__}")
    match = _LAYOUT_ID_RE.match(value.strip())
    if match is None:
        raise KeyboardLayoutError(f"malformed keyboard layout id: {value!r}")
    return match.group(1), match.group(2) or ""


def format_keyboard_layout_id(layout: str, variant: str = "") -> str:
    return f"{layout}({variant})" if variant else layout


def keyboard_layout(layout_id: object) -> KeyboardLayout:
    """Return the compiled layout, raising KeyboardLayoutError if it cannot be built."""
    layout, variant = parse_keyboard_layout_id(layout_id)
    compiled, error = _compile_layout(layout, variant)
    if compiled is None:
        raise KeyboardLayoutError(error or "keyboard layout could not be compiled")
    return compiled


def keyboard_layout_error(value: object) -> str | None:
    """Why the layout cannot be used, or None when it compiles."""
    try:
        keyboard_layout(value)
    except KeyboardLayoutError as exc:
        return str(exc)
    return None


def is_known_keyboard_layout(value: object) -> bool:
    return keyboard_layout_error(value) is None


def normalize_keyboard_layout_id(value: object) -> str:
    """Trim a layout id; empty or non-string values become the default.

    This does not validate. Settings keep whatever layout the user wrote, and
    the places that compile a layout (type macro creation, the settings
    command) report why it cannot be used instead of silently switching to
    the default.
    """
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_KEYBOARD_LAYOUT
    return value.strip()


def keyboard_layout_name(layout_id: str) -> str:
    """Human-readable name from the XKB rules, or the id itself. Never compiles."""
    return _registry().get(layout_id, layout_id)


def keyboard_layout_choices() -> list[tuple[str, str]]:
    """Every layout and variant the XKB rules describe, as (id, description)."""
    return list(_registry().items())


@lru_cache(maxsize=8)
def unmodified_key_outputs(layout_id: str) -> Mapping[int, str]:
    """Describe what each physical key produces without held modifiers."""
    layout, variant = parse_keyboard_layout_id(layout_id)
    try:
        with xkb.Keymap(layout, variant) as keymap:
            outputs: dict[int, str] = {}
            for level in keymap.levels():
                if level.level != 0 or len(level.keysyms) != 1:
                    continue
                keysym = level.keysyms[0]
                char = xkb.keysym_to_char(keysym)
                if char == " ":
                    outputs[level.evdev_code] = "Space"
                elif char and char.isprintable() and not char.isspace():
                    outputs[level.evdev_code] = char
                elif (name := xkb.keysym_name(keysym)).startswith("dead_"):
                    outputs[level.evdev_code] = name.replace("dead_", "dead ", 1).replace("_", " ")
            return outputs
    except (xkb.XkbUnavailableError, ValueError) as exc:
        raise KeyboardLayoutError(str(exc)) from exc


# Spacing forms of the dead keys xkeyboard-config puts on main-block keys, as
# printed on keycaps. Other dead keys show a dotted circle.
_DEAD_KEY_GLYPHS = {
    "grave": "`",
    "acute": "´",
    "circumflex": "^",
    "tilde": "~",
    "perispomeni": "~",
    "macron": "¯",
    "breve": "˘",
    "abovedot": "˙",
    "diaeresis": "¨",
    "abovering": "˚",
    "doubleacute": "˝",
    "caron": "ˇ",
    "cedilla": "¸",
    "ogonek": "˛",
    "iota": "ͺ",
    "stroke": "/",
    "belowdot": ".",
    "belowcomma": ",",
}


@dataclass(frozen=True)
class KeyLegend:
    """What a keycap shows on a layout.

    ``base`` is the unshifted output. ``shifted`` is empty when Shift only
    capitalizes it; letters show their capital as ``base``, like printed keys.
    """

    base: str
    shifted: str = ""


@lru_cache(maxsize=8)
def key_legends(layout_id: str) -> Mapping[int, KeyLegend]:
    """Keycap legends for every key that types a character or dead key."""
    layout, variant = parse_keyboard_layout_id(layout_id)
    try:
        with xkb.Keymap(layout, variant) as keymap:
            legends: dict[int, KeyLegend] = {}
            for level in keymap.levels():
                if level.level != 0:
                    continue
                code = level.evdev_code
                base = _legend_text(keymap.keysym_for_chord(code, ()))
                if not base:
                    continue
                shifted = _legend_text(keymap.keysym_for_chord(code, (evdev.ecodes.KEY_LEFTSHIFT,)))
                if shifted == base.upper() or shifted == base:
                    legends[code] = KeyLegend(base.upper() if len(base.upper()) == 1 else base)
                else:
                    legends[code] = KeyLegend(base, shifted)
            return legends
    except (xkb.XkbUnavailableError, ValueError) as exc:
        raise KeyboardLayoutError(str(exc)) from exc


def _legend_text(keysym: int) -> str:
    char = xkb.keysym_to_char(keysym)
    if char and char.isprintable() and not char.isspace():
        return char
    name = xkb.keysym_name(keysym)
    if name.startswith("dead_"):
        return _DEAD_KEY_GLYPHS.get(name.removeprefix("dead_"), "◌")
    return ""


# A compiled layout holds about 260 KiB of character map. The setting and a few
# ``--layout`` overrides are all a process needs; the bound keeps a client that
# tries many layouts from growing the session indefinitely.
_COMPILED_LAYOUT_CACHE_SIZE = 8


@lru_cache(maxsize=_COMPILED_LAYOUT_CACHE_SIZE)
def _compile_layout(layout: str, variant: str) -> tuple[KeyboardLayout | None, str | None]:
    """Compile once per process. Failures are cached too, so the GUI can check
    the configured layout on every keystroke without touching libxkbcommon."""
    layout_id = format_keyboard_layout_id(layout, variant)
    try:
        with xkb.Keymap(layout, variant) as keymap:
            chars = _chars_from_keymap(keymap)
    except (xkb.XkbUnavailableError, ValueError) as exc:
        return None, str(exc)
    for char, code in _WHITESPACE_KEYS.items():
        chars[char] = TypedKey(code)
    name = _registry().get(layout_id, layout_id)
    return KeyboardLayout(id=layout_id, name=name, chars=chars), None


def _chars_from_keymap(keymap: xkb.Keymap) -> dict[str, TypedKey]:
    """Map each character to the cheapest presses that produce it.

    Direct presses come from the keymap levels. Everything else comes from the
    locale's compose table: any sequence whose keysyms the layout can all
    produce (dead_circumflex then ``e`` for ``ê``, dead_acute then Space for
    the acute accent itself) is typed the way a person would type it.
    """
    presses = _presses_by_keysym(keymap)
    best: dict[str, tuple[_Rank, TypedKey]] = {}

    def offer(char: str, rank: _Rank, key: TypedKey) -> None:
        if not _is_typeable_char(char):
            return
        current = best.get(char)
        if current is None or rank < current[0]:
            best[char] = (rank, key)

    for keysym, (rank, key) in presses.items():
        char = xkb.keysym_to_char(keysym)
        if char is not None:
            offer(char, rank, key)
    for sequence, text in keymap.compose_sequences(presses.keys()):
        if len(text) != 1:
            continue
        parts = [presses[keysym] for keysym in sequence]
        final_rank, final = parts[-1]
        key = TypedKey(final.code, final.modifiers, tuple(press for _rank, press in parts[:-1]))
        rank: _Rank = (
            len(parts) - 1,
            any(part_rank[1] for part_rank, _press in parts),
            sum(part_rank[2] for part_rank, _press in parts),
            max(part_rank[3] for part_rank, _press in parts),
            final_rank[4],
        )
        offer(text, rank, key)
    return {char: key for char, (_rank, key) in best.items()}


def _presses_by_keysym(keymap: xkb.Keymap) -> dict[int, tuple[_Rank, TypedKey]]:
    """The cheapest verified chord for every keysym the layout produces."""
    best: dict[int, tuple[_Rank, TypedKey]] = {}
    key_codes = evdev.ecodes.bytype.get(evdev.ecodes.EV_KEY, {})
    modifier_keys = _modifier_keys_by_name(keymap, key_codes)
    for key_level in keymap.levels():
        if key_level.evdev_code not in key_codes or len(key_level.keysyms) != 1:
            continue
        modifiers = _modifiers_for_level(key_level.modifier_masks, modifier_keys)
        if modifiers is None:
            continue
        keysym = key_level.keysyms[0]
        candidate = TypedKey(key_level.evdev_code, modifiers)
        if keymap.keysym_for_chord(candidate.code, candidate.modifiers) != keysym:
            continue
        rank = candidate._rank(key_level.level)
        current = best.get(keysym)
        if current is None or rank < current[0]:
            best[keysym] = (rank, candidate)
    return best


def _is_typeable_char(char: str) -> bool:
    if len(char) != 1:
        return False
    if char in _WHITESPACE_KEYS:
        return True
    return char.isprintable() and not char.isspace()


def _modifier_keys_by_name(keymap: xkb.Keymap, key_codes: Mapping[int, object]) -> dict[str, int]:
    """One physical key to hold for each modifier the keymap lets a key set.

    The keymap decides which key that is: Right Alt sets the third level on
    most layouts, Caps Lock does on Neo. Main-block keys win over keypad or
    ISO keys, then the lower code, so Left Shift is chosen over Right Shift.
    """
    chosen: dict[str, int] = {}
    for code, names in keymap.modifier_keys().items():
        if code not in key_codes:
            continue
        for name in names:
            current = chosen.get(name)
            if current is None or _modifier_key_order(code) < _modifier_key_order(current):
                chosen[name] = code
    return chosen


def _modifier_key_order(code: int) -> tuple[bool, int]:
    return (code > _MAIN_BLOCK_MAX_CODE or code in _OPTIONAL_CODES, code)


def _modifiers_for_level(
    masks: tuple[frozenset[str], ...],
    modifier_keys: Mapping[str, int],
) -> tuple[int, ...] | None:
    """Pick the first modifier combination Keymasq can hold to reach a level.

    Holding a key may set more modifiers than the mask names (Right Alt on
    ``us`` is Alt); the chord is verified against the keymap afterwards, so
    such a key is dropped when it changes the keysym.
    """
    if not masks:
        return ()
    for mask in masks:
        if all(name in modifier_keys for name in mask):
            return tuple(sorted({modifier_keys[name] for name in mask}, key=_modifier_order))
    return None


def _modifier_order(code: int) -> tuple[int, int]:
    return (0 if code in (evdev.ecodes.KEY_LEFTSHIFT, evdev.ecodes.KEY_RIGHTSHIFT) else 1, code)


# The main rules file plus the "extras" file xkeyboard-config ships for layouts
# it considers exotic (de(bone), de(neo_qwertz), ...). Both compile the same way.
_RULES_FILES = ("evdev.xml", "evdev.extras.xml")


@cache
def _registry() -> dict[str, str]:
    """Layout and variant descriptions from the first XKB rules directory found."""
    try:
        paths = xkb.include_paths()
    except xkb.XkbUnavailableError:
        return {}
    for directory in paths:
        rules_dir = Path(directory) / "rules"
        if not (rules_dir / _RULES_FILES[0]).is_file():
            continue
        registry: dict[str, str] = {}
        for filename in _RULES_FILES:
            rules = rules_dir / filename
            if not rules.is_file():
                continue
            try:
                registry.update(_parse_rules(rules))
            except ET.ParseError as exc:
                log.warning("Cannot parse XKB rules %s: %s", rules, exc)
        return registry
    return {}


def _parse_rules(rules: Path) -> dict[str, str]:
    registry: dict[str, str] = {}
    for layout in ET.parse(rules).getroot().iterfind("./layoutList/layout"):
        layout_name = layout.findtext("./configItem/name")
        if not layout_name or layout_name in _PLACEHOLDER_LAYOUTS:
            continue
        description = layout.findtext("./configItem/description") or layout_name
        registry[layout_name] = description
        for variant in layout.iterfind("./variantList/variant"):
            variant_name = variant.findtext("./configItem/name")
            if not variant_name:
                continue
            variant_description = variant.findtext("./configItem/description") or variant_name
            registry[format_keyboard_layout_id(layout_name, variant_name)] = variant_description
    return registry
