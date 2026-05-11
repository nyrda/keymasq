from keymasq.keymasqd.daemon_helpers import str_value


def test_str_value_preserves_falsy_non_none_values() -> None:
    assert str_value(0) == "0"
    assert str_value(False) == "False"
    assert str_value([]) == "[]"
    assert str_value("") == ""


def test_str_value_uses_default_for_none_only() -> None:
    assert str_value(None, "fallback") == "fallback"
