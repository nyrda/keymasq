#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

aur_repo_url="${AUR_REPO_SSH_URL:-ssh://aur@aur.archlinux.org/keyforge.git}"
aur_commit_name="${AUR_COMMIT_NAME:-github-actions[bot]}"
aur_commit_email="${AUR_COMMIT_EMAIL:-41898282+github-actions[bot]@users.noreply.github.com}"
aur_commit_message="${AUR_COMMIT_MESSAGE:-Update AUR package}"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

git clone "$aur_repo_url" "$workdir/repo"

find "$workdir/repo" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
cp -a "$REPO_DIR/packaging/aur/." "$workdir/repo/"

if [[ -z "$(git -C "$workdir/repo" status --short)" ]]; then
    echo "AUR repo already up to date"
    exit 0
fi

git -C "$workdir/repo" add PKGBUILD keyforge.install .SRCINFO
git -C "$workdir/repo" -c user.name="$aur_commit_name" -c user.email="$aur_commit_email" \
    commit -m "$aur_commit_message"
git -C "$workdir/repo" push origin HEAD
