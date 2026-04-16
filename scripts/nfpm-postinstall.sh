#!/bin/sh
set -e

# Create keymasq system user and group
systemd-sysusers 2>/dev/null || {
    if ! id keymasq >/dev/null 2>&1; then
        useradd -r -s /usr/sbin/nologin -M keymasq
    fi
    if ! getent group input | grep -q keymasq; then
        usermod -aG input keymasq 2>/dev/null || true
    fi
}

# Create runtime and state directories
systemd-tmpfiles --create 2>/dev/null || {
    install -d -m 0755 -o keymasq -g keymasq /run/keymasq
    install -d -m 0750 -o keymasq -g keymasq /var/lib/keymasq
}

# Reload udev rules
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger --subsystem-match=input --action=add 2>/dev/null || true
udevadm trigger --subsystem-match=misc --action=add 2>/dev/null || true

# Reload systemd
systemctl daemon-reload 2>/dev/null || true

# Bump mtimes so desktop environments that watch icon/theme changes
# can notice the new launcher assets without a full session restart.
find /usr/share/icons/hicolor -path '*/apps/keymasq.*' -exec touch {} + >/dev/null 2>&1 || true
touch /usr/share/icons/hicolor 2>/dev/null || true
touch /usr/share/applications/io.github.nyrda.Keymasq.desktop 2>/dev/null || true

echo ""
if [ "${1:-1}" -gt 1 ]; then
    echo "Keymasq has been upgraded!"
else
    echo "Keymasq has been installed!"
fi
echo ""
echo "To use Keymasq:"
echo "  1. Enable the daemon:"
echo "       sudo systemctl enable --now keymasqd"
echo ""
echo "  2. Enable the session manager:"
echo "       systemctl --user enable --now keymasq-session"
echo ""
echo "  3. Launch Keymasq:"
echo "       keymasq"
echo ""
echo "Security policy: /etc/keymasq/security.toml"
