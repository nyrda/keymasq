#!/usr/bin/env bash
set -euo pipefail

sudo systemctl try-restart keymasqd.service || true
systemctl --user try-restart keymasq-session.service || true
