#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <rpm> [rpm...]" >&2
    exit 2
fi

: "${RPM_SIGNING_KEY_PRIVATE_ASC:?set RPM_SIGNING_KEY_PRIVATE_ASC to the armored private key}"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

export GNUPGHOME="$TMP_DIR/gnupg"
export HOME="$TMP_DIR/home"
RPMDB_PATH="$TMP_DIR/rpmdb"
install -d -m 700 "$GNUPGHOME" "$HOME" "$RPMDB_PATH"

PRIVATE_KEY_PATH="$TMP_DIR/private.asc"
printf '%s\n' "$RPM_SIGNING_KEY_PRIVATE_ASC" > "$PRIVATE_KEY_PATH"
chmod 600 "$PRIVATE_KEY_PATH"

rpm --dbpath "$RPMDB_PATH" --initdb
gpg --batch --import "$PRIVATE_KEY_PATH"

FINGERPRINT="$(
    gpg --batch --with-colons --list-secret-keys \
        | awk -F: '/^fpr:/ { print $10; exit }'
)"

if [[ -z "$FINGERPRINT" ]]; then
    echo "failed to determine imported signing key fingerprint" >&2
    exit 1
fi

PUBLIC_KEY_PATH="${RPM_SIGNING_PUBLIC_KEY_PATH:-dist/rpm-signing-key.asc}"
install -d "$(dirname "$PUBLIC_KEY_PATH")"
gpg --batch --armor --export "$FINGERPRINT" > "$PUBLIC_KEY_PATH"
rpm --dbpath "$RPMDB_PATH" --import "$PUBLIC_KEY_PATH"

cat > "$HOME/.rpmmacros" <<EOF
%_signature gpg
%_gpg_name $FINGERPRINT
%__gpg $(command -v gpg)
%_gpg_digest_algo sha256
%_dbpath $RPMDB_PATH
EOF

for rpm_path in "$@"; do
    rpmsign --addsign "$rpm_path"
done

rpm --dbpath "$RPMDB_PATH" --checksig "$@"
