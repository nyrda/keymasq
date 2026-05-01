#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"

if [[ ! -d "$REPO_DIR" ]]; then
  echo "Repo directory not found: $REPO_DIR" >&2
  exit 1
fi

if [[ ! -f "$REPO_DIR/PKGBUILD" ]]; then
  echo "Missing PKGBUILD in: $REPO_DIR" >&2
  exit 1
fi

echo "Installing locally from: $REPO_DIR"
(
  cd "$REPO_DIR"
  makepkg -si --noconfirm --force
)
