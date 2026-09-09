from datetime import UTC, datetime
from pathlib import Path

import pytest
from packaging.version import Version

from tests.script_loader import load_script

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/nightly-metadata.py"
SHA = "a" * 40
NOW = datetime(2026, 9, 9, 10, tzinfo=UTC)


def release(tag: str, **overrides: object) -> dict[str, object]:
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": tag.startswith("nightly-"),
        "published_at": "2026-09-08T10:00:00Z",
        "target_commitish": "b" * 40,
        **overrides,
    }


def metadata(releases: list[dict[str, object]], **kwargs: object):
    script = load_script(SCRIPT, "nightly_metadata_test")
    return script.nightly_metadata(SHA, releases, NOW, **kwargs)


def test_first_nightly_sorts_between_stable_releases() -> None:
    result = metadata([release("v0.19.0")])
    assert result == {
        "build": "true",
        "sha": SHA,
        "version": "0.19.1.dev20260909100000",
        "tag": "nightly-20260909100000",
    }
    assert Version("0.19.0") < Version(result["version"]) < Version("0.19.1")


def test_unchanged_master_skips_only_after_successful_publication() -> None:
    releases = [release("v0.19.0"), release("nightly-old", target_commitish=SHA)]
    assert metadata(releases) == {"build": "false", "sha": SHA}
    assert metadata(releases, validate_only=True)["build"] == "true"
    releases[-1]["draft"] = True
    assert metadata(releases)["build"] == "true"


def test_changed_master_builds_after_missed_days_and_ignores_drafts() -> None:
    releases = [
        release("v0.19.0"),
        release("nightly-old"),
        release("nightly-draft", target_commitish=SHA, draft=True),
        release("v99.0.0", draft=True),
        release("v50.0.0", prerelease=True),
    ]
    assert metadata(releases)["version"] == "0.19.1.dev20260909100000"


def test_latest_publication_controls_comparison_not_api_order() -> None:
    releases = [
        release("v0.19.0"),
        release("nightly-old", target_commitish=SHA),
        release("nightly-new", published_at="2026-09-09T09:00:00Z"),
    ]
    assert metadata(releases)["build"] == "true"


def test_stable_version_selection_is_numeric() -> None:
    releases = [release("v0.9.9"), release("v0.19.1"), release("v0.19.1rc1")]
    assert metadata(releases)["version"].startswith("0.19.2.dev")


def test_requires_a_published_stable_version() -> None:
    with pytest.raises(ValueError, match="published stable"):
        metadata([])


def test_rejects_mutable_source_ref() -> None:
    script = load_script(SCRIPT, "nightly_metadata_test")
    with pytest.raises(ValueError, match="full commit SHA"):
        script.nightly_metadata("master", [release("v0.19.0")], NOW)
