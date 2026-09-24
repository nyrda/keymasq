import evdev
import pytest

from keymasq.common import xkb
from keymasq.common.keyboard_layouts import (
    DEFAULT_KEYBOARD_LAYOUT,
    KeyboardLayoutError,
    KeyLegend,
    TypedKey,
    is_known_keyboard_layout,
    key_legends,
    keyboard_layout,
    keyboard_layout_choices,
    keyboard_layout_name,
    normalize_keyboard_layout_id,
    parse_keyboard_layout_id,
    unmodified_key_outputs,
)

K = evdev.ecodes
requires_xkb = pytest.mark.skipif(not xkb.is_available(), reason="libxkbcommon unavailable")


def test_parse_keyboard_layout_id_accepts_layout_and_variant() -> None:
    assert parse_keyboard_layout_id(" de(nodeadkeys) ") == ("de", "nodeadkeys")
    assert parse_keyboard_layout_id("ua(macOS)") == ("ua", "macOS")
    assert parse_keyboard_layout_id("us") == ("us", "")
    for malformed in ("", "de,us", "de(", "us dvorak", 5):
        with pytest.raises(KeyboardLayoutError):
            parse_keyboard_layout_id(malformed)


@requires_xkb
def test_us_layout_resolves_shift_pairs_and_whitespace() -> None:
    layout = keyboard_layout("us")
    assert layout.name == "English (US)"
    assert layout.key_for("a") == TypedKey(K.KEY_A)
    assert layout.key_for("A") == TypedKey(K.KEY_A, (K.KEY_LEFTSHIFT,))
    assert layout.key_for("!") == TypedKey(K.KEY_1, (K.KEY_LEFTSHIFT,))
    assert layout.key_for("+") == TypedKey(K.KEY_EQUAL, (K.KEY_LEFTSHIFT,))
    assert layout.key_for("\n") == TypedKey(K.KEY_ENTER)
    assert layout.key_for("\t") == TypedKey(K.KEY_TAB)
    assert layout.key_for(" ") == TypedKey(K.KEY_SPACE)
    assert layout.key_for("é") is None


@requires_xkb
def test_german_layout_uses_altgr_and_dead_keys() -> None:
    layout = keyboard_layout("de")
    assert layout.key_for("z") == TypedKey(K.KEY_Y)
    assert layout.key_for("ä") == TypedKey(K.KEY_APOSTROPHE)
    assert layout.key_for("@") == TypedKey(K.KEY_Q, (K.KEY_RIGHTALT,))
    assert layout.key_for("€") == TypedKey(K.KEY_E, (K.KEY_RIGHTALT,))
    # The dead key's own character comes from the compose table: dead twice.
    assert layout.key_for("^") == TypedKey(K.KEY_GRAVE, (), (TypedKey(K.KEY_GRAVE),))
    assert layout.key_for("ê") == TypedKey(K.KEY_E, (), (TypedKey(K.KEY_GRAVE),))
    assert keyboard_layout("de(nodeadkeys)").key_for("^") == TypedKey(K.KEY_GRAVE)


@requires_xkb
def test_unmodified_key_outputs_use_the_physical_code_and_layout() -> None:
    assert unmodified_key_outputs("de")[K.KEY_LEFTBRACE] == "ü"
    assert unmodified_key_outputs("us")[K.KEY_LEFTBRACE] == "["
    assert unmodified_key_outputs("fr")[K.KEY_LEFTBRACE] == "dead circumflex"
    assert K.KEY_LEFTCTRL not in unmodified_key_outputs("de")


@requires_xkb
def test_key_legends_label_physical_keys_like_keycaps() -> None:
    # Letters show their capital; other keys show the shifted character too.
    assert key_legends("de")[K.KEY_Y] == KeyLegend("Z")
    assert key_legends("de")[K.KEY_SEMICOLON] == KeyLegend("Ö")
    assert key_legends("de")[K.KEY_MINUS] == KeyLegend("ß", "?")
    assert key_legends("us")[K.KEY_1] == KeyLegend("1", "!")
    assert key_legends("fr")[K.KEY_Q] == KeyLegend("A")
    assert key_legends("fr")[K.KEY_1] == KeyLegend("&", "1")
    assert key_legends("ru")[K.KEY_Q] == KeyLegend("Й")
    # Turkish capitals follow the layout, not Python's casing of i and ı.
    assert key_legends("tr")[K.KEY_APOSTROPHE] == KeyLegend("İ")
    assert key_legends("tr")[K.KEY_I] == KeyLegend("I")
    # Dead keys show their spacing accent.
    assert key_legends("de")[K.KEY_EQUAL] == KeyLegend("´", "`")
    assert key_legends("de")[K.KEY_GRAVE] == KeyLegend("^", "°")
    # Keys that type no character keep their fixed names.
    assert K.KEY_SPACE not in key_legends("de")
    assert K.KEY_LEFTCTRL not in key_legends("de")


def test_key_legends_reject_unknown_layouts() -> None:
    with pytest.raises(KeyboardLayoutError):
        key_legends("de,us")


