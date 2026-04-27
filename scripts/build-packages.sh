#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
STAGING_ROOT="$REPO_DIR/staging"
DIST="$REPO_DIR/dist"

cd "$REPO_DIR"

normalize_rpm_label() {
    case "$1" in
        fedora) printf '%s\n' "fedora" ;;
        opensuse*|sles|sle*) printf '%s\n' "opensuse" ;;
        *) printf '%s\n' "$1" ;;
    esac
}

resolve_rpm_target_id() {
    local host="${RPM_BUILD_HOST:-}"

    if [[ -n "$host" ]]; then
        ssh "$host" '. /etc/os-release && printf "%s\n" "$ID"'
        return 0
    fi

    . /etc/os-release
    printf '%s\n' "$ID"
}

resolve_rpm_version_id() {
    local host="${RPM_BUILD_HOST:-}"

    if [[ -n "$host" ]]; then
        ssh "$host" '. /etc/os-release && printf "%s\n" "$VERSION_ID"'
        return 0
    fi

    . /etc/os-release
    printf '%s\n' "$VERSION_ID"
}

resolve_rpm_python_sitelib() {
    local host="${RPM_BUILD_HOST:-}"
    local macro_value=""

    if [[ -n "${RPM_PYTHON_SITELIB:-}" ]]; then
        printf '%s\n' "$RPM_PYTHON_SITELIB"
        return 0
    fi

    if [[ -n "$host" ]]; then
        macro_value="$(ssh "$host" 'rpm --eval "%{python3_sitelib}" 2>/dev/null || true')"
        if [[ -n "$macro_value" && "$macro_value" != '%{python3_sitelib}' ]]; then
            printf '%s\n' "$macro_value"
            return 0
        fi

        ssh "$host" 'python3 - <<'"'"'PY'"'"'
import sysconfig
print(sysconfig.get_path("purelib", scheme="posix_prefix", vars={"base": "/usr", "platbase": "/usr"}))
PY'
        return 0
    fi

    python3 - <<'PY'
import sysconfig
print(sysconfig.get_path("purelib", scheme="posix_prefix", vars={"base": "/usr", "platbase": "/usr"}))
PY
}

resolve_rpm_python_prefix() {
    local host="${RPM_BUILD_HOST:-}"

    if [[ -n "$host" ]]; then
        ssh "$host" 'python3 - <<'"'"'PY'"'"'
import sys
print(f"python{sys.version_info.major}{sys.version_info.minor}")
PY'
        return 0
    fi

    python3 - <<'PY'
import sys
print(f"python{sys.version_info.major}{sys.version_info.minor}")
PY
}

configure_opensuse_dependencies() {
    local python_prefix="$1"

    export RPM_EVDEV_DEP="${python_prefix}-evdev"
    export RPM_TOMLI_W_DEP="${python_prefix}-tomli-w"
    export RPM_DBUS_NEXT_DEP="${python_prefix}-dbus_next"
    export RPM_UVLOOP_DEP="${python_prefix}-uvloop"
    export RPM_XLIB_DEP="${python_prefix}-python-xlib"
    export RPM_PYGOBJECT_DEP="${python_prefix}-gobject"
    export RPM_GTK_DEP='typelib(Gtk) = 4.0'
    export RPM_ADW_DEP='typelib(Adw) = 1'
}

