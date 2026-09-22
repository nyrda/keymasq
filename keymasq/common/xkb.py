"""Minimal ctypes binding to libxkbcommon.

Keymasq only needs to compile an XKB keymap from rule names, read which
keysyms each key produces at each shift level, and enumerate the compose
sequences (dead key plus letter) the user's locale defines. The library ships
with every GTK4 or Wayland install; the
loader path can be pinned through ``build_paths`` or ``KEYMASQ_LIBXKBCOMMON_PATH``
for environments where the dynamic loader cannot find it by name.
"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from functools import cache

from keymasq.common.paths import LIBXKBCOMMON_PATH

XKB_COMPOSE_COMPOSED = 2
XKB_STATE_MODS_EFFECTIVE = 1 << 3
# Unknown layouts are a normal validation outcome, so keep the library quiet.
XKB_LOG_LEVEL_CRITICAL = 10
_KEYSYM_NAME_BUFFER = 64
_COMPOSE_BUFFER = 16
_MAX_MASKS_PER_LEVEL = 8
_EVDEV_KEYCODE_OFFSET = 8


class XkbUnavailableError(RuntimeError):
    """libxkbcommon could not be loaded."""


class _RuleNames(ctypes.Structure):
    _fields_ = [
        ("rules", ctypes.c_char_p),
        ("model", ctypes.c_char_p),
        ("layout", ctypes.c_char_p),
        ("variant", ctypes.c_char_p),
        ("options", ctypes.c_char_p),
    ]


_c_void = ctypes.c_void_p
_u32 = ctypes.c_uint32
_SIGNATURES: dict[str, tuple[list[object], object]] = {
    "xkb_context_new": ([ctypes.c_int], _c_void),
    "xkb_context_unref": ([_c_void], None),
    "xkb_context_set_log_level": ([_c_void, ctypes.c_int], None),
    "xkb_context_num_include_paths": ([_c_void], ctypes.c_uint),
    "xkb_context_include_path_get": ([_c_void, ctypes.c_uint], ctypes.c_char_p),
    "xkb_keymap_new_from_names": ([_c_void, ctypes.POINTER(_RuleNames), ctypes.c_int], _c_void),
    "xkb_keymap_unref": ([_c_void], None),
    "xkb_keymap_min_keycode": ([_c_void], _u32),
    "xkb_keymap_max_keycode": ([_c_void], _u32),
    "xkb_keymap_num_layouts_for_key": ([_c_void, _u32], _u32),
    "xkb_keymap_num_levels_for_key": ([_c_void, _u32, _u32], _u32),
    "xkb_keymap_key_get_syms_by_level": (
        [_c_void, _u32, _u32, _u32, ctypes.POINTER(ctypes.POINTER(_u32))],
        ctypes.c_int,
    ),
    "xkb_keymap_key_get_mods_for_level": (
        [_c_void, _u32, _u32, _u32, ctypes.POINTER(_u32), ctypes.c_size_t],
        ctypes.c_size_t,
    ),
    "xkb_keymap_num_mods": ([_c_void], _u32),
    "xkb_keymap_mod_get_name": ([_c_void, _u32], ctypes.c_char_p),
    "xkb_state_new": ([_c_void], _c_void),
    "xkb_state_unref": ([_c_void], None),
    "xkb_state_update_key": ([_c_void, _u32, ctypes.c_int], ctypes.c_int),
    "xkb_state_key_get_one_sym": ([_c_void, _u32], _u32),
    "xkb_state_serialize_mods": ([_c_void, ctypes.c_int], _u32),
    "xkb_state_mod_index_is_active": ([_c_void, _u32, ctypes.c_int], ctypes.c_int),
    "xkb_keysym_to_utf32": ([_u32], _u32),
    "xkb_keysym_get_name": ([_u32, ctypes.c_char_p, ctypes.c_size_t], ctypes.c_int),
    "xkb_compose_table_new_from_locale": ([_c_void, ctypes.c_char_p, ctypes.c_int], _c_void),
    "xkb_compose_table_unref": ([_c_void], None),
    "xkb_compose_state_new": ([_c_void, ctypes.c_int], _c_void),
    "xkb_compose_state_unref": ([_c_void], None),
    "xkb_compose_state_feed": ([_c_void, _u32], ctypes.c_int),
    "xkb_compose_state_get_status": ([_c_void], ctypes.c_int),
    "xkb_compose_state_get_utf8": ([_c_void, ctypes.c_char_p, ctypes.c_size_t], ctypes.c_int),
    "xkb_compose_state_reset": ([_c_void], None),
}
# libxkbcommon 1.6 added a way to walk the compose table. Older libraries fall
# back to probing dead key pairs through a compose state.
_OPTIONAL_SIGNATURES: dict[str, tuple[list[object], object]] = {
    "xkb_compose_table_iterator_new": ([_c_void], _c_void),
    "xkb_compose_table_iterator_free": ([_c_void], None),
    "xkb_compose_table_iterator_next": ([_c_void], _c_void),
    "xkb_compose_table_entry_sequence": (
        [_c_void, ctypes.POINTER(ctypes.c_size_t)],
        ctypes.POINTER(_u32),
    ),
    "xkb_compose_table_entry_utf8": ([_c_void], ctypes.c_char_p),
}


def _library_candidates() -> list[str]:
    # The soname goes through the dynamic loader directly. ctypes.util.find_library
    # is deliberately not used: it spawns gcc, ld, and ldconfig, which costs
    # hundreds of milliseconds on the GUI main thread and finds nothing the
    # loader would not.
    candidates = [
        os.environ.get("KEYMASQ_LIBXKBCOMMON_PATH", ""),
        str(LIBXKBCOMMON_PATH) if LIBXKBCOMMON_PATH else "",
        "libxkbcommon.so.0",
    ]
    return [candidate for candidate in candidates if candidate]


@cache
def load_library() -> ctypes.CDLL:
    errors: list[str] = []
    for candidate in _library_candidates():
        try:
            library = ctypes.CDLL(candidate)
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
            continue
        for name, (argtypes, restype) in _SIGNATURES.items():
            function = getattr(library, name)
            function.argtypes = argtypes
            function.restype = restype
        for name, (argtypes, restype) in _OPTIONAL_SIGNATURES.items():
            try:
                function = getattr(library, name)
            except AttributeError:
                continue
            function.argtypes = argtypes
            function.restype = restype
        return library
    raise XkbUnavailableError("libxkbcommon is not available: " + "; ".join(errors))


def is_available() -> bool:
    try:
        load_library()
    except XkbUnavailableError:
        return False
    return True


@dataclass(frozen=True)
class KeyLevel:
    """What one physical key produces at one shift level."""

    evdev_code: int
    level: int
    keysyms: tuple[int, ...]
    modifier_masks: tuple[frozenset[str], ...]


class Keymap:
    """A compiled keymap for one layout and variant, read through libxkbcommon."""

    def __init__(self, layout: str, variant: str = "") -> None:
        self._lib = load_library()
        self._context = self._lib.xkb_context_new(0)
        if not self._context:
            raise XkbUnavailableError("xkb_context_new failed")
        self._lib.xkb_context_set_log_level(self._context, XKB_LOG_LEVEL_CRITICAL)
        names = _RuleNames(
            rules=None,
            model=None,
            layout=layout.encode(),
            variant=variant.encode() if variant else None,
            options=None,
        )
        self._keymap = self._lib.xkb_keymap_new_from_names(self._context, ctypes.byref(names), 0)
        if not self._keymap:
            self._lib.xkb_context_unref(self._context)
            self._context = None
            raise ValueError(
                f"unknown keyboard layout: {layout}" + (f"({variant})" if variant else "")
            )
        self._mod_names = tuple(
            (self._lib.xkb_keymap_mod_get_name(self._keymap, index) or b"").decode()
            for index in range(self._lib.xkb_keymap_num_mods(self._keymap))
        )
        self._compose_table = None
        for locale in _compose_locales():
            table = self._lib.xkb_compose_table_new_from_locale(self._context, locale.encode(), 0)
            if table:
                self._compose_table = table
                break

    def close(self) -> None:
        if self._compose_table:
            self._lib.xkb_compose_table_unref(self._compose_table)
            self._compose_table = None
        if self._keymap:
            self._lib.xkb_keymap_unref(self._keymap)
            self._keymap = None
        if self._context:
            self._lib.xkb_context_unref(self._context)
            self._context = None

    def __enter__(self) -> Keymap:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _mask_names(self, mask: int) -> frozenset[str]:
        return frozenset(
            name for index, name in enumerate(self._mod_names) if mask & (1 << index) and name
        )

    def levels(self, layout_index: int = 0) -> Iterator[KeyLevel]:
        """Yield every (key, level) of the first layout group in the keymap."""
        lib = self._lib
        keymap = self._keymap
        syms = ctypes.POINTER(_u32)()
        masks = (_u32 * _MAX_MASKS_PER_LEVEL)()
        for keycode in range(
            lib.xkb_keymap_min_keycode(keymap), lib.xkb_keymap_max_keycode(keymap) + 1
        ):
            if keycode < _EVDEV_KEYCODE_OFFSET:
                continue
            if lib.xkb_keymap_num_layouts_for_key(keymap, keycode) <= layout_index:
                continue
            for level in range(lib.xkb_keymap_num_levels_for_key(keymap, keycode, layout_index)):
                count = lib.xkb_keymap_key_get_syms_by_level(
                    keymap, keycode, layout_index, level, ctypes.byref(syms)
                )
                if count <= 0:
                    continue
                mask_count = lib.xkb_keymap_key_get_mods_for_level(
                    keymap, keycode, layout_index, level, masks, _MAX_MASKS_PER_LEVEL
                )
                yield KeyLevel(
                    evdev_code=keycode - _EVDEV_KEYCODE_OFFSET,
                    level=level,
                    keysyms=tuple(syms[index] for index in range(count)),
                    modifier_masks=tuple(
                        self._mask_names(masks[index]) for index in range(mask_count)
                    ),
                )

    def modifier_keys(self) -> dict[int, frozenset[str]]:
        """Which modifiers each physical key sets while held.

        Keys whose modifier stays on after release (Caps Lock on most layouts,
        Num Lock) are left out: a macro cannot hold a latch. Which key is a
        plain modifier depends on the layout; Neo puts the third level on Caps
        Lock and the fifth on Right Alt.
        """
        lib = self._lib
        keymap = self._keymap
        keys: dict[int, frozenset[str]] = {}
        for keycode in range(
            max(_EVDEV_KEYCODE_OFFSET, lib.xkb_keymap_min_keycode(keymap)),
            lib.xkb_keymap_max_keycode(keymap) + 1,
        ):
            state = lib.xkb_state_new(keymap)
            if not state:
                raise XkbUnavailableError("xkb_state_new failed")
            try:
                lib.xkb_state_update_key(state, keycode, 1)
                if not lib.xkb_state_serialize_mods(state, XKB_STATE_MODS_EFFECTIVE):
                    continue
                names = frozenset(
                    name
                    for index, name in enumerate(self._mod_names)
                    if name and self._mod_active(state, index)
                )
                lib.xkb_state_update_key(state, keycode, 0)
                if lib.xkb_state_serialize_mods(state, XKB_STATE_MODS_EFFECTIVE):
                    continue
            finally:
                lib.xkb_state_unref(state)
            if names:
                keys[keycode - _EVDEV_KEYCODE_OFFSET] = names
        return keys

    def _mod_active(self, state: int, index: int) -> bool:
        return self._lib.xkb_state_mod_index_is_active(state, index, XKB_STATE_MODS_EFFECTIVE) > 0

    def keysym_for_chord(self, evdev_code: int, modifiers: tuple[int, ...]) -> int:
        """Return the keysym produced by a physical key chord."""
        state = self._lib.xkb_state_new(self._keymap)
        if not state:
            raise XkbUnavailableError("xkb_state_new failed")
        try:
            for modifier in modifiers:
                self._lib.xkb_state_update_key(state, modifier + _EVDEV_KEYCODE_OFFSET, 1)
            return int(
                self._lib.xkb_state_key_get_one_sym(
                    state,
                    evdev_code + _EVDEV_KEYCODE_OFFSET,
                )
            )
        finally:
            self._lib.xkb_state_unref(state)

    def compose_sequences(self, keysyms: Collection[int]) -> Iterator[tuple[tuple[int, ...], str]]:
        """Yield (keysym sequence, text) for every compose sequence made only of ``keysyms``.

        The sequences come from the locale's Compose table, the same one GTK
        and Qt apply when they receive the keys, so a dead key followed by a
        letter composes here exactly as it will in the target application.
        """
        if not self._compose_table:
            return
        producible = frozenset(keysyms)
        if hasattr(self._lib, "xkb_compose_table_iterator_new"):
            yield from self._walk_compose_table(producible)
        else:
            yield from self._probe_dead_key_pairs(producible)

    def _walk_compose_table(
        self, producible: frozenset[int]
    ) -> Iterator[tuple[tuple[int, ...], str]]:
        lib = self._lib
        iterator = lib.xkb_compose_table_iterator_new(self._compose_table)
        if not iterator:
            return
        try:
            length = ctypes.c_size_t()
            while True:
                entry = lib.xkb_compose_table_iterator_next(iterator)
                if not entry:
                    return
                syms = lib.xkb_compose_table_entry_sequence(entry, ctypes.byref(length))
                sequence = tuple(int(syms[index]) for index in range(length.value))
                if not sequence or any(keysym not in producible for keysym in sequence):
                    continue
                text = lib.xkb_compose_table_entry_utf8(entry)
                if text:
                    yield sequence, text.decode("utf-8", errors="replace")
        finally:
            lib.xkb_compose_table_iterator_free(iterator)

    def _probe_dead_key_pairs(
        self, producible: frozenset[int]
    ) -> Iterator[tuple[tuple[int, ...], str]]:
        """Two-key sequences only: every dead keysym followed by every producible keysym."""
        lib = self._lib
        dead_keysyms = [keysym for keysym in producible if keysym_name(keysym).startswith("dead_")]
        if not dead_keysyms:
            return
        state = lib.xkb_compose_state_new(self._compose_table, 0)
        if not state:
            return
        buffer = ctypes.create_string_buffer(_COMPOSE_BUFFER)
        try:
            for dead in dead_keysyms:
                for keysym in producible:
                    lib.xkb_compose_state_reset(state)
                    lib.xkb_compose_state_feed(state, dead)
                    lib.xkb_compose_state_feed(state, keysym)
                    if lib.xkb_compose_state_get_status(state) != XKB_COMPOSE_COMPOSED:
                        continue
                    if lib.xkb_compose_state_get_utf8(state, buffer, _COMPOSE_BUFFER) <= 0:
                        continue
                    text = buffer.value.decode("utf-8", errors="replace")
                    if text:
                        yield (dead, keysym), text
        finally:
            lib.xkb_compose_state_unref(state)


def _compose_locales() -> list[str]:
    """The user's locale first, so a personal ~/.XCompose or locale-specific
    table matches what their applications compose; "C" is the stock table."""
    locales: list[str] = []
    for variable in ("LC_ALL", "LC_CTYPE", "LANG"):
        value = os.environ.get(variable, "").strip()
        if value and value not in locales:
            locales.append(value)
    locales.append("C")
    return locales


def keysym_to_char(keysym: int) -> str | None:
    codepoint = load_library().xkb_keysym_to_utf32(keysym)
    return chr(codepoint) if codepoint else None


def keysym_name(keysym: int) -> str:
    buffer = ctypes.create_string_buffer(_KEYSYM_NAME_BUFFER)
    if load_library().xkb_keysym_get_name(keysym, buffer, _KEYSYM_NAME_BUFFER) <= 0:
        return ""
    return buffer.value.decode("ascii", errors="replace")


def include_paths() -> list[str]:
    """XKB data directories in the order libxkbcommon searches them."""
    lib = load_library()
    context = lib.xkb_context_new(0)
    if not context:
        return []
    try:
        return [
            (lib.xkb_context_include_path_get(context, index) or b"").decode()
            for index in range(lib.xkb_context_num_include_paths(context))
        ]
    finally:
        lib.xkb_context_unref(context)
