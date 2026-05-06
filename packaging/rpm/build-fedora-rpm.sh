#!/usr/bin/env bash
set -euo pipefail

BUILD_MODE="${KEYMASQ_RPM_BUILD_MODE:-binary}"

if [[ "${1:-}" == "--srpm" ]]; then
    BUILD_MODE="srpm"
    shift
fi

if [[ $# -ne 2 ]]; then
    echo "usage: build-fedora-rpm.sh [--srpm] <target-dir> <source-tarball>" >&2
    exit 2
fi

case "$BUILD_MODE" in
    binary|srpm) ;;
    *)
        echo "unsupported KEYMASQ_RPM_BUILD_MODE: $BUILD_MODE" >&2
        exit 2
        ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
TARGET_DIR="$(mkdir -p "$1" && cd "$1" && pwd)"
SOURCE_TARBALL="$2"

if ! fedora_release="$(rpm --eval '%{?fedora}')" || [[ -z "$fedora_release" ]]; then
    echo "Fedora RPM builds must run in a Fedora build environment" >&2
    exit 1
fi

if [[ ! -f "$SOURCE_TARBALL" ]]; then
    echo "source tarball not found: $SOURCE_TARBALL" >&2
    exit 1
fi

source "$SCRIPT_DIR/metadata.env"

topdir="$(mktemp -d "$TARGET_DIR/.rpmbuild.fedora.XXXXXX")"
trap 'rm -rf "$topdir"' EXIT

mkdir -p "$topdir"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}

source_stage_dir="$topdir/SOURCES/source-stage"
mkdir -p "$source_stage_dir"
tar -xzf "$SOURCE_TARBALL" -C "$source_stage_dir"
mv "$source_stage_dir/keymasq-${VERSION}" "$source_stage_dir/keymasq-${VERSION}-build"
tar -C "$source_stage_dir" -czf "$topdir/SOURCES/keymasq-${VERSION}.tar.gz" \
    "keymasq-${VERSION}-build"
rm -rf "$source_stage_dir"

