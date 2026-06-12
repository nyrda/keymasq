#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

if [[ ! -f "${REPO_ROOT}/flake.nix" ]]; then
  echo "flake.nix not found under: ${REPO_ROOT}" >&2
  exit 1
fi

OUT_PATH="$(nix build "${REPO_ROOT}#default" --no-link --print-out-paths)"
exec "${OUT_PATH}/bin/keymasq" "$@"
