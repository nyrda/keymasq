#!/usr/bin/env bash
# Run the worktree daemon under systemd so watchdog and recovery match installation.
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
  stage_root=/run/keymasq-dev-source
  systemctl stop keymasqd.service
  # Retire services left by the earlier, unreleased masking experiment.
  for unit in keymasq-maskd-dev.service keymasq-maskd.service; do
    if systemctl cat "$unit" >/dev/null 2>&1; then
      systemctl stop "$unit"
    fi
  done
  install -d -m 0755 -o root -g root "$stage_root"
  rm -rf "$stage_root/keymasq"
  cp -R "$source_root/keymasq" "$stage_root/keymasq"
  chown -R root:root "$stage_root"
  chmod -R go-w "$stage_root"
  for name in daemon record; do
    {
      printf '#!%s\n' "$(command -v bash)"
      printf 'export PYTHONPATH=%q\n' "$stage_root"
      printf 'export PATH=%q\n' "$tool_path"
      if [[ "$name" == daemon ]]; then
        printf 'exec %q -P -m keymasq.keymasqd' "$python_executable"
        printf ' %q' "$@"
        printf '\n'
      else
        printf 'exec %q -P -m keymasq.record "$@"\n' "$python_executable"
      fi
    } > "$stage_root/run-$name"
    chmod 0755 "$stage_root/run-$name"
  done
  install -d /run/systemd/system/keymasqd.service.d /run/systemd/system/keymasq-hardware@.service.d
  # Supply units for hosts whose installed version predates masking support.
  cp "$source_root/systemd/keymasqd.service" /run/systemd/system/keymasqd.service
  cp "$source_root/systemd/keymasq-hardware@.service" /run/systemd/system/keymasq-hardware@.service
  cat > /run/systemd/system/keymasqd.service.d/90-worktree.conf <<UNIT
[Service]
Type=notify
WatchdogSec=20
WatchdogSignal=SIGKILL
TimeoutStopSec=20
ExecStartPre=
ExecStartPre=+$stage_root/run-record recover-hardware
ExecStart=
ExecStart=$stage_root/run-daemon
ExecStopPost=
ExecStopPost=+$stage_root/run-record recover-hardware
AmbientCapabilities=CAP_DAC_OVERRIDE
UNIT
  cat > /run/systemd/system/keymasq-hardware@.service.d/90-worktree.conf <<UNIT
[Service]
ExecStart=
ExecStart=$stage_root/run-record hardware-operation %i
UNIT
  install -Dm644 "$source_root/polkit/49-keymasq-hardware.rules" /etc/polkit-1/rules.d/49-keymasq-hardware-dev.rules
  systemctl daemon-reload
  systemctl start keymasqd.service
  echo "Worktree daemon is running with watchdog recovery and short-lived hardware jobs."
  trap 'systemctl stop keymasqd.service' EXIT
  journalctl -fu keymasqd.service
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
