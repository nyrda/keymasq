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

stop_installed_daemon_service() {
  if ! command -v systemctl >/dev/null 2>&1; then
    return
  fi

  if ! systemctl is-active --quiet keymasqd.service; then
    return
  fi

  if [[ "${EUID}" -eq 0 ]]; then
    systemctl stop keymasqd.service
  else
    sudo systemctl stop keymasqd.service
  fi
}

prepare_runtime_dirs() {
  if [[ "${EUID}" -eq 0 ]]; then
    install -d -m 0755 -o keymasq -g keymasq /run/keymasq
    install -d -m 0750 -o keymasq -g keymasq /var/lib/keymasq
  else
    sudo install -d -m 0755 -o keymasq -g keymasq /run/keymasq
    sudo install -d -m 0750 -o keymasq -g keymasq /var/lib/keymasq
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

exec sudo -u keymasq env \
  HOME=/var/lib/keymasq \
  PATH="${PATH}" \
  PYTHONPATH="${PYTHONPATH}" \
  python -m keymasq.keymasqd "$@"
