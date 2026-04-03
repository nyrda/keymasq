#!/usr/bin/env bash
set -euo pipefail

workspace_dir="${1:-/workspace}"
repo_copy="/tmp/keyforge"
build_dir="/tmp/keyforge-aur-build"

rm -rf "$repo_copy" "$build_dir"
cp -a "$workspace_dir" "$repo_copy"

if ! id -u builder >/dev/null 2>&1; then
    useradd -m builder
fi

chown -R builder:builder "$repo_copy"

runuser -u builder -- bash -lc '
    set -euo pipefail

    cd "'"$repo_copy"'"

    version="$(python3 - <<'"'"'PY'"'"'
from pathlib import Path
import tomllib

data = tomllib.loads(Path("pyproject.toml").read_text())
print(data["project"]["version"])
PY
    )"

    archive_path="'"$repo_copy"'/dist/keyforge-${version}.tar.gz"
    archive_sha256="$(sha256sum "$archive_path" | awk '"'"'{print $1}'"'"')"

    python3 packaging/pacman/render.py \
        --aur-source-url "file://$archive_path" \
        --aur-sha256 "$archive_sha256"

    mkdir -p "'"$build_dir"'"
    cp packaging/aur/PKGBUILD packaging/aur/keyforge.install packaging/aur/.SRCINFO "'"$build_dir"'/"

    cd "'"$build_dir"'"
    makepkg --nodeps --noconfirm --cleanbuild --clean
'
