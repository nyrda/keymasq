import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import NoReturn

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts/rewrite-build-metadata.py"
)


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rewrite_build_metadata_test", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _fail_build_rules(*_args: object, **_kwargs: object) -> NoReturn:
    pytest.fail("_build_rules should not be called for invalid version input")


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--python-version", '1.2.3"\nBROKEN = "x'),
        ("--debian-version", "1.2.3)\nkeymasq (9.9.9)"),
        ("--rpm-version", '1.2.3"\nRELEASE="99'),
        ("--rpm-version", "1.2.3$(touch bad)"),
    ],
)
def test_rewrite_build_metadata_rejects_malformed_versions_before_rewrite(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    option: str,
    value: str,
) -> None:
    script = _load_script()
    monkeypatch.setattr(script, "_build_rules", _fail_build_rules)
    monkeypatch.setattr(sys, "argv", [SCRIPT_PATH.name, option, value])

    with pytest.raises(SystemExit) as excinfo:
        script.main()

    assert excinfo.value.code == 2
    assert "invalid version format" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("validator_name", "value"),
    [
        ("_validate_python_version", "1.2.3rc1"),
        ("_validate_python_version", "1.2.3.dev4"),
        ("_validate_debian_version", "1.2.3~rc1"),
        ("_validate_rpm_version", "1.2.3~rc1"),
    ],
)
def test_rewrite_build_metadata_accepts_ci_version_suffixes(
    validator_name: str,
    value: str,
) -> None:
    script = _load_script()
    validator = getattr(script, validator_name)

    assert validator(value) == value
