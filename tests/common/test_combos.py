from keymasq.common.combos import normalize_combo_evdev, normalize_combo_restore_keys


def test_normalize_combo_evdev_canonicalizes_gamepad_aliases() -> None:
    assert normalize_combo_evdev("btn_a") == "btn_south"
    assert normalize_combo_evdev("BTN_X") == "btn_north"
    assert normalize_combo_evdev("btn_south") == "btn_south"


def test_normalize_combo_evdev_still_normalizes_generic_modifiers() -> None:
    assert normalize_combo_evdev("key_leftctrl") == "ctrl"
    assert normalize_combo_evdev("key_rightalt") == "alt"


def test_normalize_combo_restore_keys_canonicalizes_gamepad_aliases() -> None:
    assert normalize_combo_restore_keys(["btn_a", "btn_south", "key_a"]) == [
        "btn_south",
        "key_a",
    ]
