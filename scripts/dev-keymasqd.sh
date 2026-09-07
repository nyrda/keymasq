#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${SCRIPT_PATH}")/.." && pwd)}"

if [[ ! -f "${REPO_ROOT}/flake.nix" ]]; then
  echo "flake.nix not found under: ${REPO_ROOT}" >&2
  exit 1
fi

if [[ -z "${IN_NIX_SHELL:-}" ]]; then
  exec nix develop "${REPO_ROOT}" -c "${SCRIPT_PATH}" "$@"
fi

source "${REPO_ROOT}/scripts/dev-shell-env.sh"
normalize_dev_shell_for_pkexec

host_command() {
  # Match optional NixOS sudo rules even when the worktree's Nix shell uses
  # another nixpkgs pin. Other hosts keep using their normal PATH commands.
  local name="$1"
  if [[ -x "/run/current-system/sw/bin/${name}" ]]; then
    printf '/run/current-system/sw/bin/%s\n' "${name}"
  else
    command -v "${name}"
  fi
}

if [[ -x /run/wrappers/bin/sudo ]]; then
  SUDO_COMMAND=/run/wrappers/bin/sudo
else
  SUDO_COMMAND="$(command -v sudo)"
fi
SYSTEMCTL_COMMAND="$(host_command systemctl || true)"
INSTALL_COMMAND="$(host_command install)"
ENV_COMMAND="$(host_command env)"

# Normal sudo uses a matching NOPASSWD rule automatically, and otherwise
# authenticates as usual. Do not retry executed commands on failure: a daemon
# exit is not an indication that passwordless authorization was unavailable.
stop_installed_daemon_service() {
  if [[ -z "${SYSTEMCTL_COMMAND}" ]]; then
    return
  fi

  if ! "${SYSTEMCTL_COMMAND}" is-active --quiet keymasqd.service; then
    return
  fi

  if [[ "${EUID}" -eq 0 ]]; then
    "${SYSTEMCTL_COMMAND}" stop keymasqd.service
  else
    "${SUDO_COMMAND}" "${SYSTEMCTL_COMMAND}" stop keymasqd.service
  fi
}

prepare_runtime_dirs() {
  if [[ "${EUID}" -eq 0 ]]; then
    "${INSTALL_COMMAND}" -d -m 0755 -o keymasq -g keymasq /run/keymasq
    "${INSTALL_COMMAND}" -d -m 0750 -o keymasq -g keymasq /var/lib/keymasq
  else
    "${SUDO_COMMAND}" "${INSTALL_COMMAND}" -d -m 0755 -o keymasq -g keymasq /run/keymasq
    "${SUDO_COMMAND}" "${INSTALL_COMMAND}" -d -m 0750 -o keymasq -g keymasq /var/lib/keymasq
  fi
}

stage_source_checkout() {
  local repo_id
  local stage_root

  if command -v sha256sum >/dev/null 2>&1; then
    repo_id="$(printf '%s' "${REPO_ROOT}" | sha256sum | cut -c1-12)"
  else
    repo_id="$(printf '%s' "${REPO_ROOT}" | cksum | awk '{print $1}')"
  fi

  stage_root="/tmp/keymasq-dev-keymasqd-${USER:-$(id -un)}-${repo_id}"
  rm -rf "${stage_root}"
  mkdir -p "${stage_root}"
  chmod 0755 "${stage_root}"
  cp -R "${REPO_ROOT}/keymasq" "${stage_root}/"

  printf '%s\n' "${stage_root}"
}

STAGED_PYTHONPATH="$(stage_source_checkout)"
export PYTHONPATH="${STAGED_PYTHONPATH}${PYTHONPATH:+:${PYTHONPATH}}"

stop_installed_daemon_service
prepare_runtime_dirs

exec "${SUDO_COMMAND}" -u keymasq "${ENV_COMMAND}" \
  HOME=/var/lib/keymasq \
  PATH="${PATH}" \
  PYTHONPATH="${PYTHONPATH}" \
  python -m keymasq.keymasqd "$@"
