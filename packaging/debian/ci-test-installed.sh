#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: ci-test-installed.sh <path-to-deb>" >&2
    exit 2
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PACKAGE_PATH="$1"

if [[ ! -f "$PACKAGE_PATH" ]]; then
    echo "package not found: $PACKAGE_PATH" >&2
    exit 1
fi

PACKAGE_PATH="$(realpath "$PACKAGE_PATH")"

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y python3-pytest python3-pytest-asyncio "$PACKAGE_PATH"

dpkg-deb -I "$PACKAGE_PATH"
dpkg-deb -c "$PACKAGE_PATH"

cd "$REPO_DIR"
sh debian/tests/pkg-smoke
sh debian/tests/installed-cli
