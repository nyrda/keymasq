#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/check.sh [--vm] [auto|keymasqd|session|gui|docshots|full]

Runs ruff, basedpyright, stylelint for GUI CSS, and the selected pytest category.
Defaults to auto, which selects the category from pending and untracked changes
under keymasq/, tests/, and nix/docshots/.
EOF
}

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BACKEND="host"
CATEGORY="auto"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vm)
      BACKEND="vm"
      ;;
  auto|keymasqd|session|keymasq-session|gui|docshots|full)
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

resolve_auto_category() {
  local path
  local selected=""
  local -a changed_paths=()
  local -a untracked_paths=()

  mapfile -t changed_paths < <(git diff --name-only HEAD -- keymasq tests nix/docshots)
  mapfile -t untracked_paths < <(git ls-files --others --exclude-standard -- keymasq tests nix/docshots)

  for path in "${changed_paths[@]}" "${untracked_paths[@]}"; do
    [[ -n "$path" ]] || continue

    case "$path" in
      keymasq/keymasqd/*|tests/keymasqd/*)
        if [[ -z "$selected" ]]; then
          selected="keymasqd"
        elif [[ "$selected" != "keymasqd" ]]; then
          selected="full"
        fi
        ;;
      keymasq/session/*|tests/session/*)
        if [[ -z "$selected" ]]; then
          selected="session"
        elif [[ "$selected" != "session" ]]; then
          selected="full"
        fi
        ;;
      keymasq/gui/*|tests/gui/*)
        if [[ -z "$selected" ]]; then
          selected="gui"
        elif [[ "$selected" != "gui" ]]; then
          selected="full"
        fi
        ;;
      nix/docshots/*)
        if [[ -z "$selected" ]]; then
          selected="docshots"
        elif [[ "$selected" != "docshots" ]]; then
          selected="full"
        fi
        ;;
      keymasq/*|tests/*)
        selected="full"
        ;;
    esac

    if [[ "$selected" == "full" ]]; then
      break
    fi
  done

  if [[ -z "$selected" ]]; then
    return 1
  fi

  printf '%s\n' "$selected"
}

if [[ "$CATEGORY" == "auto" ]]; then
  if CATEGORY="$(resolve_auto_category)"; then
    echo "auto: selected ${CATEGORY}"
  else
    echo "auto: no pending or untracked changes under keymasq/ or tests/; nothing to run"
    exit 0
  fi
fi

case "$CATEGORY" in
  keymasqd)
    PYTEST_MARK_EXPR="keymasqd"
    ;;
  session|keymasq-session)
    CATEGORY="session"
    PYTEST_MARK_EXPR="session"
    ;;
  gui)
    PYTEST_MARK_EXPR="gui"
    ;;
  full)
    PYTEST_MARK_EXPR=""
    ;;
  docshots)
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

run_in_repo() {
  (
    cd "$ROOT_DIR"
    "$@"
  )
}

run_default_nix() {
  run_in_repo nix develop -c "$@"
}

run_ci_nix() {
  run_in_repo nix develop ".#ci" -c "$@"
}

run_ci_gui_nix() {
  run_in_repo nix develop ".#ci-gui" -c "$@"
}

run_compact_check() {
  local label="$1"
  local raw_log="$tmp_dir/${label}.raw.log"
  local clean_log="$tmp_dir/${label}.clean.log"
  shift

  if "$@" >"$raw_log" 2>&1; then
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

extract_pytest_final_summary() {
  awk '
    NF {
      last = $0
    }
    END {
      if (last != "") {
        print last
      } else {
        exit 1
      }
    }
  '
}

run_pytest_gui_host() {
  run_ci_gui_nix bash -s -- "$@" <<'EOF'
set -euo pipefail
export GDK_BACKEND=x11

xvfb_pid=""
for display_num in $(seq 90 150); do
  if [[ ! -e "/tmp/.X11-unix/X${display_num}" ]]; then
    export DISPLAY=":${display_num}"
    break
  fi
done

if [[ -z "${DISPLAY:-}" ]]; then
  echo "failed to find an unused X display" >&2
  exit 1
fi

Xvfb "$DISPLAY" -screen 0 1280x1024x24 >/tmp/keymasq-xvfb.log 2>&1 &
xvfb_pid=$!
trap 'kill "$xvfb_pid" 2>/dev/null || true' EXIT

for _ in $(seq 1 50); do
  if [[ -S "/tmp/.X11-unix/X${DISPLAY#:}" ]]; then
    break
  fi
  if ! kill -0 "$xvfb_pid" 2>/dev/null; then
    echo "Xvfb exited before accepting connections" >&2
    exit 1
  fi
  sleep 0.1
done

if [[ ! -S "/tmp/.X11-unix/X${DISPLAY#:}" ]]; then
  echo "timed out waiting for Xvfb on $DISPLAY" >&2
  exit 1
fi

pytest "$@"
EOF
}

run_pytest_host_command() {
  if [[ "$CATEGORY" == "gui" || "$CATEGORY" == "full" ]]; then
    run_pytest_gui_host "$@"
  else
    run_ci_nix pytest "$@"
  fi
}

run_pytest_host() {
  local pytest_workers="${KEYMASQ_PYTEST_WORKERS:-}"
  local raw_log="$tmp_dir/pytest-host.raw.log"
  local clean_log="$tmp_dir/pytest-host.clean.log"
  local summary_log="$tmp_dir/pytest-host.summary.log"
  local -a pytest_args=(tests -q -ra --tb=short)

  if [[ -z "$pytest_workers" ]]; then
    pytest_workers="$(nproc 2>/dev/null || echo 1)"
    if (( pytest_workers > 7 )); then
      pytest_workers=7
    fi
  fi

  if [[ -n "$PYTEST_MARK_EXPR" ]]; then
    pytest_args+=(-m "$PYTEST_MARK_EXPR")
  fi

  if [[ "$CATEGORY" == "keymasqd" || "$CATEGORY" == "session" ]]; then
    pytest_args+=(--ignore=tests/gui)
  fi

  if [[ "$pytest_workers" != "0" && "$pytest_workers" != "1" ]]; then
    pytest_args+=(-n "$pytest_workers")
  fi

  echo "pytest (${CATEGORY}):"
  if run_pytest_host_command "${pytest_args[@]}" >"$raw_log" 2>&1; then
    strip_log_noise <"$raw_log" >"$clean_log"
    if extract_pytest_final_summary <"$clean_log" >"$summary_log"; then
      printf 'pytest: ok - '
      cat "$summary_log"
    else
      printf 'pytest: ok\n'
    fi
    return 0
  fi

  strip_log_noise <"$raw_log" >"$clean_log"
  printf 'pytest: failed\n'
  cat "$clean_log"
  return 1
}

run_pytest_vm() {
  local flake_ref="path:$ROOT_DIR"
  local raw_log="$tmp_dir/pytest-vm.raw.log"
  local clean_log="$tmp_dir/pytest-vm.clean.log"
  local report_log="$tmp_dir/pytest-vm.report.log"
  local -a command=(nix run "${flake_ref}#checks.x86_64-linux.pytest-vm.driver" -- --keep-machine-state)

  if [[ -n "$PYTEST_MARK_EXPR" ]]; then
    command=(env "KEYMASQ_PYTEST_MARK_EXPR=$PYTEST_MARK_EXPR" "${command[@]}")
  fi

  echo "pytest (${CATEGORY}, vm):"
  if run_in_repo "${command[@]}" >"$raw_log" 2>&1; then
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

if ! run_compact_check "ruff" run_default_nix ruff check keymasq tests nix/docshots; then
  exit 1
fi

if ! run_compact_check "basedpyright" run_default_nix basedpyright; then
  exit 1
fi

if [[ "$CATEGORY" == "gui" || "$CATEGORY" == "full" ]]; then
  if ! run_compact_check "stylelint" run_ci_gui_nix stylelint "keymasq/gui/**/*.css"; then
    exit 1
  fi
fi

if [[ "$CATEGORY" == "docshots" ]]; then
  exit 0
fi

if [[ "$BACKEND" == "vm" ]]; then
  run_pytest_vm
else
  run_pytest_host
fi
