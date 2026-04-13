#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


@dataclass(frozen=True)
class RewriteRule:
    path: Path
    pattern: re.Pattern[str]
    replacement: str
    count: int = 1


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _release_date() -> str:
    return datetime.now(tz=UTC).date().isoformat()


def _debian_timestamp() -> str:
    return datetime.now(tz=UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")


def _current_version(root: Path) -> str:
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    version = project["version"]
    if not isinstance(version, str):
        raise TypeError("project.version must be a string")
    return version


def _build_rules(
    root: Path, version: str, release_date: str, current_version: str
) -> list[RewriteRule]:
    rules = [
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
            re.compile(r"(?m)^## \[[^\]]+\] - .+$"),
            f"## [{version}] - {release_date}",
        ),
        RewriteRule(
            root / "keyforge/gui/application.py",
            re.compile(r'(?m)^APP_VERSION = "[^"]+"$'),
            f'APP_VERSION = "{version}"',
        ),
        RewriteRule(
            root / "assets/keyforge.metainfo.xml",
            re.compile(r'(?m)^ {4}<release version="[^"]+" date="[^"]+">$'),
            f'    <release version="{version}" date="{release_date}">',
        ),
    ]
    if current_version != version:
        rules.append(
            RewriteRule(
                root / "debian/changelog",
                re.compile(r"(?m)^ -- nyrda <nyrda@keyforge.tools>  .+$"),
                f" -- nyrda <nyrda@keyforge.tools>  {_debian_timestamp()}",
            )
        )
    return rules


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


def _load_pacman_render(root: Path) -> ModuleType:
    render_path = root / "packaging/pacman/render.py"
    spec = importlib.util.spec_from_file_location("_keyforge_pacman_render", render_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load pacman renderer from {render_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render_pacman_outputs(root: Path, version: str, dry_run: bool) -> list[Path]:
    module = _load_pacman_render(root)
    aur_source_url = (
        f"{module.COMMON_METADATA['url']}/releases/download/v{version}/keyforge-{version}.tar.gz"
    )
    aur_sha256 = "SKIP"
    local_replacements = module.build_replacements(
        version, "local", aur_source_url, aur_sha256
    )
    aur_replacements = module.build_replacements(version, "aur", aur_source_url, aur_sha256)
    outputs = {
        root / "PKGBUILD": module.render_template("PKGBUILD.in", local_replacements),
        root / "keyforge.install": module.render_template(
            "keyforge.install.in", local_replacements
        ),
        root / "packaging/aur/PKGBUILD": module.render_template(
            "PKGBUILD.in", aur_replacements
        ),
        root / "packaging/aur/keyforge.install": module.render_template(
            "keyforge.install.in", aur_replacements
        ),
        root / "packaging/aur/.SRCINFO": module.build_srcinfo(
            version, aur_source_url, aur_sha256
        ),
    }

    changed_paths: list[Path] = []
    for path, content in outputs.items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == content:
            continue
        changed_paths.append(path.relative_to(root))
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    return changed_paths


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Update the Keyforge release version across maintained version files "
            "and regenerate pacman packaging outputs."
        )
    )
    parser.add_argument("version", help="Version to set, for example 0.1.1")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check and report file updates without writing changes",
    )
    parser.add_argument(
        "--release-date",
        default=_release_date(),
        help="Release date to stamp into changelog and metainfo files (YYYY-MM-DD).",
    )
    args = parser.parse_args()

    version = str(args.version).strip()
    if not VERSION_RE.fullmatch(version):
        parser.error("version must use the form X.Y.Z")
    release_date = str(args.release_date).strip()
    try:
        datetime.strptime(release_date, "%Y-%m-%d")
    except ValueError as exc:
        parser.error(f"release date must use the form YYYY-MM-DD: {exc}")

    root = _repo_root()
    current_version = _current_version(root)
    changed_paths: list[Path] = []
    for rule in _build_rules(root, version, release_date, current_version):
        if _rewrite_file(rule, dry_run=args.dry_run):
            changed_paths.append(rule.path.relative_to(root))
    changed_paths.extend(_render_pacman_outputs(root, version, dry_run=args.dry_run))
    changed_paths = list(dict.fromkeys(changed_paths))

    action = "Would update" if args.dry_run else "Updated"
    if changed_paths:
        for path in changed_paths:
            print(f"{action}: {path}")
    else:
        print("No version changes were needed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
