#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

SYSTEM="x86_64-linux"

usage() {
  cat <<'EOF'
Usage: ./scripts/integration.sh [test ...]

Runs Keymasq NixOS VM integration checks through nix build.

Tests:
  daemon-session  daemon/session runtime integration suite
  gnome-bridge    GNOME Shell bridge listener preflight
  gnome           GNOME listener
  kde             KDE Plasma listener
  hyprland        Hyprland listener
  niri            Niri listener
  xfce            X11/XFCE listener
  cosmic          COSMIC listener
  sway            wlroots/Sway listener
  listeners       all listener VM tests
  all             daemon-session plus all listener VM tests

Options:
  -s, --scenario NAME[,NAME]  Run selected daemon-session scenario keys
  -r, --repeat N              Repeat selected integration checks N times

Examples:
  ./scripts/integration.sh cosmic
  ./scripts/integration.sh daemon-session
  ./scripts/integration.sh daemon-session --scenario hotplug-replug
  ./scripts/integration.sh --scenario profile-lifetime-direct-actions --repeat 10
  ./scripts/integration.sh cosmic --repeat 3
  ./scripts/integration.sh listeners

Use path: flake references so newly added or uncommitted VM files are included.
EOF
}

check_name_for_test() {
  case "$1" in
    daemon-session|daemon-session-integration|daemon-session-integration-test)
      printf '%s\n' "daemon-session-integration-test"
      ;;
    gnome-bridge|listener-vm-gnome-bridge)
      printf '%s\n' "listener-vm-gnome-bridge"
      ;;
    gnome|listener-vm-gnome)
      printf '%s\n' "listener-vm-gnome"
      ;;
    kde|plasma|listener-vm-kde)
      printf '%s\n' "listener-vm-kde"
      ;;
    hyprland|listener-vm-hyprland)
      printf '%s\n' "listener-vm-hyprland"
      ;;
    niri|listener-vm-niri)
      printf '%s\n' "listener-vm-niri"
      ;;
    xfce|x11|listener-vm-xfce)
      printf '%s\n' "listener-vm-xfce"
      ;;
    cosmic|listener-vm-cosmic)
      printf '%s\n' "listener-vm-cosmic"
      ;;
    sway|wayland|wlroots|listener-vm-sway)
      printf '%s\n' "listener-vm-sway"
      ;;
    *)
      return 1
      ;;
  esac
}

expand_group() {
  case "$1" in
    listeners)
      printf '%s\n' \
        gnome-bridge \
        gnome \
        kde \
        hyprland \
        niri \
        xfce \
        cosmic \
        sway
      ;;
    all)
      printf '%s\n' \
        daemon-session \
        gnome-bridge \
        gnome \
        kde \
        hyprland \
        niri \
        xfce \
        cosmic \
        sway
      ;;
    *)
      printf '%s\n' "$1"
      ;;
  esac
}

append_scenario_filter() {
  local value="$1"
  local normalized
  normalized="${value//_/-}"
  normalized="${normalized,,}"
  if [[ ! "$normalized" =~ ^[a-z0-9_.-]+(,[a-z0-9_.-]+)*$ ]]; then
    echo "Invalid scenario filter: $value" >&2
    echo "Use scenario keys such as: hotplug-replug,profile-lifetime-direct-actions" >&2
    exit 1
  fi
  if [[ -n "$scenario_filter" ]]; then
    scenario_filter="${scenario_filter},${normalized}"
  else
    scenario_filter="$normalized"
  fi
}

set_repeat_count() {
  local value="$1"
  if [[ ! "$value" =~ ^[1-9][0-9]*$ ]]; then
    echo "Invalid repeat count: $value" >&2
    exit 1
  fi
  repeat_count="$value"
  repeat_requested=1
}

build_or_rebuild() {
  local target="$1"
  local force_rebuild="$2"
  shift 2
  local nix_args=("$@")
  local log_file
  local status

  if [[ "$force_rebuild" != "1" ]]; then
    nix build --no-link "${nix_args[@]}" "$target"
    return
  fi

  log_file="$(mktemp)"
  set +e
  nix build --no-link --rebuild "${nix_args[@]}" "$target" 2>&1 | tee "$log_file"
  status=${PIPESTATUS[0]}
  set -e

  if [[ "$status" -eq 0 ]]; then
    rm -f "$log_file"
    return
  fi

  if grep -q "not valid, so checking is not possible" "$log_file"; then
    rm -f "$log_file"
    nix build --no-link "${nix_args[@]}" "$target"
    return
  fi

  rm -f "$log_file"
  return "$status"
}

if [[ $# -eq 0 ]]; then
  usage
  exit 0
fi

scenario_filter=""
repeat_count=""
repeat_requested=0
raw_tests=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help|help|list|--list)
      usage
      exit 0
      ;;
    -s|--scenario)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for $1" >&2
        exit 1
      fi
      shift
      append_scenario_filter "$1"
      ;;
    --scenario=*)
      append_scenario_filter "${1#*=}"
      ;;
    -r|--repeat)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for $1" >&2
        exit 1
      fi
      shift
      set_repeat_count "$1"
      ;;
    --repeat=*)
      set_repeat_count "${1#*=}"
      ;;
    *)
      raw_tests+=("$1")
      ;;
  esac
  shift
done

tests=()
if [[ ${#raw_tests[@]} -eq 0 && (-n "$scenario_filter" || -n "$repeat_count") ]]; then
  raw_tests=(daemon-session)
fi

for arg in "${raw_tests[@]}"; do
  while IFS= read -r expanded; do
    [[ -n "$expanded" ]] || continue
    tests+=("$expanded")
  done < <(expand_group "$arg")
done

resolved_tests=()
resolved_checks=()
for test in "${tests[@]}"; do
  if ! check_name="$(check_name_for_test "$test")"; then
    echo "Unknown integration test: $test" >&2
    echo >&2
    usage >&2
    exit 1
  fi

  if [[ -n "$scenario_filter" && "$check_name" != "daemon-session-integration-test" ]]; then
    echo "Scenario selection is only supported for daemon-session tests" >&2
    exit 1
  fi

  resolved_tests+=("$test")
  resolved_checks+=("$check_name")
done

for index in "${!resolved_tests[@]}"; do
  test="${resolved_tests[$index]}"
  check_name="${resolved_checks[$index]}"

  nix_args=()
  run_count="${repeat_count:-1}"
  if [[ "$check_name" == "daemon-session-integration-test" && (-n "$scenario_filter" || -n "$repeat_count") ]]; then
    check_name="daemon-session-selected-integration-test"
    nix_args+=(--impure)
  fi

  target="path:.#checks.${SYSTEM}.${check_name}"
  if [[ "$check_name" == "daemon-session-selected-integration-test" ]]; then
    echo "integration: ${test} scenarios=${scenario_filter:-all} repeat=${run_count} -> ${target}"
    (
      export KEYMASQ_INTEGRATION_SCENARIOS="$scenario_filter"
      export KEYMASQ_INTEGRATION_REPEAT="$repeat_count"
      build_or_rebuild "$target" "$repeat_requested" "${nix_args[@]}"
    )
  else
    for iteration in $(seq 1 "$run_count"); do
      if [[ "$run_count" -gt 1 ]]; then
        echo "integration: ${test} repeat ${iteration}/${run_count} -> ${target}"
      else
        echo "integration: ${test} -> ${target}"
      fi
      build_or_rebuild "$target" "$repeat_requested"
    done
  fi
done
