#!/bin/sh
set -e

# Reload udev after rule removal
udevadm control --reload-rules 2>/dev/null || true

# Reload systemd after unit removal
systemctl daemon-reload 2>/dev/null || true

# On RPM upgrades, $1 is non-zero because another version remains installed
# after this scriptlet runs. Skip the removal message in that case.
if [ "${1:-0}" -ne 0 ]; then
    exit 0
fi

# Let the remaining udev policy recompute owner, group, and mode.
udevadm trigger --action=change --subsystem-match=input 2>/dev/null || true
udevadm trigger --action=change --sysname-match=uinput 2>/dev/null || true

echo ""
echo "Keymasq has been removed."
echo ""
echo "The 'keymasq' user and group have been preserved."
echo "To remove them:"
echo "  sudo userdel keymasq"
echo "  sudo groupdel keymasq"
echo ""
echo "Configuration files remain in: /etc/keymasq/"
