#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST_DIR="${REPO_DIR}/dist/arch"

cd "$REPO_DIR"
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR"

bash packaging/pacman/ci-test-aur-build.sh "$REPO_DIR" "$DIST_DIR"
