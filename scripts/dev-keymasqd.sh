#!/usr/bin/env bash
# Run the worktree daemon in the foreground; only hardware jobs use systemd.
set -euo pipefail
SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${SCRIPT_PATH}")/.." && pwd)}"
if [[ "${1:-}" == --root ]]; then
  [[ "${EUID}" -eq 0 && $# -ge 4 ]] || exit 2
  source_root="$2"
  python_executable="$3"
  tool_path="$4"
  shift 4
  export PATH="${tool_path}"
  exec 9>/run/keymasq-dev.lock
  flock -n 9 || { echo "Another development daemon launcher is still running." >&2; exit 1; }
  stage_root="$(mktemp -d /run/keymasq-dev-source.XXXXXX)"
  declare -a changed_paths=()
  cleanup() {
    local result=$? path index
    trap - EXIT
    trap '' INT TERM
    if [[ -x "$stage_root/run-record" ]]; then
      systemctl stop 'keymasq-hardware@*.service' || result=1
      "$stage_root/run-record" recover-hardware || result=1
    fi
    for ((index=${#changed_paths[@]}-1; index>=0; index--)); do
      path="${changed_paths[index]}"
      rm -f -- "$path"
      if [[ -e "$stage_root/backup/$index" || -L "$stage_root/backup/$index" ]]; then
        cp -a -- "$stage_root/backup/$index" "$path" || result=1
      fi
    done
    systemctl daemon-reload || result=1
    rm -rf -- "$stage_root"
    exit "$result"
  }
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  install -d "$stage_root/backup"
  for path in \
    /run/systemd/system.control/keymasq-hardware@.service \
    /run/systemd/system/keymasq-hardware@.service.d/90-worktree.conf \
    /etc/polkit-1/rules.d/49-keymasq-hardware-dev.rules; do
    if [[ -e "$path" || -L "$path" ]]; then
      cp -a -- "$path" "$stage_root/backup/${#changed_paths[@]}"
    fi
    changed_paths+=("$path")
  done
  # An outgoing daemon's late hardware request must not cancel this stop.
  systemctl stop --job-mode=replace-irreversibly keymasqd.service
  # Do not overwrite the targets of pre-existing configuration symlinks.
  rm -f -- "${changed_paths[@]}"
  install -d -m 0755 -o root -g root "$stage_root"
  cp -R "$source_root/keymasq" "$stage_root/keymasq"
  chown -R root:root "$stage_root/keymasq"
  chmod -R go-w "$stage_root/keymasq"
  {
    printf '#!%s\n' "$(command -v bash)"
    printf 'export PYTHONPATH=%q\n' "$stage_root"
    printf 'export PATH=%q\n' "$tool_path"
    printf 'exec %q -P -m keymasq.record "$@"\n' "$python_executable"
  } > "$stage_root/run-record"
  chmod 0755 "$stage_root/run-record"
  install -d /run/systemd/system.control /run/systemd/system/keymasq-hardware@.service.d
  # Foreground development has no parent service. The EXIT trap handles cleanup.
  sed '/^BindsTo=/d; /^After=/d' "$source_root/systemd/keymasq-hardware@.service" \
    > /run/systemd/system.control/keymasq-hardware@.service
  cat > /run/systemd/system/keymasq-hardware@.service.d/90-worktree.conf <<UNIT
[Service]
ExecStart=
ExecStart=$stage_root/run-record hardware-operation %i
UNIT
  install -Dm644 "$source_root/polkit/49-keymasq-hardware.rules" /etc/polkit-1/rules.d/49-keymasq-hardware-dev.rules
  systemctl daemon-reload
  "$stage_root/run-record" recover-hardware
  install -d -m 0755 -o keymasq -g keymasq /run/keymasq
  install -d -m 0750 -o keymasq -g keymasq /var/lib/keymasq
  # Match the installed daemon's account and sole capability, without a service
  # or watchdog. Ctrl+C returns here and restores hardware access.
  env -u WATCHDOG_USEC -u WATCHDOG_PID -u NOTIFY_SOCKET \
    HOME=/var/lib/keymasq PYTHONPATH="$stage_root" \
    setpriv --reuid=keymasq --regid=keymasq --init-groups \
    --bounding-set=-all,+dac_override --inh-caps=+dac_override --ambient-caps=+dac_override \
    "$python_executable" -P -m keymasq.keymasqd "$@"
  exit 0
fi
if [[ -z "${IN_NIX_SHELL:-}" ]]; then
  exec nix develop "$REPO_ROOT" -c "$SCRIPT_PATH" "$@"
fi
source "$REPO_ROOT/scripts/dev-shell-env.sh"
normalize_dev_shell_for_pkexec
if [[ -x /run/wrappers/bin/sudo ]]; then
  sudo_command=/run/wrappers/bin/sudo
else
  sudo_command="$(command -v sudo)"
fi
exec "$sudo_command" "$(command -v bash)" "$SCRIPT_PATH" --root \
  "$REPO_ROOT" "$(command -v python)" "$PATH" "$@"
