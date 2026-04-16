#!/usr/bin/env bash
set -euo pipefail

# Restart system daemon first.
systemctl try-restart keymasqd.service || true

# Restart user session service for active users.
if command -v loginctl >/dev/null 2>&1; then
  # list-sessions format: SESSION UID USER SEAT ...
  while read -r _sid uid user _rest; do
    if [[ -z "${uid}" || -z "${user}" || "${uid}" == "UID" ]]; then
      continue
    fi

    runtime_dir="/run/user/${uid}"
    bus_path="${runtime_dir}/bus"
    if [[ ! -d "${runtime_dir}" || ! -S "${bus_path}" ]]; then
      continue
    fi

    if command -v runuser >/dev/null 2>&1; then
      runuser -u "${user}" -- env \
        XDG_RUNTIME_DIR="${runtime_dir}" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=${bus_path}" \
        systemctl --user try-restart keymasq-session.service || true
    else
      sudo -u "${user}" env \
        XDG_RUNTIME_DIR="${runtime_dir}" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=${bus_path}" \
        systemctl --user try-restart keymasq-session.service || true
    fi
  done < <(loginctl list-sessions --no-legend 2>/dev/null || true)
fi
