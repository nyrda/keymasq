#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

strip_log_noise() {
  sed -E \
    -e $'s/\x1B\\[[0-9;]*[[:alpha:]]//g' \
    -e 's/^vm-test-run-pytest-vm> //' \
    -e 's/^pytest-vm: //' \
    -e 's/[[:space:]]+$//'
}

extract_pytest_report() {
  awk '
    BEGIN {
      capture = 0
      printed = 0
    }
    $0 == "pytest results:" {
      capture = 1
      next
    }
    capture {
      if ($0 ~ /^\(finished: run the VM test script,/ || $0 ~ /^test script finished/ || $0 ~ /^cleanup$/ || $0 ~ /^kill / || $0 ~ /^vde_switch:/ || $0 ~ /^additionally exposed symbols:/ || $0 ~ /^    / || $0 ~ /^pytest-vm,$/ || $0 ~ /^vlan1,$/ || $0 ~ /^start_all,/) {
        exit
      }
      if ($0 != "") {
        print
        printed = 1
      }
    }
    END {
      if (!printed) {
        exit 1
      }
    }
  '
}

LAST_BG_PID=""

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

start_compact_check() {
  local label="$1"
  local command="$2"

  (
    local rc=0
    if bash -lc "$command" >"$tmp_dir/${label}.raw.log" 2>&1; then
      rc=0
    else
      rc=$?
    fi
    printf '%s\n' "$rc" >"$tmp_dir/${label}.rc"
  ) &
  LAST_BG_PID="$!"
}

start_background_command() {
  local raw_log="$1"
  local command="$2"

  setsid bash -lc "$command" >"$raw_log" 2>&1 &
  LAST_BG_PID="$!"
}

finish_compact_check() {
  local label="$1"
  local pid="$2"
  local raw_log="$tmp_dir/${label}.raw.log"
  local clean_log="$tmp_dir/${label}.clean.log"
  local rc=0

  wait "$pid" || true
  if [[ -f "$tmp_dir/${label}.rc" ]]; then
    rc="$(cat "$tmp_dir/${label}.rc")"
  fi

  if [[ "$rc" == "0" ]]; then
    printf '%s: ok\n' "$label"
    return 0
  fi

  strip_log_noise <"$raw_log" >"$clean_log"
  printf '%s: failed\n' "$label"
  cat "$clean_log"
  return 1
}

pytest_raw_log="$tmp_dir/pytest-vm.raw.log"
pytest_clean_log="$tmp_dir/pytest-vm.clean.log"
pytest_report="$tmp_dir/pytest-vm.report.log"

if ! run_compact_check "ruff" "cd '$ROOT_DIR' && nix develop -c bash -lc 'ruff check keyforge tests'"; then
  exit 1
fi

start_compact_check "basedpyright" "cd '$ROOT_DIR' && nix develop -c bash -lc 'basedpyright'"
basedpyright_pid="$LAST_BG_PID"
start_background_command "$pytest_raw_log" "cd '$ROOT_DIR' && nix run .#checks.x86_64-linux.pytest-vm.driver -- --keep-machine-state"
pytest_pid="$LAST_BG_PID"

if finish_compact_check "basedpyright" "$basedpyright_pid"; then
  :
else
  kill -- -"$pytest_pid" 2>/dev/null || kill "$pytest_pid" 2>/dev/null || true
  wait "$pytest_pid" || true
  exit 1
fi

pytest_rc=0
if wait "$pytest_pid"; then
  pytest_rc=0
else
  pytest_rc=$?
fi

strip_log_noise <"$pytest_raw_log" >"$pytest_clean_log"
if ! extract_pytest_report <"$pytest_clean_log" >"$pytest_report"; then
  echo "pytest-vm: failed to extract concise report"
  tail -n 40 "$pytest_clean_log"
  exit 1
fi

echo "pytest-vm:"
cat "$pytest_report"

if (( pytest_rc != 0 )); then
  exit "$pytest_rc"
fi
