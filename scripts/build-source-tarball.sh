#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DIST_DIR="$REPO_DIR/dist"

version="${1:-}"

if [[ -z "$version" ]]; then
    version="$(REPO_DIR="$REPO_DIR" python3 - <<'PY'
from os import environ
from pathlib import Path
import tomllib

data = tomllib.loads(Path(environ["REPO_DIR"], "pyproject.toml").read_text(encoding="utf-8"))
print(data["project"]["version"])
PY
    )"
fi

readonly archive_name="keyforge-${version}.tar.gz"
readonly archive_path="$DIST_DIR/$archive_name"
readonly archive_prefix="keyforge-${version}/"

declare -a source_paths=(
    CHANGELOG.md
    LICENSE
    README.md
    pyproject.toml
    assets/keyforge.desktop
    assets/keyforge.metainfo.xml
    assets/keyforge.svg
    assets/icons
    examples
    gnome-extension
    keyforge
    polkit
    systemd
    sysusers.d
    tmpfiles.d
    udev
)

for path in "${source_paths[@]}"; do
    if [[ ! -e "$REPO_DIR/$path" ]]; then
        printf 'Missing required source path: %s\n' "$path" >&2
        exit 1
    fi
done

declare -a tracked_source_paths=()
for path in "${source_paths[@]}"; do
    mapfile -d '' -t tracked_matches < <(git -C "$REPO_DIR" ls-files -z -- "$path")
    if [[ "${#tracked_matches[@]}" -eq 0 ]]; then
        printf 'No tracked files found for source path: %s\n' "$path" >&2
        exit 1
    fi
    tracked_source_paths+=("${tracked_matches[@]}")
done

mkdir -p "$DIST_DIR"
rm -f "$archive_path"

printf '%s\0' "${tracked_source_paths[@]}" |
    tar -C "$REPO_DIR" \
        --null \
        --files-from=- \
        --create \
        --gzip \
        --file "$archive_path" \
        --sort=name \
        --mtime='UTC 1970-01-01' \
        --owner=0 \
        --group=0 \
        --numeric-owner \
        --transform "s,^,${archive_prefix},"

printf '%s\n' "$archive_path"
