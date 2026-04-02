#!/usr/bin/env bash
set -euo pipefail

NFPM_VERSION="${NFPM_VERSION:-2.43.3}"
ARCHIVE="nfpm_${NFPM_VERSION}_Linux_x86_64.tar.gz"
URL="https://github.com/goreleaser/nfpm/releases/download/v${NFPM_VERSION}/${ARCHIVE}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

curl -fsSL "$URL" -o "$TMP_DIR/$ARCHIVE"
tar -xzf "$TMP_DIR/$ARCHIVE" -C "$TMP_DIR" nfpm
install -m 0755 "$TMP_DIR/nfpm" /usr/local/bin/nfpm
