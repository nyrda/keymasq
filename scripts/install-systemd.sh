#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: sudo bash scripts/install-systemd.sh [--user <username>] [--python <python3-path>]

Installs Keymasq systemd units and wrappers for source installs.
EOF
}

TARGET_USER="${SUDO_USER:-}"
PYTHON_BIN="$(command -v python3 || true)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)
      TARGET_USER="$2"
      shift 2
      ;;
    --python)
      PYTHON_BIN="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1"
      usage
      exit 1
      ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root (sudo)."
  exit 1
fi

if [[ -z "${TARGET_USER}" ]]; then
  echo "Could not determine target desktop user. Pass --user <username>."
  exit 1
fi

if [[ -z "${PYTHON_BIN}" || ! -x "${PYTHON_BIN}" ]]; then
  echo "python3 not found. Pass --python <python3-path>."
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "Installing Keymasq systemd setup for user: ${TARGET_USER}"

if ! id keymasq >/dev/null 2>&1; then
  useradd -r -s /usr/sbin/nologin -M keymasq
fi

if ! getent group keymasq >/dev/null 2>&1; then
  groupadd --system keymasq
fi

usermod -aG keymasq keymasq >/dev/null 2>&1 || true

rm -f /etc/udev/rules.d/60-keymasq-hide-grabbed.rules
install -Dm644 "${REPO_ROOT}/udev/91-keymasq-acl.rules" /etc/udev/rules.d/91-keymasq-acl.rules
install -Dm644 "${REPO_ROOT}/udev/99-keymasq-hide-grabbed.rules" /etc/udev/rules.d/99-keymasq-hide-grabbed.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=input --action=add
udevadm trigger --subsystem-match=misc --action=add

install -d -m 0750 -o keymasq -g keymasq /var/lib/keymasq

cat >/usr/local/bin/keymasqd-wrapper <<EOF
#!/usr/bin/env bash
exec ${PYTHON_BIN} -m keymasq.keymasqd "\$@"
EOF
chmod 0755 /usr/local/bin/keymasqd-wrapper

cat >/usr/local/bin/keymasq-session-wrapper <<EOF
#!/usr/bin/env bash
exec ${PYTHON_BIN} -m keymasq.session.manager "\$@"
EOF
chmod 0755 /usr/local/bin/keymasq-session-wrapper

cat >/etc/systemd/system/keymasqd.service <<'EOF'
[Unit]
Description=Keymasq Input Remapping Daemon
After=systemd-udevd.service systemd-udev-trigger.service
Wants=systemd-udev-trigger.service

[Service]
Type=simple
User=keymasq
Group=keymasq
SupplementaryGroups=input
Nice=-5
ExecStart=/usr/local/bin/keymasqd-wrapper
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
# Required for udevadm trigger to write sysfs uevent files during source hide/restore.
AmbientCapabilities=CAP_DAC_OVERRIDE
CapabilityBoundingSet=CAP_DAC_OVERRIDE
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
RuntimeDirectory=keymasq
RuntimeDirectoryMode=0755
ReadWritePaths=/run/keymasq /var/lib/keymasq

[Install]
WantedBy=multi-user.target
EOF

USER_HOME="$(getent passwd "${TARGET_USER}" | cut -d: -f6)"
if [[ -z "${USER_HOME}" ]]; then
  echo "Could not resolve home for user ${TARGET_USER}"
  exit 1
fi

install -d -m 0755 -o "${TARGET_USER}" -g "${TARGET_USER}" "${USER_HOME}/.config/systemd/user"
cat >"${USER_HOME}/.config/systemd/user/keymasq-session.service" <<'EOF'
[Unit]
Description=Keymasq Session Manager
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/local/bin/keymasq-session-wrapper
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
chown "${TARGET_USER}:${TARGET_USER}" "${USER_HOME}/.config/systemd/user/keymasq-session.service"

systemctl daemon-reload
systemctl enable --now keymasqd
sudo -u "${TARGET_USER}" systemctl --user daemon-reload
sudo -u "${TARGET_USER}" systemctl --user enable --now keymasq-session

echo "Done. Launch GUI with: keymasq"
