#!/bin/sh
set -e

# Stop and disable services before removal
systemctl disable --now keyforged.service 2>/dev/null || true
systemctl daemon-reload 2>/dev/null || true
