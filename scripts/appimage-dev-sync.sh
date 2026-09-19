#!/usr/bin/env bash
# Push the worktree's Python package onto a remote host that has the Keymasq
# AppImage installed (for example a Steam Deck), without rebuilding the AppImage.
#
# The installed runtime stays untouched. A hardlinked clone of it lives in
# /opt/keymasq/dev-runtime with a private copy of the keymasq package, and
# systemd drop-ins point the launchers there through KEYMASQ_APPDIR.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV_HOST="${KEYMASQ_DEV_HOST:-}"
DEV_RUNTIME=/opt/keymasq/dev-runtime
DROPIN_NAME=90-dev-runtime.conf
SSH_OPTIONS=(-o BatchMode=yes -o ConnectTimeout=8)

usage() {
  cat <<EOF
Usage: $(basename "$0") [sync [--no-restart] | revert | status | logs]

  sync     Sync keymasq/ to ${DEV_RUNTIME} and restart the services on it (default).
           --no-restart only stages the files; services keep their current runtime.
  revert   Point the services back at the installed AppImage runtime
  status   Show which runtime the services use and whether they are active
  logs     Follow keymasqd and keymasq-session logs

Host: KEYMASQ_DEV_HOST=user@host (required; SSH key login and passwordless sudo).
The SSH user must be the desktop user that installed the AppImage.
GUI/CLI on the dev runtime: KEYMASQ_APPDIR=${DEV_RUNTIME} /opt/keymasq/bin/keymasq
EOF
}

remote() {
  ssh "${SSH_OPTIONS[@]}" "${DEV_HOST}" "DEV_RUNTIME=${DEV_RUNTIME} DROPIN_NAME=${DROPIN_NAME} bash -s" -- "$@"
}

prepare_runtime() {
  remote <<'EOF'
set -euo pipefail
# The session drop-in and restart go through this user's systemd manager.
if ! systemctl --user cat keymasq-session.service >/dev/null 2>&1; then
  echo "keymasq-session.service is not installed for $(id -un); connect as the desktop user that installed the AppImage." >&2
  exit 1
fi
base="$(readlink -f /opt/keymasq/runtime/current)"
if [[ "$(cat "${DEV_RUNTIME}/.base" 2>/dev/null || true)" != "${base}" ]]; then
  echo "Cloning ${base##*/} into ${DEV_RUNTIME}"
  sudo rm -rf "${DEV_RUNTIME}"
  sudo cp -al "${base}" "${DEV_RUNTIME}"
  package="$(echo "${DEV_RUNTIME}"/lib/python3.*/site-packages/keymasq)"
  # Replace the hardlinked package with a private copy so syncing and chown
  # never reach the installed runtime's inodes.
  sudo rm -rf "${package}"
  sudo cp -a "${base}${package#"${DEV_RUNTIME}"}" "${package}"
  sudo find "${package}" -name __pycache__ -type d -prune -exec rm -rf {} +
  sudo chown -R "$(id -un):" "${package}"
  echo "${base}" | sudo tee "${DEV_RUNTIME}/.base" >/dev/null
fi
echo "${DEV_RUNTIME}"/lib/python3.*/site-packages/keymasq
EOF
}

install_dropins() {
  remote <<'EOF'
set -euo pipefail
dropin="$(printf '[Service]\nEnvironment=KEYMASQ_APPDIR=%s\n' "${DEV_RUNTIME}")"
# The privileged hardware helper is a separate unit and must run the same code.
for unit in keymasqd.service keymasq-hardware@.service; do
  sudo mkdir -p "/etc/systemd/system/${unit}.d"
  echo "${dropin}" | sudo tee "/etc/systemd/system/${unit}.d/${DROPIN_NAME}" >/dev/null
done
mkdir -p ~/.config/systemd/user/keymasq-session.service.d
echo "${dropin}" > ~/.config/systemd/user/keymasq-session.service.d/"${DROPIN_NAME}"
EOF
}

restart_services() {
  remote <<'EOF'
set -euo pipefail
sudo systemctl daemon-reload
systemctl --user daemon-reload
sudo systemctl restart keymasqd.service
systemctl --user restart keymasq-session.service
EOF
}

status() {
  remote <<'EOF'
for scope in "" "--user"; do
  unit=keymasqd.service; [[ -n "${scope}" ]] && unit=keymasq-session.service
  env="$(systemctl ${scope} show "${unit}" -p Environment --value | tr ' ' '\n' | grep KEYMASQ_APPDIR || true)"
  printf '%-24s %-8s %s\n' "${unit}" "$(systemctl ${scope} is-active "${unit}")" "${env:-installed runtime}"
done
EOF
}

command="${1:-sync}"
option="${2:-}"
if [[ -z "${DEV_HOST}" && ! "${command}" =~ ^(-h|--help|help)$ ]]; then
  echo "Set KEYMASQ_DEV_HOST=user@host to the AppImage host." >&2
  exit 2
fi
case "${command}" in
  sync)
    package_path="$(prepare_runtime | tail -n 1)"
    rsync -rlt --delete --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r \
      --exclude=__pycache__ --exclude=/common/build_paths.py \
      --out-format='%o %n' -e "ssh -o BatchMode=yes" \
      "${REPO_ROOT}/keymasq/" "${DEV_HOST}:${package_path}/"
    if [[ "${option}" != "--no-restart" ]]; then
      install_dropins
      restart_services
      status
    fi
    ;;
  revert)
    remote <<'EOF'
set -euo pipefail
sudo rm -f "/etc/systemd/system/keymasqd.service.d/${DROPIN_NAME}" \
  "/etc/systemd/system/keymasq-hardware@.service.d/${DROPIN_NAME}"
rm -f ~/.config/systemd/user/keymasq-session.service.d/"${DROPIN_NAME}"
EOF
    restart_services
    status
    ;;
  status)
    status
    ;;
  logs)
    exec ssh "${SSH_OPTIONS[@]}" -t "${DEV_HOST}" \
      "sudo journalctl -f -n 40 _SYSTEMD_UNIT=keymasqd.service + _SYSTEMD_USER_UNIT=keymasq-session.service"
    ;;
  -h | --help | help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
