#!/usr/bin/env python3

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


@dataclass(frozen=True)
class RewriteRule:
    path: Path
    pattern: re.Pattern[str]
    replacement: str
    count: int = 1


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _build_rules(root: Path, version: str) -> list[RewriteRule]:
    return [
        RewriteRule(
            root / "pyproject.toml",
            re.compile(r'(?m)^version = "[^"]+"$'),
            f'version = "{version}"',
        ),
        RewriteRule(
            root / "nfpm.yaml",
            re.compile(r"(?m)^version: .+$"),
            f"version: {version}",
        ),
        RewriteRule(
            root / "flake.nix",
            re.compile(r'(?m)^ {10}version = "[^"]+";$'),
            f'          version = "{version}";',
        ),
        RewriteRule(
            root / "debian/changelog",
            re.compile(r"(?m)^keyforge \(([^-]+)(-\d+)\)"),
            rf"keyforge ({version}\2)",
        ),
        RewriteRule(
            root / "CHANGELOG.md",
            re.compile(r"(?m)^## \[[^\]]+\] - "),
            f"## [{version}] - ",
        ),
        RewriteRule(
            root / "PKGBUILD",
            re.compile(r"(?m)^pkgver=.+$"),
            f"pkgver={version}",
        ),
    ]


def _rewrite_file(rule: RewriteRule, dry_run: bool) -> bool:
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update the Keyforge release version across packaging files."
    )
    parser.add_argument("version", help="Version to set, for example 0.1.1")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check and report file updates without writing changes",
    )
    args = parser.parse_args()

    version = str(args.version).strip()
    if not VERSION_RE.fullmatch(version):
        parser.error("version must use the form X.Y.Z")

    root = _repo_root()
    changed_paths: list[Path] = []
    for rule in _build_rules(root, version):
        if _rewrite_file(rule, dry_run=args.dry_run):
            changed_paths.append(rule.path.relative_to(root))

    action = "Would update" if args.dry_run else "Updated"
    if changed_paths:
        for path in changed_paths:
            print(f"{action}: {path}")
    else:
        print("No version changes were needed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