{
cat <<SPEC_HEAD
%global debug_package %{nil}
%global buildsubdir %{name}-%{version}-build
%global pyproject_bytecompilation %{nil}
%global __brp_python_bytecompile %{nil}

Name:           keymasq
Version:        $VERSION
Release:        ${RELEASE}%{?dist}
Summary:        Keyboard and mouse remapper with GUI configuration, per-window profiles, and macros

License:        MIT
URL:            https://keymasq.tools
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  desktop-file-utils
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-devel
BuildRequires:  systemd-rpm-macros

Requires:       acl
Requires:       gtk4
Requires:       libadwaita
Requires:       polkit
Requires:       systemd
Recommends:     slurp
Recommends:     python3dist(uvloop)

%generate_buildrequires
%pyproject_buildrequires

%description
Keyboard and mouse remapper with GUI configuration, per-window profiles, and macros

%prep
%autosetup -n %{buildsubdir}

%build
%pyproject_wheel

%install
%pyproject_install
find %{buildroot}%{python3_sitelib} -type d -name __pycache__ -prune -exec rm -rf {} +
find %{buildroot}%{python3_sitelib} -type f -name '*.py[co]' -delete
%pyproject_save_files keymasq
sed -i '/__pycache__/d; /\\.py[co]/d' %{pyproject_files}

install -Dpm0644 systemd/keymasqd.service %{buildroot}%{_unitdir}/keymasqd.service
install -Dpm0644 systemd/keymasq-session.service %{buildroot}%{_userunitdir}/keymasq-session.service
install -Dpm0644 sysusers.d/keymasq.conf %{buildroot}%{_sysusersdir}/keymasq.conf
install -Dpm0644 tmpfiles.d/keymasq.conf %{buildroot}%{_tmpfilesdir}/keymasq.conf
install -Dpm0644 udev/91-keymasq-acl.rules %{buildroot}%{_udevrulesdir}/91-keymasq-acl.rules
install -Dpm0644 polkit/com.keymasq.record-macro.policy %{buildroot}%{_datadir}/polkit-1/actions/com.keymasq.record-macro.policy
install -Dpm0644 assets/tools.keymasq.keymasq.desktop %{buildroot}%{_datadir}/applications/tools.keymasq.keymasq.desktop
install -Dpm0644 assets/tools.keymasq.keymasq.metainfo.xml %{buildroot}%{_datadir}/metainfo/tools.keymasq.keymasq.metainfo.xml
install -Dpm0644 assets/tools.keymasq.keymasq.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/tools.keymasq.keymasq.svg
install -Dpm0644 LICENSE %{buildroot}%{_datadir}/licenses/%{name}/LICENSE
install -Dpm0644 README.md %{buildroot}%{_docdir}/%{name}/README.md
install -Dpm0644 examples/security.toml %{buildroot}%{_sysconfdir}/keymasq/security.toml

mkdir -p %{buildroot}%{_datadir}/gnome-shell/extensions/gnome-bridge@keymasq.tools
cp -a gnome-extension/gnome-bridge@keymasq.tools/. %{buildroot}%{_datadir}/gnome-shell/extensions/gnome-bridge@keymasq.tools/

for size in 16 22 24 32 48 64 128 256 512; do
    install -Dpm0644 assets/icons/tools.keymasq.keymasq-\${size}.png \
        %{buildroot}%{_datadir}/icons/hicolor/\${size}x\${size}/apps/tools.keymasq.keymasq.png
done

desktop-file-install \
    --dir %{buildroot}%{_datadir}/applications \
    assets/tools.keymasq.keymasq.desktop

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

%files -f %{pyproject_files}
%{_bindir}/keymasq
%{_bindir}/keymasqd
%{_bindir}/keymasq-record
%{_bindir}/keymasq-session
%{_unitdir}/keymasqd.service
%{_userunitdir}/keymasq-session.service
%{_sysusersdir}/keymasq.conf
%{_tmpfilesdir}/keymasq.conf
%{_udevrulesdir}/91-keymasq-acl.rules
%{_datadir}/polkit-1/actions/com.keymasq.record-macro.policy
%{_datadir}/applications/tools.keymasq.keymasq.desktop
%{_datadir}/metainfo/tools.keymasq.keymasq.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/tools.keymasq.keymasq.svg
%{_datadir}/icons/hicolor/16x16/apps/tools.keymasq.keymasq.png
%{_datadir}/icons/hicolor/22x22/apps/tools.keymasq.keymasq.png
%{_datadir}/icons/hicolor/24x24/apps/tools.keymasq.keymasq.png
%{_datadir}/icons/hicolor/32x32/apps/tools.keymasq.keymasq.png
%{_datadir}/icons/hicolor/48x48/apps/tools.keymasq.keymasq.png
%{_datadir}/icons/hicolor/64x64/apps/tools.keymasq.keymasq.png
%{_datadir}/icons/hicolor/128x128/apps/tools.keymasq.keymasq.png
%{_datadir}/icons/hicolor/256x256/apps/tools.keymasq.keymasq.png
%{_datadir}/icons/hicolor/512x512/apps/tools.keymasq.keymasq.png
%{_datadir}/gnome-shell/extensions/gnome-bridge@keymasq.tools/
%{_datadir}/licenses/%{name}/LICENSE
%{_docdir}/%{name}/README.md
%config(noreplace) %{_sysconfdir}/keymasq/security.toml

%changelog
* Wed Apr 22 2026 nyrda <nyrda@keymasq.tools> - $VERSION-$RELEASE
- Local Fedora build
SPEC_TAIL
} > "$topdir/SPECS/keymasq.spec"

if [[ "$BUILD_MODE" == "srpm" ]]; then
    rpmbuild --define "_topdir $topdir" -bs "$topdir/SPECS/keymasq.spec" >/dev/null
    built_rpm="$(find "$topdir/SRPMS" -maxdepth 1 -name '*.src.rpm' -print -quit)"
else
    rpmbuild --define "_topdir $topdir" -bb "$topdir/SPECS/keymasq.spec" >/dev/null
    built_rpm="$(find "$topdir/RPMS" -maxdepth 2 -name '*.rpm' -print -quit)"
fi

if [[ -z "$built_rpm" ]]; then
    echo "rpmbuild did not produce a Fedora $BUILD_MODE RPM" >&2
    exit 1
fi

if [[ "$BUILD_MODE" != "srpm" ]] && rpm -qlp "$built_rpm" | grep -Eq '(^|/)(__pycache__|[^/]+\.py[co]$)'; then
    echo "Fedora RPM contains Python bytecode files" >&2
    rpm -qlp "$built_rpm" | grep -E '(^|/)(__pycache__|[^/]+\.py[co]$)' >&2
    exit 1
fi

mv "$built_rpm" "$TARGET_DIR/"