@requires_xkb
def test_dead_key_sequences_come_from_the_compose_table() -> None:
    # French: e-circumflex has no key; dead_circumflex (the [ key) then e.
    assert keyboard_layout("fr").key_for("ê") == TypedKey(K.KEY_E, (), (TypedKey(K.KEY_LEFTBRACE),))
    assert keyboard_layout("fr").key_for("ï") == TypedKey(
        K.KEY_I, (), (TypedKey(K.KEY_LEFTBRACE, (K.KEY_LEFTSHIFT,)),)
    )
    # Spanish: dead_acute (the ' key) then the vowel; n-tilde is a direct key.
    assert keyboard_layout("es").key_for("á") == TypedKey(
        K.KEY_A, (), (TypedKey(K.KEY_APOSTROPHE),)
    )
    assert keyboard_layout("es").key_for("ñ") == TypedKey(K.KEY_SEMICOLON)
    # US International: e-acute is AltGr+e directly; e-circumflex needs Shift+6 first.
    assert keyboard_layout("us(intl)").key_for("é") == TypedKey(K.KEY_E, (K.KEY_RIGHTALT,))
    assert keyboard_layout("us(intl)").key_for("ê") == TypedKey(
        K.KEY_E, (), (TypedKey(K.KEY_6, (K.KEY_LEFTSHIFT,)),)
    )
    # A direct key always beats a compose sequence for the same character.
    assert keyboard_layout("fr").key_for("é") == TypedKey(K.KEY_2)


@requires_xkb
def test_compose_pair_probe_matches_compose_table_walk() -> None:
    """The fallback for libxkbcommon < 1.6 finds the same two-key sequences."""
    with xkb.Keymap("fr") as keymap:
        if not hasattr(keymap._lib, "xkb_compose_table_iterator_new"):
            pytest.skip("compose table iterator unavailable; the probe is the only path")
        producible = frozenset(
            level.keysyms[0] for level in keymap.levels() if len(level.keysyms) == 1
        )
        walked = {
            sequence: text
            for sequence, text in keymap._walk_compose_table(producible)
            if len(sequence) == 2 and xkb.keysym_name(sequence[0]).startswith("dead_")
        }
        probed = dict(keymap._probe_dead_key_pairs(producible))
    assert probed == walked
    assert any(text == "ê" for text in probed.values())


@requires_xkb
def test_french_layout_shifts_digits_and_dvorak_moves_letters() -> None:
    assert keyboard_layout("fr").key_for("1") == TypedKey(K.KEY_1, (K.KEY_LEFTSHIFT,))
    assert keyboard_layout("fr").key_for("a") == TypedKey(K.KEY_Q)
    assert keyboard_layout("us(dvorak)").key_for("u") == TypedKey(K.KEY_F)


@requires_xkb
def test_layout_rejects_levels_the_selected_physical_modifiers_cannot_reach() -> None:
    assert keyboard_layout("ru").key_for("₽") is None
    assert keyboard_layout("us(dvorak)").key_for("·") is None
    assert keyboard_layout("us").key_for("¦") is None
    assert keyboard_layout("de").key_for("@") == TypedKey(K.KEY_Q, (K.KEY_RIGHTALT,))


@requires_xkb
def test_layout_validation_and_normalization() -> None:
    assert is_known_keyboard_layout("de")
    assert not is_known_keyboard_layout("DE")
    assert not is_known_keyboard_layout("nonsense")
    assert not is_known_keyboard_layout("de,us")
    assert normalize_keyboard_layout_id(" de(nodeadkeys) ") == "de(nodeadkeys)"
    # Variant names keep their case; XKB looks them up case-sensitively.
    assert keyboard_layout("de(T3)").id == "de(T3)"
    # Normalization only trims; unusable ids are kept so their error surfaces.
    assert normalize_keyboard_layout_id("bogus") == "bogus"
    assert normalize_keyboard_layout_id(None) == DEFAULT_KEYBOARD_LAYOUT
    assert normalize_keyboard_layout_id("  ") == DEFAULT_KEYBOARD_LAYOUT
    assert keyboard_layout_name("de(nodeadkeys)") == "German (no dead keys)"
    assert keyboard_layout_name("bogus") == "bogus"


@requires_xkb
def test_layout_choices_come_from_xkb_rules() -> None:
    choices = dict(keyboard_layout_choices())
    assert choices["us"] == "English (US)"
    # Layouts from rules/evdev.extras.xml are offered too; they compile like the rest.
    assert choices["de(bone)"] == "German (Bone)"
    assert is_known_keyboard_layout("de(bone)")
    assert choices["de"] == "German"
    assert "German" in choices["de(nodeadkeys)"]
    assert "custom" not in choices


def test_unavailable_library_raises_layout_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from keymasq.common import keyboard_layouts

    def _unavailable(*_args: object, **_kwargs: object) -> None:
        raise xkb.XkbUnavailableError("no library")

    monkeypatch.setattr(xkb, "Keymap", _unavailable)
    keyboard_layouts._compile_layout.cache_clear()
    with pytest.raises(KeyboardLayoutError, match="no library"):
        keyboard_layout("xx(missing)")
    keyboard_layouts._compile_layout.cache_clear()


@requires_xkb
def test_modifier_keys_come_from_the_keymap() -> None:
    """Neo puts the third level on Caps Lock and the backslash key, and the
    fifth on Right Alt; the mapping is read from the keymap, not assumed."""
    with xkb.Keymap("de") as keymap:
        keys = keymap.modifier_keys()
    assert "LevelThree" in keys[K.KEY_RIGHTALT]
    assert "Shift" in keys[K.KEY_LEFTSHIFT]
    assert K.KEY_CAPSLOCK not in keys  # a latch, not a modifier a macro can hold

    with xkb.Keymap("de", "neo") as keymap:
        keys = keymap.modifier_keys()
    assert "LevelThree" in keys[K.KEY_CAPSLOCK]
    assert "LevelThree" in keys[K.KEY_BACKSLASH]
    assert "LevelFive" in keys[K.KEY_RIGHTALT]


@requires_xkb
@pytest.mark.parametrize("layout_id", ["de(neo)", "de(bone)"])
def test_neo_family_punctuation_is_typed_directly(layout_id: str) -> None:
    layout = keyboard_layout(layout_id)
    for char in "[]@_{}?":
        key = layout.key_for(char)
        assert key is not None, char
        assert key.dead_keys == ()
        assert key.modifiers == (K.KEY_BACKSLASH,), char
