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

Examples:
  ./scripts/integration.sh cosmic
  ./scripts/integration.sh daemon-session
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

if [[ $# -eq 0 ]]; then
  usage
  exit 0
fi

case "$1" in
  -h|--help|help|list|--list)
    usage
    exit 0
    ;;
esac

tests=()
for arg in "$@"; do
  case "$arg" in
    -h|--help|help|list|--list)
      usage
      exit 0
      ;;
  esac

  while IFS= read -r expanded; do
    [[ -n "$expanded" ]] || continue
    tests+=("$expanded")
  done < <(expand_group "$arg")
done

for test in "${tests[@]}"; do
  if ! check_name="$(check_name_for_test "$test")"; then
    echo "Unknown integration test: $test" >&2
    echo >&2
    usage >&2
    exit 1
  fi

  target="path:.#checks.${SYSTEM}.${check_name}"
  echo "integration: ${test} -> ${target}"
  nix build "$target"
done
