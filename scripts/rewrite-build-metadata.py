#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from build_metadata_rewrite import (
    RewriteRule,
    apply_rules,
    debian_changelog_version_rule,
    python_metadata_rules,
    repo_root,
    rpm_metadata_rule,
)

PEP440_VERSION_RE = re.compile(
    r"""
    ^
    (?:[1-9][0-9]*!)?
    [0-9]+(?:\.[0-9]+)*
    (?:(?:a|b|rc)[0-9]+)?
    (?:\.post[0-9]+)?
    (?:\.dev[0-9]+)?
    (?:\+[a-z0-9]+(?:[._-][a-z0-9]+)*)?
    $
    """,
    re.IGNORECASE | re.VERBOSE,
)
DEBIAN_VERSION_RE = re.compile(
    r"^(?:[0-9]+:)?[0-9][A-Za-z0-9.+:~]*(?:-[A-Za-z0-9+.~]+)?$"
)
RPM_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~^]*$")


def _validate_version(value: str, pattern: re.Pattern[str], description: str) -> str:
    if not pattern.fullmatch(value):
        raise argparse.ArgumentTypeError(f"{description} has an invalid version format")
    return value


def _validate_python_version(value: str) -> str:
    return _validate_version(value, PEP440_VERSION_RE, "python-version")


def _validate_debian_version(value: str) -> str:
    return _validate_version(value, DEBIAN_VERSION_RE, "debian-version")


def _validate_rpm_version(value: str) -> str:
    return _validate_version(value, RPM_VERSION_RE, "rpm-version")


def _build_rules(
    root: Path,
    python_version: str | None,
    debian_version: str | None,
    rpm_version: str | None,
) -> list[RewriteRule]:
    rules: list[RewriteRule] = []

    if python_version is not None:
        rules.extend(python_metadata_rules(root, python_version))

    if debian_version is not None:
        rules.append(debian_changelog_version_rule(root, debian_version))

    if rpm_version is not None:
        rules.append(rpm_metadata_rule(root, rpm_version))

    return rules


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rewrite build metadata for CI-only package version overrides."
    )
    parser.add_argument(
        "--python-version",
        type=_validate_python_version,
        help="PEP 440 version for pyproject.toml",
    )
    parser.add_argument(
        "--debian-version",
        type=_validate_debian_version,
        help="Package version for debian/changelog",
    )
    parser.add_argument(
        "--rpm-version",
        type=_validate_rpm_version,
        help="Package version for packaging/rpm/metadata.env",
    )
    args = parser.parse_args()

    if not any((args.python_version, args.debian_version, args.rpm_version)):
        parser.error("at least one version override must be provided")

    root = repo_root()
    changed_paths = apply_rules(
        root,
        _build_rules(
            root,
            python_version=args.python_version,
            debian_version=args.debian_version,
            rpm_version=args.rpm_version,
        ),
    )

    if changed_paths:
        for path in changed_paths:
            print(f"Updated: {path}")
    else:
        print("No build metadata changes were needed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
