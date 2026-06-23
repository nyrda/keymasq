#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

SYSTEM="x86_64-linux"

DAEMON_SESSION_TEST_ENTRY="daemon-session|daemon-session-integration-test|daemon/session runtime integration suite|daemon-session-integration"
LISTENER_TEST_ENTRIES=(
  "gnome-bridge|listener-vm-gnome-bridge|GNOME Shell bridge listener preflight|"
  "gnome|listener-vm-gnome|GNOME listener|"
  "kde|listener-vm-kde|KDE Plasma listener|plasma"
  "hyprland|listener-vm-hyprland|Hyprland listener|"
  "niri|listener-vm-niri|Niri listener|"
  "xfce|listener-vm-xfce|X11/XFCE listener|x11"
  "cosmic|listener-vm-cosmic|COSMIC listener|"
  "sway|listener-vm-sway|wlroots/Sway listener|wayland wlroots"
)
INTEGRATION_TEST_ENTRIES=(
  "$DAEMON_SESSION_TEST_ENTRY"
  "${LISTENER_TEST_ENTRIES[@]}"
)

print_test_entry() {
  local entry="$1"
  local name check_name description aliases
  IFS='|' read -r name check_name description aliases <<< "$entry"
  printf '  %-15s %s\n' "$name" "$description"
}

print_test_names() {
  local entry
  local name check_name description aliases
  for entry in "$@"; do
    IFS='|' read -r name check_name description aliases <<< "$entry"
    printf '%s\n' "$name"
  done
}

usage() {
  cat <<'EOF'
Usage: ./scripts/integration.sh [--evdev current|1.6.1|1.7.0] [test ...]

Runs Keymasq NixOS VM integration checks through nix build.

Tests:
EOF
  for entry in "${INTEGRATION_TEST_ENTRIES[@]}"; do
    print_test_entry "$entry"
  done
  cat <<'EOF'
  listeners       all listener VM tests
  all             daemon-session plus all listener VM tests

Options:
  -s, --scenario NAME[,NAME]  Run selected daemon-session scenario keys
  -r, --repeat N              Repeat selected integration checks N times
      --evdev VERSION         Use current, 1.6.1, or 1.7.0 for daemon-session checks

Examples:
  ./scripts/integration.sh cosmic
  ./scripts/integration.sh daemon-session
  ./scripts/integration.sh daemon-session --evdev 1.6.1
  ./scripts/integration.sh daemon-session --scenario hotplug-replug
  ./scripts/integration.sh --scenario profile-lifetime-direct-actions --repeat 10
  ./scripts/integration.sh cosmic --repeat 3
  ./scripts/integration.sh listeners

Use path: flake references so newly added or uncommitted VM files are included.
EOF
}

check_name_for_test() {
  local requested="$1"
  local entry
  local name check_name description aliases alias

  for entry in "${INTEGRATION_TEST_ENTRIES[@]}"; do
    IFS='|' read -r name check_name description aliases <<< "$entry"
    if [[ "$requested" == "$name" || "$requested" == "$check_name" ]]; then
      printf '%s\n' "$check_name"
      return 0
    fi
    for alias in $aliases; do
      if [[ "$requested" == "$alias" ]]; then
        printf '%s\n' "$check_name"
        return 0
      fi
    done
  done

  return 1
}

expand_group() {
  case "$1" in
    listeners)
      print_test_names "${LISTENER_TEST_ENTRIES[@]}"
      ;;
    all)
      print_test_names "$DAEMON_SESSION_TEST_ENTRY" "${LISTENER_TEST_ENTRIES[@]}"
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

normalize_evdev_lane() {
  case "${1:-current}" in
    current|latest|default)
      printf '%s\n' "current"
      ;;
    1.6|1.6.1|161|evdev161)
      printf '%s\n' "evdev161"
      ;;
    1.7|1.7.0|170|evdev170)
      printf '%s\n' "evdev170"
      ;;
    *)
      echo "Unsupported evdev lane: $1" >&2
      echo "Use current, 1.6.1, or 1.7.0" >&2
      exit 1
      ;;
  esac
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
evdev_lane="current"
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
    --evdev)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --evdev" >&2
        exit 1
      fi
      shift
      evdev_lane="$(normalize_evdev_lane "$1")"
      ;;
    --evdev=*)
      evdev_lane="$(normalize_evdev_lane "${1#*=}")"
      ;;
    *)
      raw_tests+=("$1")
      ;;
  esac
  shift
done

tests=()
if [[ ${#raw_tests[@]} -eq 0 && (-n "$scenario_filter" || -n "$repeat_count" || "$evdev_lane" != "current") ]]; then
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

  if [[ "$evdev_lane" != "current" && "$check_name" != daemon-session* ]]; then
    echo "Evdev compatibility lanes are only supported for daemon-session tests" >&2
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
  selected_daemon_check=0
  if [[ "$check_name" == "daemon-session-integration-test" && (-n "$scenario_filter" || -n "$repeat_count") ]]; then
    check_name="daemon-session-selected-integration-test"
    selected_daemon_check=1
    nix_args+=(--impure)
  fi
  if [[ "$evdev_lane" != "current" && "$check_name" == daemon-session* ]]; then
    check_name="${check_name}-${evdev_lane}"
  fi

  target="path:.#checks.${SYSTEM}.${check_name}"
  if [[ "$selected_daemon_check" == "1" ]]; then
    echo "integration: ${test} scenarios=${scenario_filter:-all} repeat=${run_count} evdev=${evdev_lane} -> ${target}"
    (
      export KEYMASQ_INTEGRATION_SCENARIOS="$scenario_filter"
      export KEYMASQ_INTEGRATION_REPEAT="${repeat_count:-$run_count}"
      build_or_rebuild "$target" "$repeat_requested" "${nix_args[@]}"
    )
  else
    for iteration in $(seq 1 "$run_count"); do
      if [[ "$run_count" -gt 1 ]]; then
        echo "integration: ${test} repeat ${iteration}/${run_count} evdev=${evdev_lane} -> ${target}"
      else
        echo "integration: ${test} evdev=${evdev_lane} -> ${target}"
      fi
      build_or_rebuild "$target" "$repeat_requested"
    done
  fi
done
