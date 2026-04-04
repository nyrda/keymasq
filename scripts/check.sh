#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

strip_log_noise() {
  sed -E \
    -e $'s/\x1B\\[[0-9;]*[[:alpha:]]//g' \
    -e 's/[[:space:]]+$//'
}

run_compact_check() {
  local label="$1"
  local command="$2"
  local raw_log="$tmp_dir/${label}.raw.log"
  local clean_log="$tmp_dir/${label}.clean.log"

  if bash -lc "$command" >"$raw_log" 2>&1; then
    printf '%s: ok\n' "$label"
    return 0
  fi

  strip_log_noise <"$raw_log" >"$clean_log"
  printf '%s: failed\n' "$label"
  cat "$clean_log"
  return 1
}

run_pytest() {
  local command="cd '$ROOT_DIR' && nix develop -c bash -lc 'pytest tests -v'"

  if command -v xvfb-run >/dev/null 2>&1; then
    command="cd '$ROOT_DIR' && xvfb-run -a nix develop -c bash -lc 'pytest tests -v'"
  fi

  echo "pytest:"
  bash -lc "$command"
}

if ! run_compact_check "ruff" "cd '$ROOT_DIR' && nix develop -c bash -lc 'ruff check keyforge tests'"; then
  exit 1
fi

if ! run_compact_check "basedpyright" "cd '$ROOT_DIR' && nix develop -c bash -lc 'basedpyright'"; then
  exit 1
fi

run_pytest
