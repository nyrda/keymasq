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

echo ""
echo "Keyforge has been removed."
echo ""
echo "The 'keyforge' user and group have been preserved."
echo "To remove them:"
echo "  sudo userdel keyforge"
echo "  sudo groupdel keyforge"
echo ""
echo "Configuration files remain in: /etc/keyforge/"
