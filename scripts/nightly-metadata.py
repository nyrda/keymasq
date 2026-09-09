#!/usr/bin/env python3
"""Select an immutable master snapshot and a version for GitHub nightlies."""

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path


def nightly_metadata(
    sha: str,
    releases: list[dict[str, object]],
    now: datetime,
    *,
    validate_only: bool = False,
) -> dict[str, str]:
    if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ValueError("master must resolve to a full commit SHA")
    published = [release for release in releases if not release.get("draft")]
    nightlies = [
        release
        for release in published
        if release.get("prerelease") and str(release["tag_name"]).startswith("nightly-")
    ]
    latest = max(nightlies, key=lambda r: str(r["published_at"]), default=None)
    if not validate_only and latest and latest["target_commitish"] == sha:
        return {"build": "false", "sha": sha}

    stable_versions = []
    for release in published:
        if release.get("prerelease"):
            continue
        match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", str(release["tag_name"]))
        if match:
            stable_versions.append(tuple(int(part) for part in match.groups()))
    if not stable_versions:
        raise ValueError("a published stable vMAJOR.MINOR.PATCH release is required")
    major, minor, patch = max(stable_versions)
    stamp = now.astimezone(UTC).strftime("%Y%m%d%H%M%S")
    return {
        "build": "true",
        "sha": sha,
        "version": f"{major}.{minor}.{patch + 1}.dev{stamp}",
        "tag": f"nightly-{stamp}",
    }


def main() -> None:
    sha, releases_path = sys.argv[1:]
    pages = json.loads(Path(releases_path).read_text(encoding="utf-8"))
    metadata = nightly_metadata(
        sha,
        [release for page in pages for release in page],
        datetime.now(UTC),
        validate_only=os.environ.get("VALIDATE_ONLY") == "true",
    )
    for key, value in metadata.items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
