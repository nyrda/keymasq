"""Consistency checks between dependency documentation and package manifests.

These tests pin the facts that docs/DEPENDENCIES.md states about
pyproject.toml and the maintained package families, so dependency drift
between the manifests and the documentation fails mechanically instead of
rotting silently.
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


def _pyproject() -> dict:
    return tomllib.loads(_read("pyproject.toml"))


def _doc() -> str:
    return _read("docs/DEPENDENCIES.md")


def _doc_section(title: str) -> str:
    match = re.search(
        rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)",
        _doc(),
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"missing section '## {title}' in docs/DEPENDENCIES.md"
    return match.group(1)


def _plain_bullets(text: str) -> set[str]:
    return set(re.findall(r"^- `([^`]+)`$", text, flags=re.MULTILINE))


def test_documented_base_dependencies_match_pyproject() -> None:
    declared = set(_pyproject()["project"]["dependencies"])
    section = _doc_section("Base Python Runtime Dependencies")
    listing = section.split("What they are used for:")[0]

    assert _plain_bullets(listing) == declared


def test_uvloop_is_speedups_extra_not_base_dependency() -> None:
    project = _pyproject()["project"]

    assert not any("uvloop" in dep for dep in project["dependencies"])
    assert project["optional-dependencies"]["speedups"] == ["uvloop"]
    assert "not a required base dependency" in _doc_section(
        "Base Python Runtime Dependencies"
    )


def test_documented_extras_match_pyproject() -> None:
    declared = _pyproject()["project"]["optional-dependencies"]
    section = _doc_section("Development and Test Dependencies")

    documented: dict[str, set[str]] = {}
    current: str | None = None
    for outer, inner in re.findall(
        r"^- `([^`]+)`$|^  - `([^`]+)`$",
        section,
        flags=re.MULTILINE,
    ):
        if outer:
            current = outer
            documented[current] = set()
        elif current is not None:
            documented[current].add(inner)

    assert documented == {name: set(entries) for name, entries in declared.items()}


def test_undeclared_tools_are_not_documented() -> None:
    declared = _pyproject()["project"]["optional-dependencies"]
    all_entries = " ".join(entry for entries in declared.values() for entry in entries)

    if "mypy" not in all_entries:
        assert "mypy" not in _doc()


def _pkgbuild_depends(rel_path: str) -> str:
    match = re.search(r"^depends=\((.*?)\)", _read(rel_path), flags=re.MULTILINE | re.DOTALL)
    assert match is not None, f"missing depends=() in {rel_path}"
    return match.group(1)


def _debian_field(field: str) -> str:
    match = re.search(
        rf"^{field}:\n((?: [^\n]+\n)+)",
        _read("debian/control"),
        flags=re.MULTILINE,
    )
    assert match is not None, f"missing {field} block in debian/control"
    return match.group(1)


def test_uvloop_packaging_classification_matches_docs() -> None:
    # Arch and AUR: hard dependency.
    assert "'python-uvloop'" in _pkgbuild_depends("PKGBUILD")
    assert "'python-uvloop'" in _pkgbuild_depends("packaging/aur/PKGBUILD")

    # Debian: hard dependency, not merely recommended.
    assert "python3-uvloop" in _debian_field("Depends")
    assert "python3-uvloop" not in _debian_field("Recommends")

    # Fedora and openSUSE: weak dependency only.
    fedora = _read("packaging/rpm/build-fedora-rpm.sh")
    assert re.search(r"^Recommends:\s+python3dist\(uvloop\)", fedora, flags=re.MULTILINE)
    assert not re.search(r"^Requires:.*uvloop", fedora, flags=re.MULTILINE)
    opensuse = _read("packaging/rpm/build-opensuse-rpm.sh")
    assert re.search(r"^Recommends: \$\{RPM_UVLOOP_DEP\}", opensuse, flags=re.MULTILINE)
    assert not re.search(r"^Requires:.*UVLOOP", opensuse, flags=re.MULTILINE)

    # AppImage: bundled. Nix: part of the wrapped Python environment.
    assert "python-uvloop" in _read("packaging/appimage/get-dependencies.sh")
    assert re.search(r"^\s+uvloop$", _read("flake.nix"), flags=re.MULTILINE)


def test_slurp_packaging_classification_matches_docs() -> None:
    assert "'slurp'" in _pkgbuild_depends("PKGBUILD")
    assert "'slurp'" in _pkgbuild_depends("packaging/aur/PKGBUILD")

    assert "slurp" in _debian_field("Recommends")
    assert "slurp" not in _debian_field("Depends")

    for rel_path in (
        "packaging/rpm/build-fedora-rpm.sh",
        "packaging/rpm/build-opensuse-rpm.sh",
    ):
        content = _read(rel_path)
        assert re.search(r"^Recommends:\s+slurp", content, flags=re.MULTILINE)
        assert not re.search(r"^Requires:\s+slurp", content, flags=re.MULTILINE)
