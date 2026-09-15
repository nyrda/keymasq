#!/usr/bin/env bash
# Run this checkout's root helper under systemd, including watchdog recovery.
set -euo pipefail

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "${SCRIPT_PATH}")/.." && pwd)"

if [[ "${1:-}" == --root ]]; then
  if [[ "${EUID}" -ne 0 || $# -ne 4 ]]; then
    echo "Internal invocation requires root, source path, Python path and tool path." >&2
    exit 2
  fi
  source_root="$2"
  python_executable="$3"
  tool_path="$4"
  stage_root=/run/keymasq-maskd-dev-source
  # Stop clients before replacing the helper. Its normal recovery restores any
  # existing reservation, and the journal handles interrupted transitions.
  for unit in keymasqd.service keymasq-maskd.service; do
    if systemctl cat "${unit}" >/dev/null 2>&1; then
      systemctl stop "${unit}"
    fi
  done
  systemctl stop keymasq-maskd-dev.service 2>/dev/null || true
  install -d -m 0755 -o root -g root "${stage_root}"
  rm -rf "${stage_root}/keymasq"
  cp -R "${source_root}/keymasq" "${stage_root}/keymasq"
  chown -R root:root "${stage_root}"
  chmod -R go-w "${stage_root}"
  systemd-run --unit=keymasq-maskd-dev --collect \
    --service-type=notify \
    --property=WatchdogSec=20s \
    --property=Restart=on-failure \
    --property=RestartSec=2s \
    --property=TimeoutStopSec=30s \
    --property=KillMode=mixed \
    --property=NoNewPrivileges=yes \
    --property="CapabilityBoundingSet=CAP_DAC_OVERRIDE CAP_CHOWN CAP_FOWNER CAP_KILL CAP_SYS_PTRACE" \
    --property=ProtectSystem=strict \
    --property=ProtectHome=yes \
    --property=PrivateTmp=yes \
    --property="RestrictAddressFamilies=AF_UNIX AF_NETLINK" \
    --property=RuntimeDirectory=keymasq-masking \
    --property=RuntimeDirectoryPreserve=yes \
    --property=StateDirectory=keymasq-masking \
    --property="ReadWritePaths=/run /var/lib/keymasq-masking" \
    --property="ExecStopPost=${python_executable} -m keymasq.masking.service --recover-offline" \
    --setenv="PYTHONPATH=${stage_root}" \
    --setenv="PATH=${tool_path}" \
    "${python_executable}" -m keymasq.masking.service
  echo "Worktree masking helper started. Start ./scripts/dev.sh and open Device masking."
  echo "Logs: journalctl -fu keymasq-maskd-dev"
  echo "Restore installed services after stopping the dev workspace:"
  echo "sudo systemctl stop keymasq-maskd-dev; sudo systemctl start keymasq-maskd keymasqd"
  exit 0
fi

if [[ -z "${IN_NIX_SHELL:-}" ]]; then
  exec nix develop "${REPO_ROOT}" -c bash "${SCRIPT_PATH}" "$@"
fi
if [[ -x /run/wrappers/bin/sudo ]]; then
  sudo_command=/run/wrappers/bin/sudo
else
  sudo_command="$(command -v sudo)"
fi
exec "${sudo_command}" "$(command -v bash)" "${SCRIPT_PATH}" --root \
  "${REPO_ROOT}" "$(command -v python)" "${PATH}"
