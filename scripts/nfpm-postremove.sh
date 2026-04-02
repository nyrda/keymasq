#!/bin/sh
set -e

# Reload udev after rule removal
udevadm control --reload-rules 2>/dev/null || true

# Reload systemd after unit removal
systemctl daemon-reload 2>/dev/null || true

echo ""
echo "Keyforge has been removed."
echo ""
echo "The 'keyforge' user and group have been preserved."
echo "To remove them:"
echo "  sudo userdel keyforge"
echo "  sudo groupdel keyforge"
echo ""
echo "Configuration files remain in: /etc/keyforge/"
