import re
import sys
from pathlib import Path
from typing import NoReturn

import pytest

from tests.script_loader import load_script

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts/rewrite-build-metadata.py"
)
SCRIPT_MODULE = "rewrite_build_metadata_test"
SCRIPT_CLEANUP_MODULES = ("build_metadata_rewrite",)


def _fail_build_rules(*_args: object, **_kwargs: object) -> NoReturn:
    pytest.fail("_build_rules should not be called for invalid version input")


def test_rewrite_build_metadata_apply_rules_limits_duplicate_matches(
    tmp_path: Path,
) -> None:
    script = load_script(
        SCRIPT_PATH,
        SCRIPT_MODULE,
        cleanup_modules=SCRIPT_CLEANUP_MODULES,
    )
    target = tmp_path / "metadata.env"
    target.write_text('VERSION="1.0.0"\nVERSION="1.0.0"\n', encoding="utf-8")
    rule = script.RewriteRule(
        target,
        re.compile(r'(?m)^VERSION=".*"$'),
        'VERSION="2.0.0"',
    )

    changed_paths = script.apply_rules(tmp_path, [rule])

    assert changed_paths == [Path("metadata.env")]
    assert target.read_text(encoding="utf-8") == (
        'VERSION="2.0.0"\nVERSION="1.0.0"\n'
    )


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
    script = load_script(
        SCRIPT_PATH,
        SCRIPT_MODULE,
        cleanup_modules=SCRIPT_CLEANUP_MODULES,
    )
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
    script = load_script(
        SCRIPT_PATH,
        SCRIPT_MODULE,
        cleanup_modules=SCRIPT_CLEANUP_MODULES,
    )
    validator = getattr(script, validator_name)

    assert validator(value) == value


def test_rewrite_build_metadata_uses_shared_metadata_rules(tmp_path: Path) -> None:
    script = load_script(
        SCRIPT_PATH,
        SCRIPT_MODULE,
        cleanup_modules=SCRIPT_CLEANUP_MODULES,
    )
    (tmp_path / "keymasq").mkdir()
    (tmp_path / "debian").mkdir()
    (tmp_path / "packaging/rpm").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('version = "0.1.0"\n', encoding="utf-8")
    (tmp_path / "keymasq/_version.py").write_text(
        '__version__ = "0.1.0"\n', encoding="utf-8"
    )
    (tmp_path / "debian/changelog").write_text(
        "keymasq (0.1.0-1) unstable; urgency=medium\n", encoding="utf-8"
    )
    (tmp_path / "packaging/rpm/metadata.env").write_text(
        'VERSION="0.1.0"\n', encoding="utf-8"
    )

    rules = script._build_rules(
        tmp_path,
        python_version="0.2.0+ci.1",
        debian_version="0.2.0~ci-1",
        rpm_version="0.2.0_ci",
    )

    assert [rule.path.relative_to(tmp_path) for rule in rules] == [
        Path("pyproject.toml"),
        Path("keymasq/_version.py"),
        Path("debian/changelog"),
        Path("packaging/rpm/metadata.env"),
    ]
    assert script.apply_rules(tmp_path, rules) == [
        Path("pyproject.toml"),
        Path("keymasq/_version.py"),
        Path("debian/changelog"),
        Path("packaging/rpm/metadata.env"),
    ]

    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == (
        'version = "0.2.0+ci.1"\n'
    )
    assert (tmp_path / "keymasq/_version.py").read_text(encoding="utf-8") == (
        '__version__ = "0.2.0+ci.1"\n'
    )
    assert (tmp_path / "debian/changelog").read_text(encoding="utf-8") == (
        "keymasq (0.2.0~ci-1) unstable; urgency=medium\n"
    )
    assert (tmp_path / "packaging/rpm/metadata.env").read_text(encoding="utf-8") == (
        'VERSION="0.2.0_ci"\n'
    )
