#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${SCRIPT_PATH}")/.." && pwd)}"

if [[ ! -f "${REPO_ROOT}/flake.nix" ]]; then
  echo "flake.nix not found under: ${REPO_ROOT}" >&2
  exit 1
fi

if [[ -z "${IN_NIX_SHELL:-}" ]]; then
  exec nix develop "${REPO_ROOT}" -c "${SCRIPT_PATH}" "$@"
fi

export PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

exec python -m keyforge.gui "$@"
