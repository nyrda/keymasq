#!/usr/bin/env bash
set -euo pipefail

if [[ $# -eq 0 ]]; then
    echo "usage: zypper-retry.sh <zypper arguments...>" >&2
    exit 2
fi

# Tumbleweed mirrors can publish repomd.xml before the files it lists while a
# snapshot syncs, so retries have to outlast a sync, not just a network blip.
read -r -a delays <<<"${KEYMASQ_ZYPPER_RETRY_DELAYS:-30 60 120 180 240}"
attempts=$((${#delays[@]} + 1))

for ((attempt = 1; ; attempt++)); do
    status=0
    zypper --non-interactive "$@" || status=$?
    if [[ "$status" -eq 0 ]]; then
        exit 0
    fi
    if [[ "$attempt" -ge "$attempts" ]]; then
        echo "zypper $* failed after $attempt attempts" >&2
        exit "$status"
    fi
    delay="${delays[attempt - 1]}"
    echo "zypper $* failed with status $status (attempt $attempt/$attempts), retrying in ${delay}s" >&2
    sleep "$delay"
    zypper --non-interactive refresh --force || true
done
