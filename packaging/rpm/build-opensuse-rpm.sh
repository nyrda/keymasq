#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: build-opensuse-rpm.sh <target-dir>" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
TARGET_DIR="$(mkdir -p "$1" && cd "$1" && pwd)"
STAGING_DIR="${RPM_STAGING_DIR:-$REPO_DIR/staging/rpm-opensuse}"

if [[ ! -d "$STAGING_DIR" ]]; then
    echo "staging tree not found: $STAGING_DIR" >&2
    exit 1
fi

for var_name in \
    RPM_EVDEV_DEP \
    RPM_TOMLI_W_DEP \
    RPM_DBUS_NEXT_DEP \
    RPM_UVLOOP_DEP \
    RPM_XLIB_DEP \
    RPM_PYGOBJECT_DEP \
    RPM_GTK_DEP \
    RPM_ADW_DEP
do
    if [[ -z "${!var_name:-}" ]]; then
        echo "missing required environment variable: $var_name" >&2
        exit 1
    fi
done

source "$SCRIPT_DIR/metadata.env"

topdir="$(mktemp -d "$TARGET_DIR/.rpmbuild.opensuse.XXXXXX")"
trap 'rm -rf "$topdir"' EXIT

mkdir -p "$topdir"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}

tar -C "$STAGING_DIR" -czf "$topdir/SOURCES/keymasq-root.tar.gz" .

(
    cd "$STAGING_DIR"
    find . -mindepth 1 -printf '%y %P\n' | sort | while read -r type path; do
        path="/$path"
        if [[ "$path" == "/etc/keymasq/security.toml" ]]; then
            printf '%%config(noreplace) %s\n' "$path"
        elif [[ "$type" != "d" ]]; then
            printf '%s\n' "$path"
        fi
    done
) > "$topdir/SOURCES/keymasq.files"

{
cat <<SPEC_HEAD
Name: keymasq
Version: $VERSION
Release: $RELEASE
Summary: A key remapping tool for Linux using evdev and uinput
License: MIT
URL: https://keymasq.tools
Vendor: nyrda
Packager: nyrda <nyrda@keymasq.tools>
BuildArch: x86_64
Requires: acl
Requires: python3 >= 3.12
Requires: ${RPM_EVDEV_DEP}
Requires: ${RPM_TOMLI_W_DEP}
Requires: ${RPM_DBUS_NEXT_DEP}
Requires: ${RPM_XLIB_DEP}
Requires: ${RPM_PYGOBJECT_DEP}
Requires: ${RPM_GTK_DEP}
Requires: ${RPM_ADW_DEP}
Requires: polkit
Requires: systemd
Recommends: slurp
Recommends: ${RPM_UVLOOP_DEP}
Source0: keymasq-root.tar.gz
Source1: keymasq.files

%description
A key remapping tool for Linux using evdev and uinput

%prep

%build

%install
mkdir -p %{buildroot}
tar -xzf %{SOURCE0} -C %{buildroot}

%post
SPEC_HEAD
cat "$REPO_DIR/scripts/rpm-postinstall.sh"
cat <<'SPEC_MID'

%preun
SPEC_MID
cat "$REPO_DIR/scripts/rpm-preremove.sh"
cat <<'SPEC_MID'

%postun
SPEC_MID
cat "$REPO_DIR/scripts/rpm-postremove.sh"
cat <<'SPEC_TAIL'

%files -f %{SOURCE1}
SPEC_TAIL
} > "$topdir/SPECS/keymasq.spec"

rpmbuild --define "_topdir $topdir" -bb "$topdir/SPECS/keymasq.spec" >/dev/null

built_rpm="$(find "$topdir/RPMS" -maxdepth 2 -name '*.rpm' -print -quit)"
if [[ -z "$built_rpm" ]]; then
    echo "rpmbuild did not produce an openSUSE RPM" >&2
    exit 1
fi

mv "$built_rpm" "$TARGET_DIR/"
