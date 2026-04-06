#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/check.sh [--vm] [keyforged|session|gui|full]

Runs ruff, basedpyright, and the selected pytest category.
Defaults to full.
EOF
}

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BACKEND="host"
CATEGORY="full"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vm)
      BACKEND="vm"
      ;;
    keyforged|session|keyforge-session|gui|full)
      CATEGORY="$1"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
  shift
done

case "$CATEGORY" in
  keyforged)
    PYTEST_MARK_EXPR="keyforged"
    ;;
  session|keyforge-session)
    CATEGORY="session"
    PYTEST_MARK_EXPR="session"
    ;;
  gui)
    PYTEST_MARK_EXPR="gui"
    ;;
  full)
    PYTEST_MARK_EXPR=""
    ;;
esac

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

strip_log_noise() {
  sed -E \
    -e $'s/\x1B\\[[0-9;]*[[:alpha:]]//g' \
    -e 's/^vm-test-run-pytest-vm> //' \
    -e 's/^pytest-vm: //' \
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

run_pytest_host() {
  local pytest_args="tests -v"
  local command=""

  if [[ -n "$PYTEST_MARK_EXPR" ]]; then
    pytest_args="$pytest_args -m $PYTEST_MARK_EXPR"
  fi

  command="cd '$ROOT_DIR' && nix develop -c bash -lc 'pytest ${pytest_args}'"

  if command -v xvfb-run >/dev/null 2>&1; then
    command="cd '$ROOT_DIR' && xvfb-run -a nix develop -c bash -lc 'pytest ${pytest_args}'"
  fi

  echo "pytest (${CATEGORY}):"
  bash -lc "$command"
}

run_pytest_vm() {
  local flake_ref="path:$ROOT_DIR"
  local raw_log="$tmp_dir/pytest-vm.raw.log"
  local clean_log="$tmp_dir/pytest-vm.clean.log"
  local report_log="$tmp_dir/pytest-vm.report.log"
  local command="cd '$ROOT_DIR' && "

  if [[ -n "$PYTEST_MARK_EXPR" ]]; then
    command+="KEYFORGE_PYTEST_MARK_EXPR='$PYTEST_MARK_EXPR' "
  fi
  command+="nix run '$flake_ref#checks.x86_64-linux.pytest-vm.driver' -- --keep-machine-state"

  echo "pytest (${CATEGORY}, vm):"
  if bash -lc "$command" >"$raw_log" 2>&1; then
    :
  else
    strip_log_noise <"$raw_log" >"$clean_log"
    if extract_pytest_report <"$clean_log" >"$report_log"; then
      cat "$report_log"
    else
      cat "$clean_log"
    fi
    return 1
  fi

  strip_log_noise <"$raw_log" >"$clean_log"
  if extract_pytest_report <"$clean_log" >"$report_log"; then
    cat "$report_log"
  else
    cat "$clean_log"
  fi
}

if ! run_compact_check "ruff" "cd '$ROOT_DIR' && nix develop -c bash -lc 'ruff check keyforge tests'"; then
  exit 1
fi

if ! run_compact_check "basedpyright" "cd '$ROOT_DIR' && nix develop -c bash -lc 'basedpyright'"; then
  exit 1
fi

if [[ "$BACKEND" == "vm" ]]; then
  run_pytest_vm
else
  run_pytest_host
fi
