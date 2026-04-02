#!/bin/sh
set -e

# Create keyforge system user and group
systemd-sysusers 2>/dev/null || {
    if ! id keyforge >/dev/null 2>&1; then
        useradd -r -s /usr/sbin/nologin -M keyforge
    fi
    if ! getent group input | grep -q keyforge; then
        usermod -aG input keyforge 2>/dev/null || true
    fi
}

# Create runtime and state directories
systemd-tmpfiles --create 2>/dev/null || {
    install -d -m 0755 -o keyforge -g keyforge /run/keyforge
    install -d -m 0750 -o keyforge -g keyforge /var/lib/keyforge
}

# Reload udev rules
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger --subsystem-match=input --action=add 2>/dev/null || true
udevadm trigger --subsystem-match=misc --action=add 2>/dev/null || true

# Reload systemd
systemctl daemon-reload 2>/dev/null || true

echo ""
echo "Keyforge has been installed!"
echo ""
echo "To use Keyforge:"
echo "  1. Enable the daemon:"
echo "       sudo systemctl enable --now keyforged"
echo ""
echo "  2. Enable the session manager:"
echo "       systemctl --user enable --now keyforge-session"
echo ""
echo "  3. Launch Keyforge:"
echo "       keyforge"
echo ""
echo "Security policy: /etc/keyforge/security.toml"
