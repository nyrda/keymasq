import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts/release-version.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_version_test", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    scripts_dir = str(SCRIPT_PATH.parent)
    sys.path.insert(0, scripts_dir)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
        sys.path.remove(scripts_dir)
    return module


def _write_release_fixture(root: Path) -> None:
    (root / "keymasq").mkdir()
    (root / "packaging/rpm").mkdir(parents=True)
    (root / "debian").mkdir()
    (root / "assets").mkdir()

    (root / "pyproject.toml").write_text(
        '[project]\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    (root / "keymasq/_version.py").write_text(
        '__version__ = "1.2.3"\n',
        encoding="utf-8",
    )
    (root / "packaging/rpm/metadata.env").write_text(
        'VERSION="1.2.3"\n',
        encoding="utf-8",
    )
    (root / "flake.nix").write_text(
        '{\n          version = "1.2.3";\n}\n',
        encoding="utf-8",
    )
    (root / "debian/changelog").write_text(
        "\n".join(
            [
                "keymasq (1.2.3-1) unstable; urgency=medium",
                "",
                "  * Existing release.",
                "",
                " -- nyrda <nyrda@keymasq.tools>  Mon, 01 Jan 2024 00:00:00 +0000",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.2.3\n\n- Existing release.\n",
        encoding="utf-8",
    )
    (root / "assets/tools.keymasq.keymasq.metainfo.xml").write_text(
        "\n".join(
            [
                '<component type="desktop-application">',
                "  <id>tools.keymasq.keymasq</id>",
                "  <releases>",
                '    <release version="1.2.3" date="2024-01-01">',
                "      <description>",
                "        <p>Existing release.</p>",
                "      </description>",
                "    </release>",
                "  </releases>",
                "</component>",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_release_version_refreshes_same_version_dated_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = _load_script()
    _write_release_fixture(tmp_path)
    monkeypatch.setattr(script, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(script, "_render_pacman_outputs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        sys,
        "argv",
        [SCRIPT_PATH.name, "1.2.3", "--release-date", "2026-05-31"],
    )

    assert script.main() == 0

    assert (
        " -- nyrda <nyrda@keymasq.tools>  Sun, 31 May 2026 00:00:00 +0000"
        in (tmp_path / "debian/changelog").read_text(encoding="utf-8")
    )
    assert (
        '    <release version="1.2.3" date="2026-05-31">'
        in (tmp_path / "assets/tools.keymasq.keymasq.metainfo.xml").read_text(
            encoding="utf-8"
        )
    )
    output = capsys.readouterr().out
    assert "Updated: debian/changelog" in output
    assert "Updated: assets/tools.keymasq.keymasq.metainfo.xml" in output


def test_rewrite_changelog_refreshes_dated_release_header(tmp_path: Path) -> None:
    script = _load_script()
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.2.3 - 2026-05-01\n\n- Existing release.\n",
        encoding="utf-8",
    )

    changed = script._rewrite_changelog(
        tmp_path, "1.2.3", "2026-06-01", "1.2.2", dry_run=False
    )

    assert changed is True
    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == (
        "# Changelog\n\n## 1.2.3 - 2026-06-01\n\n- Existing release.\n"
    )


def test_rewrite_changelog_stamps_undated_release_header(tmp_path: Path) -> None:
    script = _load_script()
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.2.3\n\n- Existing release.\n",
        encoding="utf-8",
    )

    changed = script._rewrite_changelog(
        tmp_path, "1.2.3", "2026-06-01", "1.2.2", dry_run=False
    )

    assert changed is True
    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == (
        "# Changelog\n\n## 1.2.3 - 2026-06-01\n\n- Existing release.\n"
    )


def test_release_version_apply_rules_limits_duplicate_matches(tmp_path: Path) -> None:
    script = _load_script()
    target = tmp_path / "metadata.env"
    target.write_text('VERSION="1.0.0"\nVERSION="1.0.0"\n', encoding="utf-8")
    rule = script.RewriteRule(
        target,
        re.compile(r'(?m)^VERSION=".*"$'),
        'VERSION="2.0.0"',
    )

    changed_paths = script.apply_rules(tmp_path, [rule], dry_run=False)

    assert changed_paths == [Path("metadata.env")]
    assert target.read_text(encoding="utf-8") == (
        'VERSION="2.0.0"\nVERSION="1.0.0"\n'
    )


def test_release_version_rejects_non_canonical_release_date(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = _load_script()
    monkeypatch.setattr(
        sys,
        "argv",
        [SCRIPT_PATH.name, "1.2.3", "--release-date", "2026-5-1"],
    )

    with pytest.raises(SystemExit) as exc_info:
        script.main()

    assert exc_info.value.code == 2
    assert "release date must use the form YYYY-MM-DD" in capsys.readouterr().err
