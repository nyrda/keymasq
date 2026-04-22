#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RewriteRule:
    path: Path
    pattern: re.Pattern[str]
    replacement: str
    count: int = 1


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _rewrite_file(rule: RewriteRule) -> bool:
    content = rule.path.read_text(encoding="utf-8")
    updated, replacements = rule.pattern.subn(rule.replacement, content, count=rule.count)
    if replacements != rule.count:
        raise RuntimeError(
            f"expected {rule.count} replacement(s) in {rule.path}, got {replacements}"
        )
    if updated == content:
        return False
    rule.path.write_text(updated, encoding="utf-8")
    return True


def _build_rules(
    root: Path,
    python_version: str | None,
    debian_version: str | None,
    rpm_version: str | None,
) -> list[RewriteRule]:
    rules: list[RewriteRule] = []

    if python_version is not None:
        rules.append(
            RewriteRule(
                root / "pyproject.toml",
                re.compile(r'(?m)^version = "[^"]+"$'),
                f'version = "{python_version}"',
            )
        )

    if debian_version is not None:
        rules.append(
            RewriteRule(
                root / "debian/changelog",
                re.compile(r"(?m)^keymasq \([^)]+\)"),
                f"keymasq ({debian_version})",
            )
        )

    if rpm_version is not None:
        rules.append(
            RewriteRule(
                root / "packaging/rpm/metadata.env",
                re.compile(r'(?m)^VERSION=".*"$'),
                f'VERSION="{rpm_version}"',
            )
        )

    return rules


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rewrite build metadata for CI-only package version overrides."
    )
    parser.add_argument("--python-version", help="PEP 440 version for pyproject.toml")
    parser.add_argument("--debian-version", help="Package version for debian/changelog")
    parser.add_argument(
        "--rpm-version", help="Package version for packaging/rpm/metadata.env"
    )
    args = parser.parse_args()

    if not any((args.python_version, args.debian_version, args.rpm_version)):
        parser.error("at least one version override must be provided")

    root = _repo_root()
    changed_paths: list[Path] = []
    for rule in _build_rules(
        root,
        python_version=args.python_version,
        debian_version=args.debian_version,
        rpm_version=args.rpm_version,
    ):
        if _rewrite_file(rule):
            changed_paths.append(rule.path.relative_to(root))

    if changed_paths:
        for path in changed_paths:
            print(f"Updated: {path}")
    else:
        print("No build metadata changes were needed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
