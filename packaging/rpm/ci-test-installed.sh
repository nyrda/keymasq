#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: ci-test-installed.sh <path-to-rpm>" >&2
    exit 2
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PACKAGE_PATH="$1"
SIGNING_KEY_PATH="${KEYFORGE_RPM_SIGNING_PUBLIC_KEY_PATH:-}"

if [[ ! -f "$PACKAGE_PATH" ]]; then
    echo "package not found: $PACKAGE_PATH" >&2
    exit 1
fi

PACKAGE_PATH="$(realpath "$PACKAGE_PATH")"

if [[ -n "$SIGNING_KEY_PATH" ]]; then
    SIGNING_KEY_PATH="$(realpath "$SIGNING_KEY_PATH")"
    rpm --import "$SIGNING_KEY_PATH"
fi

if command -v dnf >/dev/null 2>&1; then
    dnf install -y "$PACKAGE_PATH"
elif command -v zypper >/dev/null 2>&1; then
    if [[ -n "$SIGNING_KEY_PATH" ]]; then
        zypper --non-interactive install "$PACKAGE_PATH"
    else
        zypper --non-interactive install --allow-unsigned-rpm "$PACKAGE_PATH"
    fi
else
    echo "unsupported rpm test environment" >&2
    exit 1
fi

rpm -qpi "$PACKAGE_PATH"
rpm -qpl "$PACKAGE_PATH"

cd "$REPO_DIR"
bash packaging/rpm/pkg-smoke.sh
