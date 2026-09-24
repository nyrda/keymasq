#!/bin/sh
set -e

# Apply packaged sysusers/tmpfiles data when the host allows it.
systemd-sysusers /usr/lib/sysusers.d/keymasq.conf >/dev/null 2>&1 || true
systemd-tmpfiles --create /usr/lib/tmpfiles.d/keymasq.conf >/dev/null 2>&1 || true

# Reload udev rules
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger --subsystem-match=input --sysname-match='event*' --action=change 2>/dev/null || true
udevadm trigger --subsystem-match=input --sysname-match='js*' --action=change 2>/dev/null || true
udevadm trigger --subsystem-match=misc --sysname-match=uinput --action=change 2>/dev/null || true
udevadm trigger --subsystem-match=hidraw --action=change 2>/dev/null || true
udevadm settle --timeout=30 2>/dev/null || true

# Reload systemd
systemctl daemon-reload 2>/dev/null || true

# On RPM upgrades, $1 is greater than 1. Restart only an already-running daemon;
# do not start keymasqd on first install.
if [ "${1:-1}" -gt 1 ]; then
    systemctl try-restart keymasqd.service 2>/dev/null || true
fi

# Bump mtimes so desktop environments that watch icon/theme changes
# can notice the new launcher assets without a full session restart.
find /usr/share/icons/hicolor -path '*/apps/tools.keymasq.keymasq.*' -exec touch {} + >/dev/null 2>&1 || true
touch /usr/share/icons/hicolor 2>/dev/null || true
touch /usr/share/applications/tools.keymasq.keymasq.desktop 2>/dev/null || true

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

# The AppImage's /etc units override this package's units.
if [ -e /opt/keymasq/version ]; then
    echo "" >&2
    echo "Keymasq AppImage integration in /opt/keymasq overrides this package." >&2
    echo "To switch to this package, run as your desktop user:" >&2
    echo "  /opt/keymasq/bin/keymasq --uninstall" >&2
    echo "  sudo systemctl enable --now keymasqd" >&2
    echo "  systemctl --user enable --now keymasq-session" >&2
fi
