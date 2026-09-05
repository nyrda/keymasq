import pytest

from keymasq.common.coercion import (
    coerce_bool,
    coerce_float,
    coerce_int,
    coerce_str,
)


def test_coerce_int_uses_default_for_missing_or_invalid_values() -> None:
    assert coerce_int(None) == 0
    assert coerce_int(None, 7) == 7
    assert coerce_int("", 7) == 7
    assert coerce_int("bad", 7) == 7
    assert coerce_int([], 7) == 7
    assert coerce_int(True, 7) == 7
    assert coerce_int("3", 7) == 3


def test_coerce_float_uses_default_for_missing_or_invalid_values() -> None:
    assert coerce_float(None) == 0.0
    assert coerce_float(None, 1.5) == 1.5
    assert coerce_float("", 1.5) == 1.5
    assert coerce_float("bad", 1.5) == 1.5
    assert coerce_float(True, 1.5) == 1.5
    assert coerce_float("2.25", 1.5) == 2.25


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        "nan",
        "inf",
        "-inf",
    ],
)
def test_coerce_float_uses_default_for_non_finite_values(value: object) -> None:
    assert coerce_float(value, 1.5) == 1.5
    assert coerce_float(value, None) is None


def test_coerce_bool_parses_common_boolean_values() -> None:
    assert coerce_bool(True) is True
    assert coerce_bool(False, True) is False
    assert coerce_bool(1) is True
    assert coerce_bool(0, True) is False
    assert coerce_bool("true") is True
    assert coerce_bool("YES") is True
    assert coerce_bool("on") is True
    assert coerce_bool("false", True) is False
    assert coerce_bool("0", True) is False
    assert coerce_bool("off", True) is False
    assert coerce_bool("unknown", True) is True
    assert coerce_bool("", True) is True


def test_coerce_bool_strict_rejects_unrecognized_values() -> None:
    with pytest.raises(ValueError, match="Unrecognized boolean value"):
        coerce_bool("unknown", strict=True)


def test_coerce_str_preserves_falsy_non_none_values() -> None:
    assert coerce_str(0) == "0"
    assert coerce_str(False) == "False"
    assert coerce_str([]) == "[]"
    assert coerce_str("") == ""


def test_coerce_str_uses_default_for_none_only() -> None:
    assert coerce_str(None, "fallback") == "fallback"
    assert coerce_str(None, None) is None
    assert coerce_str("") == ""
    assert coerce_str("  ") == "  "


def test_coerce_int_with_none_default_rejects_bool() -> None:
    assert coerce_int(True, None) is None
    assert coerce_int("bad", None) is None
