#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from build_metadata_rewrite import (
    RewriteRule,
    apply_rules,
    debian_changelog_upstream_version_rule,
    python_metadata_rules,
    repo_root,
    rpm_metadata_rule,
)

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
CHANGELOG_RELEASE_RE_TEMPLATE = r"(?m)^## {version}(?: - \d{{4}}-\d{{2}}-\d{{2}})?$"
METAINFO_RELEASE_LINE_RE_TEMPLATE = (
    r'(?m)^    <release version="{version}" date="[^"]+">$'
)
METAINFO_PLACEHOLDER_SUMMARY = "Update release notes."


def _release_date() -> str:
    return datetime.now(tz=UTC).date().isoformat()


def _debian_timestamp(release_date: str) -> str:
    timestamp = datetime.strptime(release_date, "%Y-%m-%d").replace(tzinfo=UTC)
    return timestamp.strftime("%a, %d %b %Y 00:00:00 +0000")


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
        *python_metadata_rules(root, version),
        rpm_metadata_rule(root, version),
        RewriteRule(
            root / "flake.nix",
            re.compile(r'(?m)^ {10}version = "[^"]+";$'),
            f'          version = "{version}";',
        ),
        debian_changelog_upstream_version_rule(root, version),
    ]
    if current_version != version:
        rules.append(
            RewriteRule(
                root / "debian/changelog",
                re.compile(r"(?m)^ -- nyrda <nyrda@keymasq.tools>  .+$"),
                f" -- nyrda <nyrda@keymasq.tools>  {_debian_timestamp(release_date)}",
            )
        )
    return rules


def _rewrite_changelog(
    root: Path, version: str, release_date: str, current_version: str, dry_run: bool
) -> bool:
    path = root / "CHANGELOG.md"
    content = path.read_text(encoding="utf-8")
    if current_version == version:
        return False

    release_header = f"## {version}"
    release_pattern = re.compile(
        CHANGELOG_RELEASE_RE_TEMPLATE.format(version=re.escape(version))
    )
    updated, replacements = release_pattern.subn(release_header, content, count=1)
    if replacements == 1:
        if updated == content:
            return False
        if not dry_run:
            path.write_text(updated, encoding="utf-8")
        return True

    first_section = re.search(r"(?m)^## ", content)
    if first_section is not None:
        updated = (
            content[: first_section.start()]
            + f"{release_header}\n\n"
            + content[first_section.start() :]
        )
    else:
        updated = content.rstrip() + f"\n\n{release_header}\n"
    if updated == content:
        return False
    if not dry_run:
        path.write_text(updated, encoding="utf-8")
    return True


def _build_metainfo_release(version: str, release_date: str) -> str:
    return "\n".join(
        [
            f'    <release version="{version}" date="{release_date}">',
            "      <description>",
            f"        <p>{METAINFO_PLACEHOLDER_SUMMARY}</p>",
            "      </description>",
            "    </release>",
        ]
    )


def _rewrite_metainfo(
    root: Path, version: str, release_date: str, current_version: str, dry_run: bool
) -> bool:
    path = root / "assets/tools.keymasq.keymasq.metainfo.xml"
    content = path.read_text(encoding="utf-8")
    if current_version == version:
        return False

    release_line = f'    <release version="{version}" date="{release_date}">'
    release_pattern = re.compile(
        METAINFO_RELEASE_LINE_RE_TEMPLATE.format(version=re.escape(version))
    )
    updated, replacements = release_pattern.subn(release_line, content, count=1)
    if replacements == 1:
        if updated == content:
            return False
        if not dry_run:
            path.write_text(updated, encoding="utf-8")
        return True

    new_release = _build_metainfo_release(version, release_date)
    releases_block_match = re.search(r"(?s)  <releases>\n(?P<body>.*?)\n  </releases>", content)
    if releases_block_match is not None:
        block_body = releases_block_match.group("body").strip("\n")
        replacement = "  <releases>\n"
        replacement += f"{new_release}\n"
        if block_body:
            replacement += f"{block_body}\n"
        replacement += "  </releases>"
        updated = (
            content[: releases_block_match.start()]
            + replacement
            + content[releases_block_match.end() :]
        )
    else:
        insert_at = content.rfind("</component>")
        if insert_at == -1:
            raise RuntimeError(f"missing closing </component> tag in {path}")
        insertion = f"  <releases>\n{new_release}\n  </releases>\n"
        updated = content[:insert_at] + insertion + content[insert_at:]

    if updated == content:
        return False
    if not dry_run:
        path.write_text(updated, encoding="utf-8")
    return True


def _load_pacman_render(root: Path) -> ModuleType:
    render_path = root / "packaging/pacman/render.py"
    spec = importlib.util.spec_from_file_location("_keymasq_pacman_render", render_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load pacman renderer from {render_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render_pacman_outputs(root: Path, version: str, dry_run: bool) -> list[Path]:
    module = _load_pacman_render(root)
    aur_source_url = (
        f"{module.DEFAULT_AUR_SOURCE_BASE_URL}/keymasq-{version}.tar.gz"
    )
    aur_sha256 = "SKIP"
    local_replacements = module.build_replacements(
        version, "local", aur_source_url, aur_sha256
    )
    aur_replacements = module.build_replacements(version, "aur", aur_source_url, aur_sha256)
    outputs = {
        root / "PKGBUILD": module.render_template("PKGBUILD.in", local_replacements),
        root / "keymasq.install": module.render_template(
            "keymasq.install.in", local_replacements
        ),
        root / "packaging/aur/PKGBUILD": module.render_template(
            "PKGBUILD.in", aur_replacements
        ),
        root / "packaging/aur/keymasq.install": module.render_template(
            "keymasq.install.in", aur_replacements
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
            "Update the Keymasq release version across maintained version files "
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
        help="Release date to stamp into Debian changelog and AppStream metainfo (YYYY-MM-DD).",
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

    root = repo_root()
    current_version = _current_version(root)
    changed_paths = apply_rules(
        root,
        _build_rules(root, version, release_date, current_version),
        dry_run=args.dry_run,
    )
    if _rewrite_changelog(root, version, release_date, current_version, dry_run=args.dry_run):
        changed_paths.append(Path("CHANGELOG.md"))
    if _rewrite_metainfo(root, version, release_date, current_version, dry_run=args.dry_run):
        changed_paths.append(Path("assets/tools.keymasq.keymasq.metainfo.xml"))
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
