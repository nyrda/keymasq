from keymasq.keymasqd.daemon_helpers import float_like, int_like, str_value


def test_int_like_uses_default_for_none_and_empty_string() -> None:
    assert int_like(None) == 0
    assert int_like(None, 7) == 7
    assert int_like("", 7) == 7
    assert int_like("3", 7) == 3


def test_float_like_uses_default_for_none_and_empty_string() -> None:
    assert float_like(None) == 0.0
    assert float_like(None, 1.5) == 1.5
    assert float_like("", 1.5) == 1.5
    assert float_like("2.25", 1.5) == 2.25


def test_str_value_preserves_falsy_non_none_values() -> None:
    assert str_value(0) == "0"
    assert str_value(False) == "False"
    assert str_value([]) == "[]"
    assert str_value("") == ""


def test_str_value_uses_default_for_none_only() -> None:
    assert str_value(None, "fallback") == "fallback"