rewrite_python_shebangs() {
    local bin_dir="$1"
    local script=""

    if [[ ! -d "$bin_dir" ]]; then
        return 0
    fi

    for script in "$bin_dir"/*; do
        if head -1 "$script" | grep -q '^#!.*python'; then
            sed -i '1s|^#!.*python[0-9.]*|#!/usr/bin/python3|' "$script"
        fi
    done
}

stage_rpm_support_files() {
    local staging_dir="$1"

    mkdir -p \
        "$staging_dir/etc/keymasq" \
        "$staging_dir/usr/lib/systemd/system" \
        "$staging_dir/usr/lib/systemd/user" \
        "$staging_dir/usr/lib/sysusers.d" \
        "$staging_dir/usr/lib/tmpfiles.d" \
        "$staging_dir/usr/lib/udev/rules.d" \
        "$staging_dir/usr/share/polkit-1/actions" \
        "$staging_dir/usr/share/applications" \
        "$staging_dir/usr/share/metainfo" \
        "$staging_dir/usr/share/icons/hicolor/scalable/apps" \
        "$staging_dir/usr/share/gnome-shell/extensions/gnome-bridge@keymasq.tools" \
        "$staging_dir/usr/share/licenses/keymasq" \
        "$staging_dir/usr/share/doc/keymasq"

    cp -f "$REPO_DIR/systemd/keymasqd.service" \
        "$staging_dir/usr/lib/systemd/system/keymasqd.service"
    cp -f "$REPO_DIR/systemd/keymasq-session.service" \
        "$staging_dir/usr/lib/systemd/user/keymasq-session.service"
    cp -f "$REPO_DIR/sysusers.d/keymasq.conf" \
        "$staging_dir/usr/lib/sysusers.d/keymasq.conf"
    cp -f "$REPO_DIR/tmpfiles.d/keymasq.conf" \
        "$staging_dir/usr/lib/tmpfiles.d/keymasq.conf"
    cp -f "$REPO_DIR/udev/91-keymasq-acl.rules" \
        "$staging_dir/usr/lib/udev/rules.d/91-keymasq-acl.rules"
    cp -f "$REPO_DIR/polkit/com.keymasq.record-macro.policy" \
        "$staging_dir/usr/share/polkit-1/actions/com.keymasq.record-macro.policy"
    cp -f "$REPO_DIR/assets/tools.keymasq.keymasq.desktop" \
        "$staging_dir/usr/share/applications/tools.keymasq.keymasq.desktop"
    cp -f "$REPO_DIR/assets/tools.keymasq.keymasq.metainfo.xml" \
        "$staging_dir/usr/share/metainfo/tools.keymasq.keymasq.metainfo.xml"
    cp -f "$REPO_DIR/assets/tools.keymasq.keymasq.svg" \
        "$staging_dir/usr/share/icons/hicolor/scalable/apps/tools.keymasq.keymasq.svg"
    cp -a "$REPO_DIR/gnome-extension/gnome-bridge@keymasq.tools/." \
        "$staging_dir/usr/share/gnome-shell/extensions/gnome-bridge@keymasq.tools/"
    cp -f "$REPO_DIR/LICENSE" \
        "$staging_dir/usr/share/licenses/keymasq/LICENSE"
    cp -f "$REPO_DIR/README.md" \
        "$staging_dir/usr/share/doc/keymasq/README.md"
    cp -f "$REPO_DIR/examples/security.toml" \
        "$staging_dir/etc/keymasq/security.toml"

    local size=""
    for size in 16 22 24 32 48 64 128 256 512; do
        mkdir -p "$staging_dir/usr/share/icons/hicolor/${size}x${size}/apps"
        cp -f "$REPO_DIR/assets/icons/tools.keymasq.keymasq-${size}.png" \
            "$staging_dir/usr/share/icons/hicolor/${size}x${size}/apps/tools.keymasq.keymasq.png"
    done
}

stage_opensuse_tree() {
    local staging_dir="$1"
    local rpm_python_sitelib="$2"
    local site_pkg_rpm=""

    rm -rf "$staging_dir"
    echo "Installing wheel into RPM staging for $(basename "$staging_dir")..."
    pip install --root "$staging_dir" --prefix=/usr \
        --no-deps --no-compile --no-warn-script-location \
        --ignore-installed dist/*.whl

    site_pkg_rpm="$(find "$staging_dir/usr/lib" -maxdepth 2 -type d -name site-packages)"
    if [[ -n "$site_pkg_rpm" && "$site_pkg_rpm" != "$staging_dir$rpm_python_sitelib" ]]; then
        mkdir -p "$staging_dir$rpm_python_sitelib"
        cp -a "$site_pkg_rpm"/* "$staging_dir$rpm_python_sitelib/"
        rm -rf "$(dirname "$site_pkg_rpm")"
    fi

    rewrite_python_shebangs "$staging_dir/usr/bin"
    stage_rpm_support_files "$staging_dir"
}

copy_remote_rpm_bundle() {
    local host="$1"
    local remote_dir="$2"
    local source_tarball="$3"

    ssh "$host" "mkdir -p '$remote_dir/dist' '$remote_dir/packaging/rpm' '$remote_dir/scripts'"
    tar -C "$REPO_DIR" -cf - \
        packaging/rpm/build-fedora-rpm.sh \
        packaging/rpm/metadata.env \
        scripts/rpm-postinstall.sh \
        scripts/rpm-preremove.sh \
        scripts/rpm-postremove.sh \
        | ssh "$host" "tar -xf - -C '$remote_dir'"
    scp -q "$source_tarball" "$host:$remote_dir/dist/"
}

build_fedora_variant() {
    local host="${1:-}"
    local target_dir="$DIST/.rpm-fedora"
    local previous_host="${RPM_BUILD_HOST:-}"
    local source_tarball="$SOURCE_TARBALL"
    local target_id=""
    local fedora_version=""
    local built_rpm=""

    rm -rf "$target_dir"
    mkdir -p "$target_dir"

    if [[ -n "$host" ]]; then
        export RPM_BUILD_HOST="$host"
    else
        unset RPM_BUILD_HOST || true
    fi

    target_id="$(resolve_rpm_target_id)"
    fedora_version="$(resolve_rpm_version_id)"
    if [[ "$(normalize_rpm_label "$target_id")" != "fedora" ]]; then
        echo "Fedora build target must resolve to Fedora, got: $target_id" >&2
        exit 1
    fi

    echo ""
    echo "Building Fedora RPM..."
    echo "Fedora target release: $fedora_version"

    if [[ -n "$host" ]]; then
        local remote_dir=""
        remote_dir="$(ssh "$host" 'mktemp -d /var/tmp/keymasq-fedora-rpm.XXXXXX')"
        copy_remote_rpm_bundle "$host" "$remote_dir" "$source_tarball"
        ssh "$host" "mkdir -p '$remote_dir/out' && cd '$remote_dir' && bash packaging/rpm/build-fedora-rpm.sh out dist/$(basename "$source_tarball")"
        built_rpm="$(ssh "$host" "find '$remote_dir/out' -maxdepth 1 -name '*.rpm' -print -quit")"
        if [[ -z "$built_rpm" ]]; then
            ssh "$host" "rm -rf '$remote_dir'" >/dev/null 2>&1 || true
            echo "Fedora build did not produce an artifact" >&2
            exit 1
        fi
        scp -q "$host:$built_rpm" "$DIST/"
        ssh "$host" "rm -rf '$remote_dir'"
    else
        bash packaging/rpm/build-fedora-rpm.sh "$target_dir" "$source_tarball"
        built_rpm="$(find "$target_dir" -maxdepth 1 -name '*.rpm' -print -quit)"
        if [[ -z "$built_rpm" ]]; then
            echo "Fedora build did not produce an artifact" >&2
            exit 1
        fi
        mv "$built_rpm" "$DIST/"
        rm -rf "$target_dir"
    fi

    if [[ -n "$previous_host" ]]; then
        export RPM_BUILD_HOST="$previous_host"
    else
        unset RPM_BUILD_HOST || true
    fi
}

variant_rpm_name() {
    local rpm_path="$1"
    local label="$2"
    local rpm_base=""
    local rpm_stem=""
    local rpm_prefix=""
    local rpm_arch=""

    rpm_base="$(basename "$rpm_path")"
    rpm_stem="${rpm_base%.rpm}"
    rpm_prefix="${rpm_stem%.*}"
    rpm_arch="${rpm_stem##*.}"

    printf '%s/%s.%s.%s.rpm\n' "$DIST" "$rpm_prefix" "$label" "$rpm_arch"
}

build_opensuse_variant() {
    local host="${1:-}"
    local target_dir="$DIST/.rpm-opensuse"
    local staging_dir="$STAGING_ROOT/rpm-opensuse"
    local previous_host="${RPM_BUILD_HOST:-}"
    local target_id=""
    local python_prefix=""
    local rpm_python_sitelib=""
    local built_rpm=""
    local final_rpm=""

    rm -rf "$target_dir"
    mkdir -p "$target_dir"

    if [[ -n "$host" ]]; then
        export RPM_BUILD_HOST="$host"
    else
        unset RPM_BUILD_HOST || true
    fi

    unset RPM_PYTHON_SITELIB || true
    target_id="$(resolve_rpm_target_id)"
    python_prefix="$(resolve_rpm_python_prefix)"
    if [[ "$(normalize_rpm_label "$target_id")" != "opensuse" ]]; then
        echo "openSUSE build target must resolve to openSUSE, got: $target_id" >&2
        exit 1
    fi

    configure_opensuse_dependencies "$python_prefix"
    rpm_python_sitelib="$(resolve_rpm_python_sitelib)"
    export RPM_PYTHON_SITELIB="$rpm_python_sitelib"

    stage_opensuse_tree "$staging_dir" "$rpm_python_sitelib"

    echo ""
    echo "Building openSUSE RPM..."
    echo "RPM target distro: $target_id"
    echo "RPM python dependency prefix: $python_prefix"
    echo "RPM Python site-packages: $rpm_python_sitelib"

    RPM_STAGING_DIR="$staging_dir" bash packaging/rpm/build-opensuse-rpm.sh "$target_dir"

    built_rpm="$(find "$target_dir" -maxdepth 1 -name '*.rpm' -print -quit)"
    if [[ -z "$built_rpm" ]]; then
        echo "openSUSE build did not produce an artifact" >&2
        exit 1
    fi

    final_rpm="$(variant_rpm_name "$built_rpm" "opensuse")"
    mv "$built_rpm" "$final_rpm"
    rm -rf "$target_dir"

    if [[ -n "$previous_host" ]]; then
        export RPM_BUILD_HOST="$previous_host"
    else
        unset RPM_BUILD_HOST || true
    fi
}

declare -a rpm_labels=()
declare -a rpm_hosts=()

add_rpm_target() {
    rpm_labels+=("$1")
    rpm_hosts+=("${2:-}")
}

rm -rf "$STAGING_ROOT" "$DIST"/*.rpm
mkdir -p "$DIST"

echo "Building source tarball..."
SOURCE_TARBALL="$(bash scripts/build-source-tarball.sh)"

if [[ -n "${FEDORA_BUILD_HOST:-}" ]]; then
    add_rpm_target "fedora" "$FEDORA_BUILD_HOST"
fi

if [[ -n "${OPENSUSE_BUILD_HOST:-}" ]]; then
    add_rpm_target "opensuse" "$OPENSUSE_BUILD_HOST"
fi

if [[ ${#rpm_labels[@]} -eq 0 ]]; then
    local_target_id="$(resolve_rpm_target_id)"
    local_label="$(normalize_rpm_label "$local_target_id")"

    case "$local_label" in
        fedora|opensuse)
            add_rpm_target "$local_label"
            ;;
        *)
            echo ""
            echo "Skipping RPM build: configure FEDORA_BUILD_HOST and/or OPENSUSE_BUILD_HOST to build target-specific RPMs."
            ;;
    esac
fi

if printf '%s\n' "${rpm_labels[@]}" | grep -qx "opensuse"; then
    echo "Building wheel..."
    python3 -m build --wheel --no-isolation
fi

for i in "${!rpm_labels[@]}"; do
    case "${rpm_labels[$i]}" in
        fedora) build_fedora_variant "${rpm_hosts[$i]}" ;;
        opensuse) build_opensuse_variant "${rpm_hosts[$i]}" ;;
        *)
            echo "unsupported RPM label: ${rpm_labels[$i]}" >&2
            exit 1
            ;;
    esac
done

echo ""
echo "Packages built:"
ls -lh "$DIST"/*.rpm 2>/dev/null || true
