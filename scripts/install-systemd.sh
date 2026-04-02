#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: sudo bash scripts/install-systemd.sh [--user <username>] [--python <python3-path>]

Installs Keyforge systemd units and wrappers for source installs.
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

echo "Installing Keyforge systemd setup for user: ${TARGET_USER}"

if ! id keyforge >/dev/null 2>&1; then
  useradd -r -s /usr/sbin/nologin -M keyforge
fi

if ! getent group keyforge >/dev/null 2>&1; then
  groupadd --system keyforge
fi

usermod -aG keyforge keyforge >/dev/null 2>&1 || true

install -Dm644 "${REPO_ROOT}/udev/91-keyforge-acl.rules" /etc/udev/rules.d/91-keyforge-acl.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=input --action=add
udevadm trigger --subsystem-match=misc --action=add

install -d -m 0755 -o keyforge -g keyforge /run/keyforge
install -d -m 0750 -o keyforge -g keyforge /var/lib/keyforge

cat >/usr/local/bin/keyforged-wrapper <<EOF
#!/usr/bin/env bash
exec ${PYTHON_BIN} -m keyforge.keyforged "\$@"
EOF
chmod 0755 /usr/local/bin/keyforged-wrapper

cat >/usr/local/bin/keyforge-session-wrapper <<EOF
#!/usr/bin/env bash
exec ${PYTHON_BIN} -m keyforge.session.manager "\$@"
EOF
chmod 0755 /usr/local/bin/keyforge-session-wrapper

cat >/etc/systemd/system/keyforged.service <<'EOF'
[Unit]
Description=Keyforge Input Remapping Daemon
After=systemd-udev-settle.service
Requires=systemd-udev-settle.service

[Service]
Type=simple
User=keyforge
Group=keyforge
SupplementaryGroups=input
ExecStart=/usr/local/bin/keyforged-wrapper
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/run/keyforge /var/lib/keyforge

[Install]
WantedBy=multi-user.target
EOF

USER_HOME="$(getent passwd "${TARGET_USER}" | cut -d: -f6)"
if [[ -z "${USER_HOME}" ]]; then
  echo "Could not resolve home for user ${TARGET_USER}"
  exit 1
fi

install -d -m 0755 -o "${TARGET_USER}" -g "${TARGET_USER}" "${USER_HOME}/.config/systemd/user"
cat >"${USER_HOME}/.config/systemd/user/keyforge-session.service" <<'EOF'
[Unit]
Description=Keyforge Session Manager
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/local/bin/keyforge-session-wrapper
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
chown "${TARGET_USER}:${TARGET_USER}" "${USER_HOME}/.config/systemd/user/keyforge-session.service"

systemctl daemon-reload
systemctl enable --now keyforged
sudo -u "${TARGET_USER}" systemctl --user daemon-reload
sudo -u "${TARGET_USER}" systemctl --user enable --now keyforge-session

echo "Done. Launch GUI with: keyforge"
