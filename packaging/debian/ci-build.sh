#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST_DIR="${REPO_DIR}/dist/debian"

cd "$REPO_DIR"
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR"

DEB_BUILD_OPTIONS=nocheck dpkg-buildpackage -us -uc -b
lintian ../keyforge_*.deb

cp -f ../keyforge_*.deb "$DIST_DIR"/
cp -f ../keyforge_*.changes "$DIST_DIR"/
cp -f ../keyforge_*.buildinfo "$DIST_DIR"/
