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
from functools import cache
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
# Modifier names libxkbcommon reports for a level, and the key Keymasq holds
# to reach that level. Levels needing anything else (Lock, NumLock, Control)
# cannot be typed and are skipped.
_MODIFIER_KEYS = {
    "Shift": evdev.ecodes.KEY_LEFTSHIFT,
    "Mod5": evdev.ecodes.KEY_RIGHTALT,
    "LevelThree": evdev.ecodes.KEY_RIGHTALT,
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
    return _compile_layout(layout, variant)


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


@cache
def _compile_layout(layout: str, variant: str) -> KeyboardLayout:
    layout_id = format_keyboard_layout_id(layout, variant)
    try:
        with xkb.Keymap(layout, variant) as keymap:
            chars = _chars_from_keymap(keymap)
    except xkb.XkbUnavailableError as exc:
        raise KeyboardLayoutError(str(exc)) from exc
    except ValueError as exc:
        raise KeyboardLayoutError(str(exc)) from exc
    for char, code in _WHITESPACE_KEYS.items():
        chars[char] = TypedKey(code)
    return KeyboardLayout(id=layout_id, name=_registry().get(layout_id, layout_id), chars=chars)


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
    for key_level in keymap.levels():
        if key_level.evdev_code not in key_codes or len(key_level.keysyms) != 1:
            continue
        modifiers = _modifiers_for_level(key_level.modifier_masks)
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


def _modifiers_for_level(masks: tuple[frozenset[str], ...]) -> tuple[int, ...] | None:
    """Pick the first modifier combination Keymasq can hold to reach a level."""
    if not masks:
        return ()
    for mask in masks:
        if all(name in _MODIFIER_KEYS for name in mask):
            return tuple(sorted({_MODIFIER_KEYS[name] for name in mask}, key=_modifier_order))
    return None


def _modifier_order(code: int) -> int:
    return 0 if code == evdev.ecodes.KEY_LEFTSHIFT else 1


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
