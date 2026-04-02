#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: run-autopkgtest-libvirt.sh [--domain <virsh-domain>] [--image <qcow2>] [--source <path>]
                                  [--user <login-user>] [--password <login-password>]

Run autopkgtest against a local QEMU/KVM image. If --domain is provided, the
script resolves the primary disk from libvirt with virsh and uses a temporary
qcow2 overlay so the base image is left untouched.
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
SOURCE_DIR="$REPO_DIR"
DOMAIN=""
IMAGE=""
OVERLAY=""
LOGIN_USER=""
LOGIN_PASSWORD=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --domain)
            DOMAIN="${2:-}"
            shift 2
            ;;
        --image)
            IMAGE="${2:-}"
            shift 2
            ;;
        --source)
            SOURCE_DIR="${2:-}"
            shift 2
            ;;
        --user)
            LOGIN_USER="${2:-}"
            shift 2
            ;;
        --password)
            LOGIN_PASSWORD="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ -n "$DOMAIN" && -n "$IMAGE" ]]; then
    echo "use either --domain or --image, not both" >&2
    exit 2
fi

if ! command -v autopkgtest >/dev/null 2>&1; then
    echo "autopkgtest is required" >&2
    exit 1
fi

if [[ -n "$DOMAIN" ]]; then
    if ! command -v virsh >/dev/null 2>&1; then
        echo "virsh is required for --domain" >&2
        exit 1
    fi
    if ! command -v qemu-img >/dev/null 2>&1; then
        echo "qemu-img is required for --domain" >&2
        exit 1
    fi

    IMAGE="$(
        virsh -c qemu:///system domblklist "$DOMAIN" --details |
            awk '$2 == "disk" && $4 ~ /^\// { print $4; exit }'
    )"
    if [[ -z "$IMAGE" ]]; then
        echo "failed to resolve a disk image from domain: $DOMAIN" >&2
        exit 1
    fi
fi

if [[ -z "$IMAGE" ]]; then
    echo "an image is required; use --image or --domain" >&2
    exit 2
fi

if [[ ! -e "$IMAGE" ]]; then
    echo "image or block device does not exist: $IMAGE" >&2
    exit 1
fi

OVERLAY="$(mktemp --suffix=.qcow2)"
trap 'rm -f "$OVERLAY"' EXIT

BACKING_FORMAT="$(qemu-img info "$IMAGE" | awk -F': ' '/file format:/ { print $2; exit }')"
if [[ -z "$BACKING_FORMAT" ]]; then
    echo "failed to determine image format: $IMAGE" >&2
    exit 1
fi

qemu-img create -f qcow2 -F "$BACKING_FORMAT" -b "$IMAGE" "$OVERLAY" >/dev/null

autopkgtest_args=("$SOURCE_DIR" -- qemu)

if [[ -n "$LOGIN_USER" ]]; then
    autopkgtest_args+=(-u "$LOGIN_USER")
fi

if [[ -n "$LOGIN_PASSWORD" ]]; then
    autopkgtest_args+=(-p "$LOGIN_PASSWORD")
fi

autopkgtest_args+=("$OVERLAY")

exec autopkgtest "${autopkgtest_args[@]}"
