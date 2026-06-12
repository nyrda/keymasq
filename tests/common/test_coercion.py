import pytest

import keymasq.common.coercion as coercion
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


def test_coerce_str_can_use_none_as_default() -> None:
    assert coerce_str(None, None) is None
    assert coerce_str("") == ""
    assert coerce_str("  ") == "  "


def test_coercion_helpers_do_not_expose_policy_switches() -> None:
    assert coercion.coerce_str.__kwdefaults__ is None
    assert coercion.coerce_int.__kwdefaults__ is None
    assert coercion.coerce_float.__kwdefaults__ is None


def test_public_coercion_api_has_one_helper_per_policy() -> None:
    assert set(coercion.__all__) == {
        "bool_value",
        "coerce_bool",
        "coerce_float",
        "coerce_int",
        "coerce_str",
        "json_list",
        "json_object",
        "json_object_or_empty",
        "require_json_object",
    }


def test_coerce_int_with_none_default_rejects_bool() -> None:
    assert coerce_int(True, None) is None
    assert coerce_int("bad", None) is None


def test_legacy_coercion_policy_names_are_not_exported() -> None:
    assert not hasattr(coercion, "NumberCoercionPolicy")
    assert not hasattr(coercion, "StringCoercionPolicy")
    assert not hasattr(coercion, "str_from_optional")
    assert not hasattr(coercion, "strict_int_from_optional")
    assert not hasattr(coercion, "strict_float_from_optional")
    assert not hasattr(coercion, "optional_str")
    assert not hasattr(coercion, "int_or_default")
    assert not hasattr(coercion, "float_or_default")
    assert not hasattr(coercion, "int_or_none")
    assert not hasattr(coercion, "str_value")
    assert not hasattr(coercion, "int_value")
    assert not hasattr(coercion, "float_value")
    assert not hasattr(coercion, "int_like")
    assert not hasattr(coercion, "float_like")
    assert not hasattr(coercion, "int_from_optional")
    assert not hasattr(coercion, "float_from_optional")
    assert not hasattr(coercion, "int_or_none_allow_bool")
    assert not hasattr(coercion, "str_or_none")
    assert not hasattr(coercion, "coerce_float_or_default")
    assert not hasattr(coercion, "coerce_int_or_default")
    assert not hasattr(coercion, "coerce_int_or_none")
    assert not hasattr(coercion, "coerce_optional_str")
    assert not hasattr(coercion, "coerce_str_or_none")
    assert not hasattr(coercion, "require_int")
    assert not hasattr(coercion, "require_float")
    assert not hasattr(coercion, "parse_int_field")
    assert not hasattr(coercion, "parse_float_field")
