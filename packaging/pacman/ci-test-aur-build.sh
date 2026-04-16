#!/usr/bin/env bash
set -euo pipefail

workspace_dir="${1:-/workspace}"
dist_dir="${2:-$workspace_dir/dist/arch}"
repo_copy="/tmp/keymasq"
build_dir="/tmp/keymasq-aur-build"

rm -rf "$repo_copy" "$build_dir"
cp -a "$workspace_dir" "$repo_copy"
mkdir -p "$dist_dir"
mkdir -p "$build_dir"

if ! id -u builder >/dev/null 2>&1; then
    useradd -m builder
fi

chown -R builder:builder "$repo_copy" "$build_dir"

runuser -u builder -- bash -lc '
    set -euo pipefail

    cd "'"$repo_copy"'"

    pkgver="${KEYMASQ_PKGVER_OVERRIDE:-$(
        python3 - <<'"'"'PY'"'"'
from pathlib import Path
import tomllib

data = tomllib.loads(Path("pyproject.toml").read_text())
print(data["project"]["version"])
PY
    )}"

    archive_path="'"$repo_copy"'/dist/keymasq-${pkgver}.tar.gz"
    archive_sha256="$(sha256sum "$archive_path" | awk '"'"'{print $1}'"'"')"

    python3 packaging/pacman/render.py \
        --pkgver "$pkgver" \
        --aur-source-url "file://$archive_path" \
        --aur-sha256 "$archive_sha256"

    mkdir -p "'"$build_dir"'"
    cp packaging/aur/PKGBUILD packaging/aur/keymasq.install packaging/aur/.SRCINFO "'"$build_dir"'/"

    cd "'"$build_dir"'"
    makepkg --nodeps --noconfirm --cleanbuild --clean
'

package_path="$(
    find "$build_dir" -maxdepth 1 -type f \
        \( -name '*.pkg.tar.zst' -o -name '*.pkg.tar.xz' -o -name '*.pkg.tar.gz' -o -name '*.pkg.tar.bz2' -o -name '*.pkg.tar.lz4' \) \
        | sort \
        | head -n 1
)"

if [[ -z "$package_path" ]]; then
    echo "failed to locate built pacman package in $build_dir" >&2
    exit 1
fi

cp -f "$package_path" "$dist_dir"/
