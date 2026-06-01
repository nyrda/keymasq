from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

PYPROJECT_VERSION_LINE_RE = re.compile(r'(?m)^version = "[^"]+"$')
PYTHON_VERSION_LINE_RE = re.compile(r'(?m)^__version__ = "[^"]+"$')
DEBIAN_CHANGELOG_VERSION_RE = re.compile(r"(?m)^keymasq \([^)]+\)")
DEBIAN_CHANGELOG_UPSTREAM_VERSION_RE = re.compile(r"(?m)^keymasq \(([^-]+)(-\d+)\)")
RPM_METADATA_VERSION_LINE_RE = re.compile(r'(?m)^VERSION=".*"$')


@dataclass(frozen=True)
class RewriteRule:
    path: Path
    pattern: re.Pattern[str]
    replacement: str
    count: int = 1


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def rewrite_file(rule: RewriteRule, *, dry_run: bool = False) -> bool:
    content = rule.path.read_text(encoding="utf-8")
    updated, replacements = rule.pattern.subn(rule.replacement, content, count=rule.count)
    if replacements != rule.count:
        raise RuntimeError(
            f"expected {rule.count} replacement(s) in {rule.path}, got {replacements}"
        )
    if updated == content:
        return False
    if not dry_run:
        rule.path.write_text(updated, encoding="utf-8")
    return True


def apply_rules(
    root: Path, rules: Iterable[RewriteRule], *, dry_run: bool = False
) -> list[Path]:
    changed_paths: list[Path] = []
    for rule in rules:
        if rewrite_file(rule, dry_run=dry_run):
            changed_paths.append(rule.path.relative_to(root))
    return changed_paths


def python_metadata_rules(root: Path, version: str) -> list[RewriteRule]:
    return [
        RewriteRule(
            root / "pyproject.toml",
            PYPROJECT_VERSION_LINE_RE,
            f'version = "{version}"',
        ),
        RewriteRule(
            root / "keymasq/_version.py",
            PYTHON_VERSION_LINE_RE,
            f'__version__ = "{version}"',
        ),
    ]


def debian_changelog_version_rule(root: Path, version: str) -> RewriteRule:
    return RewriteRule(
        root / "debian/changelog",
        DEBIAN_CHANGELOG_VERSION_RE,
        f"keymasq ({version})",
    )


def debian_changelog_upstream_version_rule(root: Path, version: str) -> RewriteRule:
    return RewriteRule(
        root / "debian/changelog",
        DEBIAN_CHANGELOG_UPSTREAM_VERSION_RE,
        rf"keymasq ({version}\2)",
    )


def rpm_metadata_rule(root: Path, version: str) -> RewriteRule:
    return RewriteRule(
        root / "packaging/rpm/metadata.env",
        RPM_METADATA_VERSION_LINE_RE,
        f'VERSION="{version}"',
    )
