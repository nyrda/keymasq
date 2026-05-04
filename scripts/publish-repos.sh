#!/usr/bin/env bash
set -euo pipefail

: "${RPM_SIGNING_KEY_PRIVATE_ASC:?set RPM_SIGNING_KEY_PRIVATE_ASC}"
: "${REPO_UPLOAD_URL:?set REPO_UPLOAD_URL (e.g. https://repo.keymasq.tools)}"
: "${REPO_UPLOAD_TOKEN:?set REPO_UPLOAD_TOKEN}"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

export GNUPGHOME="$WORK_DIR/gnupg"
install -d -m 700 "$GNUPGHOME"

REPO_DIR="$WORK_DIR/repo"
mkdir -p "$REPO_DIR"

# -- Import GPG key --

printf '%s\n' "$RPM_SIGNING_KEY_PRIVATE_ASC" | gpg --batch --import

FINGERPRINT="$(
    gpg --batch --with-colons --list-secret-keys \
        | awk -F: '/^fpr:/ { print $10; exit }'
)"
if [[ -z "$FINGERPRINT" ]]; then
    echo "Failed to determine GPG key fingerprint" >&2
    exit 1
fi
echo "Using GPG key: $FINGERPRINT"

# -- Download current repo state --

REPO_UPLOAD_URL="${REPO_UPLOAD_URL%/}"

echo "Downloading current repo snapshot..."
snapshot_code=$(curl -sS -L -w '%{http_code}' -o "$WORK_DIR/snapshot.tar.gz" \
    -H "Authorization: Bearer $REPO_UPLOAD_TOKEN" \
    "$REPO_UPLOAD_URL/api/snapshot")

if [[ "$snapshot_code" == "200" ]]; then
    tar -xzf "$WORK_DIR/snapshot.tar.gz" -C "$REPO_DIR" 2>/dev/null || true
    echo "Existing repo downloaded"
else
    echo "No existing repo (HTTP $snapshot_code), starting fresh"
fi

# -- Set up directory structure --

DEB_POOL="$REPO_DIR/debian/pool/main/k/keymasq"
DEB_DIST="$REPO_DIR/debian/dists/stable"
DEB_BINARY="$DEB_DIST/main/binary-all"
FEDORA_ROOT="$REPO_DIR/fedora"
OPENSUSE_DIR="$REPO_DIR/opensuse"
RELEASES_DIR="$REPO_DIR/releases"

mkdir -p "$DEB_POOL" "$DEB_BINARY" "$FEDORA_ROOT" "$OPENSUSE_DIR" "$RELEASES_DIR"

# -- Copy new packages --

deb_count=0
for deb in dist/*.deb; do
    [ -f "$deb" ] || continue
    cp "$deb" "$DEB_POOL/"
    echo "Added $(basename "$deb") to debian pool"
    deb_count=$((deb_count + 1))
done

rpm_count=0
for rpm in dist/*.rpm; do
    [ -f "$rpm" ] || continue
    name="$(basename "$rpm")"
    if [[ "$name" =~ \.fc([0-9]+)\. ]]; then
        fedora_release="${BASH_REMATCH[1]}"
        fedora_dir="$FEDORA_ROOT/$fedora_release"
        mkdir -p "$fedora_dir"
        cp "$rpm" "$fedora_dir/"
        echo "Added $name to fedora $fedora_release repo"
    elif [[ "$name" == *".fedora."* ]]; then
        fedora_dir="$FEDORA_ROOT/generic"
        mkdir -p "$fedora_dir"
        cp "$rpm" "$fedora_dir/"
        echo "Added $name to generic fedora repo"
    elif [[ "$name" == *".opensuse."* ]]; then
        cp "$rpm" "$OPENSUSE_DIR/"
        echo "Added $name to opensuse repo"
    else
        echo "Skipping unknown RPM: $name"
        continue
    fi
    rpm_count=$((rpm_count + 1))
done

source_count=0
for archive in dist/*.tar.gz; do
    [ -f "$archive" ] || continue
    cp "$archive" "$RELEASES_DIR/"
    echo "Added $(basename "$archive") to release archives"
    source_count=$((source_count + 1))
done

if [[ $deb_count -eq 0 && $rpm_count -eq 0 && $source_count -eq 0 ]]; then
    echo "No packages found in dist/, nothing to publish"
    exit 0
fi

# -- Rebuild APT metadata --

if [[ $deb_count -gt 0 ]]; then
    echo "Rebuilding APT metadata..."
    (cd "$REPO_DIR/debian" && dpkg-scanpackages --multiversion pool/main/k/keymasq) \
        > "$DEB_BINARY/Packages"
    gzip -k -f "$DEB_BINARY/Packages"

    apt-ftparchive \
        -o "APT::FTPArchive::Release::Suite=stable" \
        -o "APT::FTPArchive::Release::Codename=stable" \
        -o "APT::FTPArchive::Release::Architectures=all" \
        -o "APT::FTPArchive::Release::Components=main" \
        -o "APT::FTPArchive::Release::Label=Keymasq" \
        -o "APT::FTPArchive::Release::Origin=keymasq.tools" \
        release "$DEB_DIST" > "$DEB_DIST/Release"

    gpg --batch --yes --detach-sign --armor \
        --output "$DEB_DIST/Release.gpg" "$DEB_DIST/Release"
    gpg --batch --yes --clearsign \
        --output "$DEB_DIST/InRelease" "$DEB_DIST/Release"
    echo "APT metadata signed"
fi

# -- Rebuild RPM metadata --

while IFS= read -r -d '' rpm_dir; do
    relative_dir="${rpm_dir#"$REPO_DIR"/}"
    if ! ls "$rpm_dir"/*.rpm &>/dev/null; then
        continue
    fi
    echo "Rebuilding RPM metadata for $relative_dir..."
    createrepo_c --update "$rpm_dir"
    gpg --batch --yes --detach-sign --armor \
        --output "$rpm_dir/repodata/repomd.xml.asc" \
        "$rpm_dir/repodata/repomd.xml"
    echo "RPM metadata signed for $relative_dir"
done < <(find "$FEDORA_ROOT" "$OPENSUSE_DIR" -mindepth 0 -maxdepth 1 -type d -print0)

# -- Export public key --

gpg --batch --armor --export "$FINGERPRINT" > "$REPO_DIR/gpg-key.asc"

# -- Upload to server --

echo "Creating sync tarball..."
tar -czf "$WORK_DIR/sync.tar.gz" -C "$REPO_DIR" .

echo "Uploading to repo server..."
sync_code=$(curl -sS -L --post301 --post302 --post303 \
    -w '%{http_code}' -o "$WORK_DIR/sync-response.json" \
    -X POST "$REPO_UPLOAD_URL/api/sync" \
    -H "Authorization: Bearer $REPO_UPLOAD_TOKEN" \
    -H "Content-Type: application/gzip" \
    --data-binary "@$WORK_DIR/sync.tar.gz")

if [[ "$sync_code" != "200" ]]; then
    echo "Sync failed (HTTP $sync_code):" >&2
    cat "$WORK_DIR/sync-response.json" >&2
    exit 1
fi

cat "$WORK_DIR/sync-response.json"
echo ""
echo "Repository published successfully"
