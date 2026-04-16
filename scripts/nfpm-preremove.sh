#!/bin/sh
set -e

# On RPM upgrades, $1 is non-zero because another version of the package will
# remain installed after this scriptlet completes. Avoid stopping/disabling the
# service in that case.
if [ "${1:-0}" -eq 0 ]; then
    systemctl disable --now keymasqd.service 2>/dev/null || true
    systemctl daemon-reload 2>/dev/null || true
fi
