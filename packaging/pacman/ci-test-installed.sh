#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: ci-test-installed.sh <path-to-pkg.tar>" >&2
    exit 2
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PACKAGE_PATH="$1"

if [[ ! -f "$PACKAGE_PATH" ]]; then
    echo "package not found: $PACKAGE_PATH" >&2
    exit 1
fi

PACKAGE_PATH="$(realpath "$PACKAGE_PATH")"

mapfile -t package_deps < <(
    bsdtar -xOf "$PACKAGE_PATH" .PKGINFO | awk -F' = ' '
        $1 == "depend" {
            dep = $2
            sub(/[<>=].*$/, "", dep)
            if (!(dep in seen)) {
                seen[dep] = 1
                print dep
            }
        }
    '
)

if ((${#package_deps[@]} > 0)); then
    pacman -Syu --noconfirm --needed "${package_deps[@]}"
else
    pacman -Syu --noconfirm
fi

pacman -Qip "$PACKAGE_PATH"
pacman -Qlp "$PACKAGE_PATH"
pacman -U --noconfirm "$PACKAGE_PATH"

cd "$REPO_DIR"
sh debian/tests/pkg-smoke
