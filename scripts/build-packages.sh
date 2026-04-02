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

resolve_rpm_target_id() {
    local host="${RPM_BUILD_HOST:-}"

    if [[ -n "$host" ]]; then
        ssh "$host" '. /etc/os-release && printf "%s\n" "$ID"'
        return 0
    fi

    . /etc/os-release
    printf '%s\n' "$ID"
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

configure_rpm_dependencies() {
    local target_id="$1"
    local python_prefix="$2"

    case "$target_id" in
        opensuse*|sles|sle*)
            export RPM_EVDEV_DEP="${python_prefix}-evdev"
            export RPM_TOMLI_W_DEP="${python_prefix}-tomli-w"
            export RPM_DBUS_NEXT_DEP="${python_prefix}-dbus_next"
            export RPM_XLIB_DEP="${python_prefix}-python-xlib"
            export RPM_PYGOBJECT_DEP="${python_prefix}-gobject"
            export RPM_GTK_DEP='typelib(Gtk) = 4.0'
            export RPM_ADW_DEP='typelib(Adw) = 1'
            ;;
        *)
            export RPM_EVDEV_DEP='python3dist(evdev)'
            export RPM_TOMLI_W_DEP='python3dist(tomli-w)'
            export RPM_DBUS_NEXT_DEP='python3dist(dbus-next)'
            export RPM_XLIB_DEP='python3dist(python-xlib)'
            export RPM_PYGOBJECT_DEP='python3dist(pygobject)'
            export RPM_GTK_DEP='gtk4'
            export RPM_ADW_DEP='libadwaita'
            ;;
    esac
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

stage_rpm_tree() {
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

build_rpm_variant() {
    local label="$1"
    local host="${2:-}"
    local staging_dir="$STAGING_ROOT/rpm"
    local target_dir="$DIST/.rpm-$label"
    local target_id=""
    local python_prefix=""
    local rpm_python_sitelib=""
    local built_rpm=""
    local final_rpm=""
    local previous_host="${RPM_BUILD_HOST:-}"

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
    configure_rpm_dependencies "$target_id" "$python_prefix"
    rpm_python_sitelib="$(resolve_rpm_python_sitelib)"
    export RPM_PYTHON_SITELIB="$rpm_python_sitelib"

    stage_rpm_tree "$staging_dir" "$rpm_python_sitelib"

    echo ""
    echo "Building .rpm for $label..."
    echo "RPM target distro: $target_id"
    echo "RPM python dependency prefix: $python_prefix"
    echo "RPM Python site-packages: $rpm_python_sitelib"
    nfpm package --config nfpm.yaml --packager rpm --target "$target_dir/"

    built_rpm="$(find "$target_dir" -maxdepth 1 -name '*.rpm' -print -quit)"
    if [[ -z "$built_rpm" ]]; then
        echo "nfpm did not produce an RPM for $label" >&2
        exit 1
    fi

    final_rpm="$(variant_rpm_name "$built_rpm" "$label")"
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

# Clean previous builds
rm -rf "$STAGING_ROOT" "$DIST"/*.rpm
mkdir -p "$DIST"

# Build the wheel
echo "Building wheel..."
python3 -m build --wheel --no-isolation

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
            add_rpm_target "$local_label" "${RPM_BUILD_HOST:-}"
            ;;
        *)
            echo ""
            echo "Skipping RPM build: configure FEDORA_BUILD_HOST and/or OPENSUSE_BUILD_HOST to build target-specific RPMs."
            ;;
    esac
fi

for i in "${!rpm_labels[@]}"; do
    build_rpm_variant "${rpm_labels[$i]}" "${rpm_hosts[$i]}"
done

echo ""
echo "Packages built:"
ls -lh "$DIST"/*.rpm 2>/dev/null || true
