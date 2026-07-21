#!/bin/sh
set -eu

APP_NAME=Keymasq
INSTALL_DIR=/opt/keymasq
APPIMAGE_NAME=Keymasq.AppImage
RUNTIME_DIR_NAME=runtime
CURRENT_RUNTIME_NAME=current
VERSION_FILE_NAME=version
UPDATE_MANIFEST_NAME=latest-x86_64.json
DEFAULT_UPDATE_BASE_URL=https://repo.keymasq.tools/appimage
USER_WRAPPER_MARKER='# Managed by Keymasq AppImage'

log() {
	printf '%s\n' "$*"
}

warn() {
	printf 'warning: %s\n' "$*" >&2
}

die() {
	printf 'error: %s\n' "$*" >&2
	exit 1
}

is_root() {
	[ "$(id -u)" = 0 ]
}

root_path() {
	if [ -n "${KEYMASQ_APPIMAGE_ROOT:-}" ]; then
		printf '%s%s\n' "$KEYMASQ_APPIMAGE_ROOT" "$1"
	else
		printf '%s\n' "$1"
	fi
}

require_root_or_pkexec() {
	if is_root || [ "${KEYMASQ_APPIMAGE_SKIP_PRIVILEGE_CHECK:-0}" = 1 ]; then
		return 0
	fi

	appimage=${APPIMAGE:-}
	if [ -z "$appimage" ]; then
		installed_appimage=$(root_path "$INSTALL_DIR/$APPIMAGE_NAME")
		if [ -x "$installed_appimage" ]; then
			appimage=$installed_appimage
		fi
	fi
	[ -n "$appimage" ] || die "cannot locate AppImage for privilege escalation"

	exec pkexec "$appimage" "$@"
}

asset_dir() {
	if [ -n "${KEYMASQ_APPIMAGE_ASSET_DIR:-}" ]; then
		printf '%s\n' "$KEYMASQ_APPIMAGE_ASSET_DIR"
		return 0
	fi
	if [ -n "${APPDIR:-}" ] && [ -d "$APPDIR/share/keymasq/appimage" ]; then
		printf '%s\n' "$APPDIR/share/keymasq/appimage"
		return 0
	fi
	die "AppImage assets are unavailable"
}

resolve_user_home() {
	user=$1
	home=$(getent passwd "$user" | awk -F: '{print $6; exit}')
	[ -n "$home" ] || die "could not resolve home directory for user $user"
	printf '%s\n' "$home"
}

resolve_target_user() {
	explicit_user=$1
	if [ -n "$explicit_user" ]; then
		printf '%s\n' "$explicit_user"
		return 0
	fi
	if [ -n "${PKEXEC_UID:-}" ]; then
		user=$(getent passwd "$PKEXEC_UID" | awk -F: '{print $1; exit}')
		if [ -n "$user" ]; then
			printf '%s\n' "$user"
			return 0
		fi
	fi
	if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
		printf '%s\n' "$SUDO_USER"
		return 0
	fi
	user=$(id -un 2>/dev/null || true)
	if [ -n "$user" ]; then
		printf '%s\n' "$user"
		return 0
	fi
	die "could not determine target desktop user; pass --user USER"
}

install_file() {
	mode=$1
	src=$2
	dst=$3
	if [ -L "$dst" ]; then
		die "refusing to install through symlinked destination: $dst"
	fi
	install -Dm "$mode" "$src" "$dst"
}

try_install_file() {
	mode=$1
	src=$2
	dst=$3
	if [ -L "$dst" ]; then
		warn "refusing to install through symlinked destination: $dst"
		return 1
	fi
	if install -Dm "$mode" "$src" "$dst"; then
		return 0
	fi
	warn "could not install $dst"
	return 1
}

write_file_atomic() {
	mode=$1
	dst=$2
	dir=$(dirname "$dst")
	base=$(basename "$dst")
	install -d -m 0755 "$dir"
	tmp=$(mktemp "$dir/.${base}.tmp.XXXXXX") || die "failed to create temporary file for $dst"
	if cat >"$tmp"; then
		chmod "$mode" "$tmp"
		mv -Tf "$tmp" "$dst"
	else
		rm -f "$tmp"
		die "failed to write $dst"
	fi
}

write_wrapper() {
	name=$1
	dst=$2
	write_file_atomic 0755 "$dst" <<EOF
#!/bin/sh
APPDIR=\${KEYMASQ_APPDIR:-$INSTALL_DIR/$RUNTIME_DIR_NAME/$CURRENT_RUNTIME_NAME}
if [ ! -x "\$APPDIR/bin/$name" ]; then
	echo "error: Keymasq runtime is not installed at \$APPDIR" >&2
	exit 1
fi
export APPDIR
exec "\$APPDIR/bin/$name" "\$@"
EOF
}

run_as_user() {
	keymasq_run_user=$1
	shift
	if [ "$keymasq_run_user" = root ]; then
		"$@"
	elif command -v runuser >/dev/null 2>&1; then
		runuser -u "$keymasq_run_user" -- "$@"
	elif command -v sudo >/dev/null 2>&1; then
		sudo -u "$keymasq_run_user" -- "$@"
	else
		die "runuser or sudo is required to install files for $keymasq_run_user"
	fi
}

write_user_file_atomic() (
	keymasq_write_user=$1
	keymasq_write_mode=$2
	keymasq_write_dst=$3
	keymasq_write_stage=$(mktemp -d /tmp/keymasq-user-write.XXXXXX) || \
		die "failed to create temporary user-file staging directory"
	trap 'rm -rf "$keymasq_write_stage"' EXIT HUP INT TERM
	keymasq_write_source="$keymasq_write_stage/content"
	cat > "$keymasq_write_source" || die "failed to stage $keymasq_write_dst"
	chmod 0644 "$keymasq_write_source"
	chmod 0755 "$keymasq_write_stage"
	if ! run_as_user "$keymasq_write_user" sh -c '
		set -eu
		mode=$1
		dst=$2
		src=$3
		dir=$(dirname "$dst")
		base=$(basename "$dst")
		tmp=$(mktemp "$dir/.${base}.tmp.XXXXXX")
		trap '\''rm -f "$tmp"'\'' EXIT HUP INT TERM
		cat "$src" >"$tmp"
		chmod "$mode" "$tmp"
		mv -Tf "$tmp" "$dst"
		trap - EXIT HUP INT TERM
	' sh "$keymasq_write_mode" "$keymasq_write_dst" "$keymasq_write_source"; then
		die "failed to write $keymasq_write_dst as $keymasq_write_user"
	fi
)

write_user_wrapper() {
	keymasq_wrapper_user=$1
	keymasq_wrapper_name=$2
	keymasq_wrapper_dst=$3
	write_user_file_atomic "$keymasq_wrapper_user" 0755 "$keymasq_wrapper_dst" <<EOF
#!/bin/sh
$USER_WRAPPER_MARKER
APPDIR=\${KEYMASQ_APPDIR:-$INSTALL_DIR/$RUNTIME_DIR_NAME/$CURRENT_RUNTIME_NAME}
if [ ! -x "\$APPDIR/bin/$keymasq_wrapper_name" ]; then
	echo "error: Keymasq runtime is not installed at \$APPDIR" >&2
	exit 1
fi
export APPDIR
exec "\$APPDIR/bin/$keymasq_wrapper_name" "\$@"
EOF
}

user_wrapper_is_managed() {
	keymasq_wrapper_user=$1
	keymasq_wrapper_name=$2
	keymasq_wrapper_path=$3
	keymasq_wrapper_appdir="APPDIR=\${KEYMASQ_APPDIR:-$INSTALL_DIR/$RUNTIME_DIR_NAME/$CURRENT_RUNTIME_NAME}"
	keymasq_wrapper_exec="exec \"\$APPDIR/bin/$keymasq_wrapper_name\" \"\$@\""
	run_as_user "$keymasq_wrapper_user" sh -c '
		path=$1
		marker=$2
		expected_appdir=$3
		expected_exec=$4
		[ -f "$path" ] && [ ! -L "$path" ] || exit 1
		grep -Fqx "$marker" "$path" && exit 0
		grep -Fqx "$expected_appdir" "$path" && grep -Fqx "$expected_exec" "$path"
	' sh "$keymasq_wrapper_path" "$USER_WRAPPER_MARKER" \
		"$keymasq_wrapper_appdir" "$keymasq_wrapper_exec"
}

validate_user_wrapper_destinations() {
	keymasq_wrapper_user=$1
	keymasq_wrapper_home=$(resolve_user_home "$keymasq_wrapper_user")
	keymasq_wrapper_dir=$(root_path "$keymasq_wrapper_home/.local/bin")
	for keymasq_wrapper_name in keymasq keymasqd keymasq-session keymasq-record waypipe gtk4-brotway-run; do
		keymasq_wrapper_path="$keymasq_wrapper_dir/$keymasq_wrapper_name"
		if { [ -e "$keymasq_wrapper_path" ] || [ -L "$keymasq_wrapper_path" ]; } && \
			! user_wrapper_is_managed "$keymasq_wrapper_user" "$keymasq_wrapper_name" "$keymasq_wrapper_path"; then
			die "refusing to replace non-Keymasq user command: $keymasq_wrapper_path"
		fi
	done
}

runtime_root_path() {
	root_path "$INSTALL_DIR/$RUNTIME_DIR_NAME"
}

runtime_current_path() {
	root_path "$INSTALL_DIR/$RUNTIME_DIR_NAME/$CURRENT_RUNTIME_NAME"
}

version_file_path() {
	root_path "$INSTALL_DIR/$VERSION_FILE_NAME"
}

runtime_path_for_sha256() {
	root_path "$INSTALL_DIR/$RUNTIME_DIR_NAME/$1"
}

current_runtime_id() {
	keymasq_current_path=$(runtime_current_path)
	if [ -L "$keymasq_current_path" ]; then
		readlink "$keymasq_current_path"
	fi
}

validate_runtime_dir() {
	keymasq_validate_dir=$1
	[ -x "$keymasq_validate_dir/bin/keymasq" ] || die "extracted runtime missing keymasq launcher"
	[ -x "$keymasq_validate_dir/bin/keymasqd" ] || die "extracted runtime missing keymasqd launcher"
	[ -x "$keymasq_validate_dir/bin/keymasq-session" ] || die "extracted runtime missing keymasq-session launcher"
	[ -x "$keymasq_validate_dir/bin/keymasq-record" ] || die "extracted runtime missing keymasq-record launcher"
	[ -x "$keymasq_validate_dir/bin/slurp" ] || die "extracted runtime missing bundled slurp launcher"
	[ -x "$keymasq_validate_dir/bin/gtk4-brotway-run" ] || die "extracted runtime missing Brotway launcher"
	[ -x "$keymasq_validate_dir/lib/gtk4-brotway/gtk4-broadwayd" ] || die "extracted runtime missing Brotway daemon"
	[ -e "$keymasq_validate_dir/lib/gtk4-brotway/libgtk-4.so.1" ] || die "extracted runtime missing Brotway GTK library"
}

copy_extracted_runtime() {
	keymasq_copy_source_dir=$1
	keymasq_copy_dst=$2
	[ -d "$keymasq_copy_source_dir" ] || die "extracted AppImage runtime does not exist: $keymasq_copy_source_dir"
	cp -a "$keymasq_copy_source_dir/." "$keymasq_copy_dst/"
}

extract_appimage_runtime_to() {
	keymasq_extract_appimage=$1
	keymasq_extract_dst=$2
	if [ -n "${KEYMASQ_APPIMAGE_EXTRACTED_SOURCE_DIR:-}" ]; then
		copy_extracted_runtime "$KEYMASQ_APPIMAGE_EXTRACTED_SOURCE_DIR" "$keymasq_extract_dst"
		return 0
	fi

	keymasq_tmp_extract=$(mktemp -d)
	if ! (
		cd "$keymasq_tmp_extract"
		"$keymasq_extract_appimage" --appimage-extract >/dev/null
	); then
		rm -rf "$keymasq_tmp_extract"
		die "failed to extract AppImage runtime"
	fi

	keymasq_extracted_dir=
	for keymasq_candidate in "$keymasq_tmp_extract/AppDir" "$keymasq_tmp_extract/squashfs-root"; do
		if [ -d "$keymasq_candidate" ]; then
			keymasq_extracted_dir=$keymasq_candidate
			break
		fi
	done
	if [ -z "$keymasq_extracted_dir" ]; then
		rm -rf "$keymasq_tmp_extract"
		die "AppImage extraction did not produce an AppDir"
	fi

	copy_extracted_runtime "$keymasq_extracted_dir" "$keymasq_extract_dst"
	rm -rf "$keymasq_tmp_extract"
}

prepare_runtime_from_appimage() {
	keymasq_prepare_appimage=$1
	keymasq_prepare_sha256=$2
	keymasq_prepare_runtime_root=$(runtime_root_path)
	keymasq_prepare_runtime_dir=$(runtime_path_for_sha256 "$keymasq_prepare_sha256")
	if [ -d "$keymasq_prepare_runtime_dir" ]; then
		validate_runtime_dir "$keymasq_prepare_runtime_dir"
		return 0
	fi

	install -d -m 0755 "$keymasq_prepare_runtime_root"
	keymasq_prepare_staging="$keymasq_prepare_runtime_root/.extract-$keymasq_prepare_sha256.$$"
	rm -rf "$keymasq_prepare_staging"
	install -d -m 0755 "$keymasq_prepare_staging"
	if ! extract_appimage_runtime_to "$keymasq_prepare_appimage" "$keymasq_prepare_staging"; then
		rm -rf "$keymasq_prepare_staging"
		die "failed to prepare extracted runtime"
	fi
	validate_runtime_dir "$keymasq_prepare_staging"
	if ! mv "$keymasq_prepare_staging" "$keymasq_prepare_runtime_dir"; then
		rm -rf "$keymasq_prepare_staging"
		if [ -d "$keymasq_prepare_runtime_dir" ]; then
			validate_runtime_dir "$keymasq_prepare_runtime_dir"
		else
			die "failed to install extracted runtime"
		fi
	fi
}

activate_runtime() {
	keymasq_activate_sha256=$1
	keymasq_activate_runtime_root=$(runtime_root_path)
	keymasq_activate_current=$(runtime_current_path)
	install -d -m 0755 "$keymasq_activate_runtime_root"
	keymasq_activate_link="$keymasq_activate_runtime_root/.current.$$"
	rm -f "$keymasq_activate_link"
	ln -s "$keymasq_activate_sha256" "$keymasq_activate_link"
	mv -Tf "$keymasq_activate_link" "$keymasq_activate_current"
}

prune_old_runtimes() {
	keymasq_prune_keep_current=$1
	keymasq_prune_keep_previous=${2:-}
	keymasq_prune_runtime_root=$(runtime_root_path)
	[ -d "$keymasq_prune_runtime_root" ] || return 0
	for keymasq_prune_runtime_dir in "$keymasq_prune_runtime_root"/*; do
		[ -e "$keymasq_prune_runtime_dir" ] || [ -L "$keymasq_prune_runtime_dir" ] || continue
		keymasq_prune_name=${keymasq_prune_runtime_dir##*/}
		[ "$keymasq_prune_name" = "$CURRENT_RUNTIME_NAME" ] && continue
		[ "$keymasq_prune_name" = "$keymasq_prune_keep_current" ] && continue
		[ -n "$keymasq_prune_keep_previous" ] && [ "$keymasq_prune_name" = "$keymasq_prune_keep_previous" ] && continue
		case "$keymasq_prune_name" in
			.*) continue ;;
		esac
		if [ -d "$keymasq_prune_runtime_dir" ]; then
			rm -rf "$keymasq_prune_runtime_dir"
		fi
	done
}

appdir_version() {
	keymasq_version_appdir=$1
	[ -d "$keymasq_version_appdir" ] || return 1
	(
		APPDIR=$keymasq_version_appdir
		export APPDIR
		run_python - <<'PY'
from importlib.metadata import PackageNotFoundError, version

try:
    print(version("python-keymasq"))
except PackageNotFoundError:
    raise SystemExit(1)
PY
	)
}

installed_version() {
	if [ -n "${KEYMASQ_APPIMAGE_CURRENT_VERSION:-}" ]; then
		printf '%s\n' "$KEYMASQ_APPIMAGE_CURRENT_VERSION"
		return 0
	fi

	keymasq_version_file=$(version_file_path)
	if [ -s "$keymasq_version_file" ]; then
		sed -n '1p' "$keymasq_version_file"
		return 0
	fi

	if [ -n "${APPDIR:-}" ] && appdir_version "$APPDIR"; then
		return 0
	fi

	keymasq_current_runtime=$(runtime_current_path)
	if [ -d "$keymasq_current_runtime" ] && appdir_version "$keymasq_current_runtime"; then
		return 0
	fi

	return 1
}

write_installed_version() {
	keymasq_write_version=$1
	[ -n "$keymasq_write_version" ] || return 0
	keymasq_write_dst=$(version_file_path)
	install -d -m 0755 "$(dirname "$keymasq_write_dst")"
	keymasq_write_tmp="$keymasq_write_dst.$$"
	printf '%s\n' "$keymasq_write_version" > "$keymasq_write_tmp"
	chmod 0644 "$keymasq_write_tmp"
	mv -f "$keymasq_write_tmp" "$keymasq_write_dst"
}

compare_versions() {
	keymasq_compare_current=$1
	keymasq_compare_candidate=$2
	run_python - "$keymasq_compare_current" "$keymasq_compare_candidate" <<'PY'
from __future__ import annotations

from itertools import zip_longest
import re
import sys

current = sys.argv[1]
candidate = sys.argv[2]

try:
    from packaging.version import Version

    current_version = Version(current)
    candidate_version = Version(candidate)
    print((candidate_version > current_version) - (candidate_version < current_version))
    raise SystemExit(0)
except ImportError:
    pass
except Exception as exc:
    raise SystemExit(f"invalid update version: {exc}") from exc

VERSION_RE = re.compile(r"^(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+)|\.dev(\d+))?$")
STAGE_RANK = {"dev": 0, "a": 1, "b": 2, "rc": 3, "final": 4}


def parse(value: str) -> tuple[tuple[int, ...], int, int]:
    match = VERSION_RE.fullmatch(value)
    if match is None:
        raise SystemExit(f"cannot compare update version without python-packaging: {value}")
    release_raw, prerelease_stage, prerelease_number, dev_number = match.groups()
    release = tuple(int(part) for part in release_raw.split("."))
    if dev_number is not None:
        return release, STAGE_RANK["dev"], int(dev_number)
    if prerelease_stage is not None and prerelease_number is not None:
        return release, STAGE_RANK[prerelease_stage], int(prerelease_number)
    return release, STAGE_RANK["final"], 0


def compare_release(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    for left_part, right_part in zip_longest(left, right, fillvalue=0):
        if left_part < right_part:
            return -1
        if left_part > right_part:
            return 1
    return 0


current_release, current_stage, current_stage_number = parse(current)
candidate_release, candidate_stage, candidate_stage_number = parse(candidate)
release_comparison = compare_release(candidate_release, current_release)
if release_comparison != 0:
    print(release_comparison)
elif candidate_stage != current_stage:
    print((candidate_stage > current_stage) - (candidate_stage < current_stage))
else:
    print((candidate_stage_number > current_stage_number) - (candidate_stage_number < current_stage_number))
PY
}

refuse_downgrade_unless_allowed() {
	keymasq_current_version=$1
	keymasq_candidate_version=$2
	keymasq_allow_downgrade=$3
	keymasq_version_comparison=$(compare_versions "$keymasq_current_version" "$keymasq_candidate_version") \
		|| die "could not compare installed version $keymasq_current_version with update version $keymasq_candidate_version"
	if [ "$keymasq_version_comparison" = "-1" ]; then
		if [ "$keymasq_allow_downgrade" = 1 ]; then
			warn "downgrading from $keymasq_current_version to $keymasq_candidate_version"
			return 0
		fi
		die "refusing to downgrade from $keymasq_current_version to $keymasq_candidate_version; rerun with --allow-downgrade only for an intentional rollback"
	fi
}

install_path_profile() {
	dst=$(root_path /etc/profile.d/keymasq.sh)
	write_file_atomic 0644 "$dst" <<'EOF'
case ":$PATH:" in
	*:/opt/keymasq/bin:*) ;;
	*) PATH="/opt/keymasq/bin:$PATH" ;;
esac
export PATH
EOF
}

install_atomic_keep_list() {
	dst=$(root_path /etc/atomic-update.conf.d/keymasq.conf)
	install -d -m 0755 "$(dirname "$dst")"
	# /opt/keymasq is on the SteamOS offload area (/home/.steamos/offload/opt,
	# the home/var partition), not the read-only A/B rootfs, so it persists
	# across atomic updates on its own. Third-party /etc files need a keep entry,
	# since /etc is governed by the atomic-update allow list.
	write_file_atomic 0644 "$dst" <<'EOF'
/etc/atomic-update.conf.d/keymasq.conf
/etc/keymasq/**
/etc/polkit-1/rules.d/50-keymasq-record.rules
/etc/profile.d/keymasq.sh
/etc/sysusers.d/keymasq.conf
/etc/tmpfiles.d/keymasq.conf
/etc/systemd/system/keymasqd.service
/etc/udev/rules.d/91-keymasq-acl.rules
/etc/udev/rules.d/99-keymasq-hide-grabbed.rules
EOF
}

run_user_systemctl() {
	user=$1
	shift
	uid=$(id -u "$user" 2>/dev/null || getent passwd "$user" | awk -F: '{print $3; exit}')
	[ -n "$uid" ] || return 1
	runtime_dir=/run/user/$uid
	bus_address=unix:path=$runtime_dir/bus
	if command -v runuser >/dev/null 2>&1; then
		runuser -u "$user" -- env XDG_RUNTIME_DIR="$runtime_dir" \
			DBUS_SESSION_BUS_ADDRESS="$bus_address" systemctl --user "$@"
	else
		sudo -u "$user" env XDG_RUNTIME_DIR="$runtime_dir" \
			DBUS_SESSION_BUS_ADDRESS="$bus_address" systemctl --user "$@"
	fi
}

install_user_dir_chain() {
	user=$1
	home=$2
	shift 2
	current=$(root_path "$home")
	ensure_user_home_dir "$current"
	for component in "$@"; do
		current="$current/$component"
		ensure_user_dir "$user" "$current"
	done
}

ensure_user_home_dir() {
	dir=$1
	if [ -L "$dir" ]; then
		die "refusing to write through symlinked user home: $dir"
	fi
	if [ -e "$dir" ]; then
		[ -d "$dir" ] || die "expected user home directory but found non-directory: $dir"
		return 0
	fi
	if [ -z "${KEYMASQ_APPIMAGE_ROOT:-}" ]; then
		die "target user home directory does not exist: $dir"
	fi
	parent=$(dirname "$dir")
	[ -d "$parent" ] || install -d -m 0755 "$parent"
	mkdir "$dir"
	chmod 0755 "$dir"
}

ensure_user_dir() {
	user=$1
	dir=$2
	if [ -L "$dir" ]; then
		die "refusing to write through symlinked user directory: $dir"
	fi
	if [ -e "$dir" ]; then
		[ -d "$dir" ] || die "expected user directory but found non-directory: $dir"
	else
		parent=$(dirname "$dir")
		[ -d "$parent" ] || die "parent user directory does not exist: $parent"
		if ! run_as_user "$user" mkdir "$dir"; then
			die "failed to create $dir as $user"
		fi
		if ! run_as_user "$user" chmod 0755 "$dir"; then
			die "failed to set $dir permissions as $user"
		fi
	fi
}

install_desktop_files() {
	user=$1
	home=$(resolve_user_home "$user")
	assets=$(asset_dir)
	install_user_dir_chain "$user" "$home" .local share applications
	install_user_dir_chain "$user" "$home" .local share icons hicolor scalable apps
	desktop_dir=$(root_path "$home/.local/share/applications")
	icon_dir=$(root_path "$home/.local/share/icons/hicolor/scalable/apps")
	desktop_file="$desktop_dir/tools.keymasq.keymasq.desktop"
	write_user_file_atomic "$user" 0644 "$desktop_file" < "$assets/keymasq.desktop"
	if ! run_as_user "$user" sed -i 's|^Exec=.*$|Exec=/opt/keymasq/bin/keymasq|' "$desktop_file"; then
		die "failed to update desktop launcher as $user"
	fi
	if [ -f "$assets/tools.keymasq.keymasq.svg" ]; then
		icon_file="$icon_dir/tools.keymasq.keymasq.svg"
		write_user_file_atomic "$user" 0644 "$icon_file" < "$assets/tools.keymasq.keymasq.svg"
	fi
}

install_user_service() {
	user=$1
	home=$(resolve_user_home "$user")
	assets=$(asset_dir)
	install_user_dir_chain "$user" "$home" .config systemd user
	unit_dir=$(root_path "$home/.config/systemd/user")
	write_user_file_atomic "$user" 0644 "$unit_dir/keymasq-session.service" < "$assets/keymasq-session.service"
}

install_user_wrappers() {
	user=$1
	home=$(resolve_user_home "$user")
	validate_user_wrapper_destinations "$user"
	install_user_dir_chain "$user" "$home" .local bin
	bin_dir=$(root_path "$home/.local/bin")
	for name in keymasq keymasqd keymasq-session keymasq-record waypipe gtk4-brotway-run; do
		write_user_wrapper "$user" "$name" "$bin_dir/$name"
	done
}

install_session_autostart() {
	user=$1
	home=$(resolve_user_home "$user")
	install_user_dir_chain "$user" "$home" .config autostart
	autostart_dir=$(root_path "$home/.config/autostart")
	autostart_file="$autostart_dir/tools.keymasq.keymasq-session.desktop"
	write_user_file_atomic "$user" 0644 "$autostart_file" <<'EOF'
[Desktop Entry]
Type=Application
Name=Keymasq Session
Comment=Start the Keymasq per-user session manager
Exec=env KEYMASQ_SESSION_RESTART_ON_DAEMON_DISCONNECT=1 /opt/keymasq/bin/keymasq-session
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
EOF
}

reload_udev_rules() {
	if ! command -v udevadm >/dev/null 2>&1; then
		warn "udevadm was not found; reload udev rules manually before using Keymasq"
		return 0
	fi
	if ! udevadm control --reload-rules; then
		warn "could not reload udev rules; device permissions may not update until udev is reloaded"
	fi
	udevadm trigger --subsystem-match=input --action=add || true
	udevadm trigger --subsystem-match=misc --action=add || true
}

clear_keymasq_udev_state() {
	keymasq_udev_failed=0
	remove_path "$(root_path /run/keymasq/hidden)"
	remove_path "$(root_path /run/keymasq/hidden-hardware)"

	if ! command -v udevadm >/dev/null 2>&1; then
		warn "udevadm was not found; existing input device state could not be restored"
		keymasq_udev_failed=1
	else
		if ! udevadm control --reload-rules; then
			warn "could not reload udev rules during uninstall"
			keymasq_udev_failed=1
		fi
		if ! udevadm trigger --action=change --subsystem-match=input; then
			warn "could not retrigger input devices during uninstall"
			keymasq_udev_failed=1
		fi
		if ! udevadm trigger --action=change --sysname-match=uinput; then
			warn "could not retrigger uinput during uninstall"
			keymasq_udev_failed=1
		fi
		if ! udevadm settle; then
			warn "udev did not settle during uninstall"
			keymasq_udev_failed=1
		fi
	fi

	if ! command -v setfacl >/dev/null 2>&1; then
		warn "setfacl was not found; Keymasq device ACLs could not be removed"
		keymasq_udev_failed=1
	else
		for keymasq_device in \
			"$(root_path /dev/uinput)" \
			"$(root_path /dev/input)"/event* \
			"$(root_path /dev/input)"/js*; do
			[ -e "$keymasq_device" ] || continue
			if ! setfacl -x u:keymasq "$keymasq_device" 2>/dev/null; then
				warn "could not remove the Keymasq ACL from $keymasq_device"
				keymasq_udev_failed=1
			fi
		done
	fi

	[ "$keymasq_udev_failed" = 0 ]
}

systemd_available() {
	case "${KEYMASQ_APPIMAGE_SERVICE_MANAGER:-auto}" in
		systemd)
			return 0
			;;
		generic|none|manual)
			return 1
			;;
		auto|"")
			;;
		*)
			warn "unknown KEYMASQ_APPIMAGE_SERVICE_MANAGER value: $KEYMASQ_APPIMAGE_SERVICE_MANAGER"
			;;
	esac

	command -v systemctl >/dev/null 2>&1 || return 1
	[ -d /run/systemd/system ] || [ -d "$(root_path /run/systemd/system)" ] || return 1
}

steamos_detected() {
	os_release=$(root_path /etc/os-release)
	[ -f "$os_release" ] || return 1
	grep -qiE '^(ID|ID_LIKE)=.*steamos' "$os_release"
}

resolve_nologin_shell() {
	for candidate in /usr/sbin/nologin /sbin/nologin /usr/bin/nologin; do
		if [ -x "$candidate" ]; then
			printf '%s\n' "$candidate"
			return 0
		fi
	done
	die "nologin shell was not found"
}

ensure_keymasq_user_and_dirs() {
	if [ -z "${KEYMASQ_APPIMAGE_ROOT:-}" ]; then
		if ! getent group keymasq >/dev/null 2>&1; then
			command -v groupadd >/dev/null 2>&1 || die "groupadd is required to create the keymasq group"
			groupadd --system keymasq
		fi

		if ! getent passwd keymasq >/dev/null 2>&1; then
			command -v useradd >/dev/null 2>&1 || die "useradd is required to create the keymasq user"
			nologin_shell=$(resolve_nologin_shell)
			if getent group input >/dev/null 2>&1; then
				useradd --system --gid keymasq --groups input --home-dir /var/lib/keymasq \
					--shell "$nologin_shell" --comment "Keymasq daemon user" keymasq
			else
				useradd --system --gid keymasq --home-dir /var/lib/keymasq \
					--shell "$nologin_shell" --comment "Keymasq daemon user" keymasq
				warn "input group was not found; relying on udev ACL rules for device access"
			fi
		elif getent group input >/dev/null 2>&1; then
			usermod -a -G input keymasq 2>/dev/null || true
		fi
	fi

	install -d -m 0755 "$(root_path /run/keymasq)"
	install -d -m 0750 "$(root_path /var/lib/keymasq)"
	chown keymasq:keymasq "$(root_path /run/keymasq)" "$(root_path /var/lib/keymasq)" 2>/dev/null || true
}

write_generic_service_instructions() {
	target_user=$1
	install_root=$(root_path "$INSTALL_DIR")
	instructions="$install_root/share/keymasq/non-systemd-services.txt"
	write_file_atomic 0644 "$instructions" <<EOF
Keymasq was installed without systemd service activation.

Install a service for the privileged daemon with these requirements:

  command: /opt/keymasq/bin/keymasqd
  user: keymasq
  group: keymasq
  supplementary group: input, when available
  restart policy: restart on failure
  ordering: start after udev/devfs has created /dev/uinput and /dev/input/event*

Before starting the daemon, grant device ACLs:

  install -d -o keymasq -g keymasq -m 0755 /run/keymasq
  install -d -o keymasq -g keymasq -m 0750 /var/lib/keymasq
  setfacl -m u:keymasq:rw /dev/uinput
  for p in /dev/input/event*; do [ -e "\$p" ] && setfacl -m u:keymasq:rw "\$p"; done

The per-user session manager was installed as an XDG autostart entry for:

  $target_user

It runs:

  KEYMASQ_SESSION_RESTART_ON_DAEMON_DISCONNECT=1 /opt/keymasq/bin/keymasq-session

The daemon is not supervised until you add the service for your init system.
EOF
}

print_generic_service_instructions() {
	instructions=$(root_path "$INSTALL_DIR/share/keymasq/non-systemd-services.txt")
	if [ -f "$instructions" ]; then
		cat "$instructions" >&2
	fi
}

install_polkit_integration() {
	assets=$1
	install_file 0644 "$assets/50-keymasq-record.rules" \
		"$(root_path /etc/polkit-1/rules.d/50-keymasq-record.rules)"
	if steamos_detected; then
		return 0
	fi
	try_install_file 0644 "$assets/com.keymasq.record-macro.policy" \
		"$(root_path /usr/share/polkit-1/actions/com.keymasq.record-macro.policy)" || true
}

refresh_common_integration() {
	target_user=$1
	assets=$(asset_dir)
	install_root=$(root_path "$INSTALL_DIR")
	install -d -m 0755 "$install_root/bin" "$install_root/share/keymasq"

	for name in keymasq keymasqd keymasq-session keymasq-record waypipe gtk4-brotway-run; do
		write_wrapper "$name" "$install_root/bin/$name"
	done

	install_file 0644 "$assets/91-keymasq-acl.rules" \
		"$(root_path /etc/udev/rules.d/91-keymasq-acl.rules)"
	install_file 0644 "$assets/99-keymasq-hide-grabbed.rules" \
		"$(root_path /etc/udev/rules.d/99-keymasq-hide-grabbed.rules)"
	install_polkit_integration "$assets"
	install_file 0644 "$assets/appimage-update.gpg.asc" \
		"$install_root/share/keymasq/appimage-update.gpg.asc"

	install -d -m 0755 "$(root_path /etc/keymasq)"
	if [ ! -f "$(root_path /etc/keymasq/security.toml)" ]; then
		install_file 0644 "$assets/security-steamos.toml" "$(root_path /etc/keymasq/security.toml)"
	fi

	install_path_profile
	install_user_wrappers "$target_user"
	install_desktop_files "$target_user"
}

install_common_payload() {
	target_user=$1
	appimage_src=${KEYMASQ_APPIMAGE_SOURCE:-${APPIMAGE:-}}
	[ -n "$appimage_src" ] || die "APPIMAGE is not set; cannot install payload"
	[ -f "$appimage_src" ] || die "AppImage source does not exist: $appimage_src"

	install_root=$(root_path "$INSTALL_DIR")
	install -d -m 0755 "$install_root/bin" "$install_root/share/keymasq"
	appimage_dst="$install_root/$APPIMAGE_NAME"
	if [ "$(readlink -f "$appimage_src")" != "$(readlink -f "$appimage_dst" 2>/dev/null || printf '%s' "$appimage_dst")" ]; then
		install_file 0755 "$appimage_src" "$appimage_dst"
	else
		chmod 0755 "$appimage_dst"
	fi
	appimage_sha256=$(sha256sum "$appimage_dst" | awk '{print $1}')
	previous_runtime=$(current_runtime_id || true)
	prepare_runtime_from_appimage "$appimage_dst" "$appimage_sha256"
	activate_runtime "$appimage_sha256"
	appimage_version=$(appdir_version "$(runtime_path_for_sha256 "$appimage_sha256")" 2>/dev/null || true)
	write_installed_version "$appimage_version"
	prune_old_runtimes "$appimage_sha256" "$previous_runtime"

	refresh_common_integration "$target_user"
}

write_systemd_integration_files() {
	target_user=$1
	install_keep_list=$2
	assets=$(asset_dir)

	install_file 0644 "$assets/keymasq-sysusers.conf" "$(root_path /etc/sysusers.d/keymasq.conf)"
	install_file 0644 "$assets/keymasq-tmpfiles.conf" "$(root_path /etc/tmpfiles.d/keymasq.conf)"
	install_file 0644 "$assets/keymasqd.service" "$(root_path /etc/systemd/system/keymasqd.service)"
	install_user_service "$target_user"
	if [ "$install_keep_list" = 1 ]; then
		install_atomic_keep_list
	fi
}

refresh_systemd_integration() {
	target_user=$1
	install_keep_list=$2
	keymasqd_was_enabled=0
	session_was_enabled=0

	if systemctl is-enabled --quiet keymasqd.service; then
		keymasqd_was_enabled=1
	fi
	if run_user_systemctl "$target_user" is-enabled --quiet keymasq-session.service; then
		session_was_enabled=1
	fi

	write_systemd_integration_files "$target_user" "$install_keep_list"

	systemd-sysusers "$(root_path /etc/sysusers.d/keymasq.conf)"
	systemd-tmpfiles --create "$(root_path /etc/tmpfiles.d/keymasq.conf)"
	reload_udev_rules
	systemctl daemon-reload
	if [ "$keymasqd_was_enabled" = 1 ] && ! systemctl reenable keymasqd.service; then
		warn "could not reenable keymasqd.service"
	fi
	if ! run_user_systemctl "$target_user" daemon-reload; then
		warn "could not reload user systemd for $target_user"
	fi
	if [ "$session_was_enabled" = 1 ] && \
		! run_user_systemctl "$target_user" reenable keymasq-session.service; then
		warn "could not reenable user keymasq-session for $target_user"
	fi
}

install_systemd_integration() {
	target_user=$1
	install_keep_list=$2
	keymasqd_was_active=0
	session_was_active=0

	if systemctl is-active --quiet keymasqd.service; then
		keymasqd_was_active=1
	fi
	if run_user_systemctl "$target_user" is-active --quiet keymasq-session.service; then
		session_was_active=1
	fi

	refresh_systemd_integration "$target_user" "$install_keep_list"
	systemctl enable --now keymasqd.service
	if [ "$keymasqd_was_active" = 1 ]; then
		systemctl try-restart keymasqd.service
	fi
	if run_user_systemctl "$target_user" enable --now keymasq-session.service; then
		if [ "$session_was_active" = 1 ] && ! run_user_systemctl "$target_user" try-restart keymasq-session.service; then
			warn "could not restart active user keymasq-session for $target_user"
		fi
	else
		warn "could not enable user keymasq-session for $target_user"
	fi
}

refresh_generic_integration() {
	target_user=$1

	ensure_keymasq_user_and_dirs
	install_session_autostart "$target_user"
	reload_udev_rules
	write_generic_service_instructions "$target_user"
}

install_generic_integration() {
	target_user=$1

	refresh_generic_integration "$target_user"
	warn "systemd was not detected; keymasqd was not enabled"
	print_generic_service_instructions
}

refresh_installed_integration() {
	target_user=$1
	runtime_id=$2
	new_asset_dir="$(runtime_path_for_sha256 "$runtime_id")/share/keymasq/appimage"
	[ -d "$new_asset_dir" ] || die "updated runtime missing AppImage integration assets: $new_asset_dir"
	KEYMASQ_APPIMAGE_ASSET_DIR=$new_asset_dir
	export KEYMASQ_APPIMAGE_ASSET_DIR

	refresh_common_integration "$target_user"

	home=$(resolve_user_home "$target_user")
	install_keep_list=0
	if [ -f "$(root_path /etc/atomic-update.conf.d/keymasq.conf)" ] || steamos_detected; then
		install_keep_list=1
	fi

	if [ -f "$(root_path /etc/systemd/system/keymasqd.service)" ] || \
		[ -f "$(root_path "$home/.config/systemd/user/keymasq-session.service")" ]; then
		refresh_systemd_integration "$target_user" "$install_keep_list"
	elif [ -f "$(root_path "$home/.config/autostart/tools.keymasq.keymasq-session.desktop")" ] || \
		[ -f "$(root_path "$INSTALL_DIR/share/keymasq/non-systemd-services.txt")" ]; then
		refresh_generic_integration "$target_user"
	else
		reload_udev_rules
	fi
}

install_auto() {
	target_user=
	while [ "$#" -gt 0 ]; do
		case "$1" in
			--user)
				target_user=${2:-}
				[ -n "$target_user" ] || die "--user requires a value"
				shift 2
				;;
			*)
				die "unknown install option: $1"
				;;
		esac
	done

	target_user=$(resolve_target_user "$target_user")
	require_root_or_pkexec --install --user "$target_user"
	validate_user_wrapper_destinations "$target_user"
	install_common_payload "$target_user"

	if systemd_available; then
		if steamos_detected; then
			install_systemd_integration "$target_user" 1
		else
			install_systemd_integration "$target_user" 0
		fi
		log "$APP_NAME installed to $INSTALL_DIR for user $target_user"
	else
		install_generic_integration "$target_user"
		log "$APP_NAME core install completed at $INSTALL_DIR for user $target_user"
	fi
}

remove_path() {
	path=$1
	if [ -e "$path" ] || [ -L "$path" ]; then
		rm -rf "$path"
	fi
}

remove_user_path() {
	keymasq_remove_user=$1
	keymasq_remove_path=$2
	if ! run_as_user "$keymasq_remove_user" rm -rf -- "$keymasq_remove_path"; then
		die "failed to remove $keymasq_remove_path as $keymasq_remove_user"
	fi
}

remove_user_wrapper_if_managed() {
	keymasq_remove_user=$1
	keymasq_remove_name=$2
	keymasq_remove_path=$3
	if [ ! -e "$keymasq_remove_path" ] && [ ! -L "$keymasq_remove_path" ]; then
		return 0
	fi
	if user_wrapper_is_managed "$keymasq_remove_user" "$keymasq_remove_name" "$keymasq_remove_path"; then
		remove_user_path "$keymasq_remove_user" "$keymasq_remove_path"
	else
		warn "preserving non-Keymasq user command: $keymasq_remove_path"
	fi
}

uninstall_keymasq() {
	target_user=
	while [ "$#" -gt 0 ]; do
		case "$1" in
			--user)
				target_user=${2:-}
				[ -n "$target_user" ] || die "--user requires a value"
				shift 2
				;;
			*)
				die "unknown uninstall option: $1"
				;;
		esac
	done

	target_user=$(resolve_target_user "$target_user")
	require_root_or_pkexec --uninstall --user "$target_user"
	home=$(resolve_user_home "$target_user")

	systemctl disable --now keymasqd.service 2>/dev/null || true
	run_user_systemctl "$target_user" disable --now keymasq-session.service 2>/dev/null || true

	remove_path "$(root_path /etc/systemd/system/keymasqd.service)"
	remove_user_path "$target_user" "$(root_path "$home/.config/systemd/user/keymasq-session.service")"
	remove_path "$(root_path /etc/sysusers.d/keymasq.conf)"
	remove_path "$(root_path /etc/tmpfiles.d/keymasq.conf)"
	remove_path "$(root_path /etc/profile.d/keymasq.sh)"
	remove_path "$(root_path /etc/udev/rules.d/91-keymasq-acl.rules)"
	remove_path "$(root_path /etc/udev/rules.d/99-keymasq-hide-grabbed.rules)"
	clear_keymasq_udev_state || die "failed to clear Keymasq state from existing input devices"
	remove_path "$(root_path /etc/polkit-1/rules.d/50-keymasq-record.rules)"
	remove_path "$(root_path /usr/share/polkit-1/actions/com.keymasq.record-macro.policy)"
	remove_path "$(root_path /etc/atomic-update.conf.d/keymasq.conf)"
	remove_user_path "$target_user" "$(root_path "$home/.local/share/applications/tools.keymasq.keymasq.desktop")"
	remove_user_path "$target_user" "$(root_path "$home/.local/share/icons/hicolor/scalable/apps/tools.keymasq.keymasq.svg")"
	remove_user_path "$target_user" "$(root_path "$home/.config/autostart/tools.keymasq.keymasq-session.desktop")"
	remove_path "$(root_path "$INSTALL_DIR/share/keymasq/non-systemd-services.txt")"

	for name in keymasq keymasqd keymasq-session keymasq-record waypipe gtk4-brotway-run; do
		remove_user_wrapper_if_managed "$target_user" "$name" "$(root_path "$home/.local/bin/$name")"
	done
	remove_path "$(root_path "$INSTALL_DIR/bin")"
	remove_path "$(root_path "$INSTALL_DIR/$RUNTIME_DIR_NAME")"
	remove_path "$(root_path "$INSTALL_DIR/share")"
	remove_path "$(root_path "$INSTALL_DIR/$VERSION_FILE_NAME")"
	remove_path "$(root_path "$INSTALL_DIR/$APPIMAGE_NAME")"
	rmdir "$(root_path "$INSTALL_DIR")" 2>/dev/null || true

	systemctl daemon-reload 2>/dev/null || true
	run_user_systemctl "$target_user" daemon-reload 2>/dev/null || true

	log "$APP_NAME integration removed; /etc/keymasq, /var/lib/keymasq, and user config were left in place"
}

download_to() {
	url=$1
	dst=$2
	case "$url" in
		file://*)
			cp "${url#file://}" "$dst"
			;;
		/*)
			cp "$url" "$dst"
			;;
		*)
			if command -v curl >/dev/null 2>&1; then
				curl -fsSL "$url" -o "$dst"
			elif command -v wget >/dev/null 2>&1; then
				wget -qO "$dst" "$url"
			else
				die "curl or wget is required for updates"
			fi
			;;
	esac
}

parse_manifest_field() {
	manifest=$1
	field=$2
	run_python - "$manifest" "$field" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
value = data.get(sys.argv[2], "")
if not isinstance(value, str):
    raise SystemExit(2)
print(value)
PY
}

verify_manifest() {
	manifest=$1
	signature=$2
	public_key=$3
	[ -s "$public_key" ] || die "update public key is missing: $public_key"
	command -v gpg >/dev/null 2>&1 || die "gpg is required for AppImage updates"

	gpg_home=$(mktemp -d)
	chmod 0700 "$gpg_home"
	if (
		gpg --homedir "$gpg_home" --batch --no-tty --quiet \
			--import "$public_key" >/dev/null 2>&1
		gpg --homedir "$gpg_home" --batch --no-tty --verify \
			"$signature" "$manifest" >/dev/null 2>&1
	); then
		rm -rf "$gpg_home"
		return 0
	fi
	rm -rf "$gpg_home"
	die "update manifest signature verification failed"
}

self_update() {
	target_user=
	allow_unsigned=${KEYMASQ_APPIMAGE_ALLOW_UNSIGNED_UPDATE:-0}
	allow_downgrade=${KEYMASQ_APPIMAGE_ALLOW_DOWNGRADE:-0}
	while [ "$#" -gt 0 ]; do
		case "$1" in
			--user)
				target_user=${2:-}
				[ -n "$target_user" ] || die "--user requires a value"
				shift 2
				;;
			--allow-unsigned|--skip-signature-check)
				allow_unsigned=1
				shift
				;;
			--allow-downgrade)
				allow_downgrade=1
				shift
				;;
			*)
				die "unknown update option: $1"
				;;
		esac
	done

	set -- --self-update
	if [ "$allow_unsigned" = 1 ]; then
		set -- "$@" --allow-unsigned
	fi
	if [ "$allow_downgrade" = 1 ]; then
		set -- "$@" --allow-downgrade
	fi
	if [ -n "$target_user" ]; then
		set -- "$@" --user "$target_user"
	fi
	require_root_or_pkexec "$@"

	target_user=$(resolve_target_user "$target_user" 2>/dev/null || printf '%s' root)
	validate_user_wrapper_destinations "$target_user"
	arch=${KEYMASQ_APPIMAGE_ARCH:-$(uname -m)}
	[ "$arch" = x86_64 ] || die "self-update is currently supported only on x86_64"
	base_url=${KEYMASQ_APPIMAGE_UPDATE_BASE_URL:-$DEFAULT_UPDATE_BASE_URL}
	manifest_name=${KEYMASQ_APPIMAGE_UPDATE_MANIFEST:-$UPDATE_MANIFEST_NAME}
	public_key=${KEYMASQ_APPIMAGE_UPDATE_PUBLIC_KEY:-$(root_path "$INSTALL_DIR/share/keymasq/appimage-update.gpg.asc")}
	target=${KEYMASQ_APPIMAGE_TARGET:-$(root_path "$INSTALL_DIR/$APPIMAGE_NAME")}
	lock_path=$(root_path "$INSTALL_DIR/.update.lock")
	install -d -m 0755 "$(dirname "$target")"

	tmp_dir=$(mktemp -d)
	trap 'rm -rf "$tmp_dir"' EXIT HUP INT TERM
	manifest="$tmp_dir/manifest.json"
	signature="$tmp_dir/manifest.sig"
	new_appimage="$tmp_dir/$APPIMAGE_NAME"

	download_to "$base_url/$manifest_name" "$manifest"
	if [ "$allow_unsigned" = 1 ]; then
		warn "skipping AppImage update signature verification for testing"
	else
		download_to "$base_url/$manifest_name.sig" "$signature"
		verify_manifest "$manifest" "$signature" "$public_key"
	fi

	appimage_url=$(parse_manifest_field "$manifest" appimage_url)
	sha256=$(parse_manifest_field "$manifest" sha256)
	version=$(parse_manifest_field "$manifest" version)
	manifest_architecture=$(parse_manifest_field "$manifest" architecture)
	[ -n "$appimage_url" ] || die "update manifest missing appimage_url"
	[ -n "$sha256" ] || die "update manifest missing sha256"
	[ -n "$version" ] || die "update manifest missing version"
	[ -n "$manifest_architecture" ] || die "update manifest missing architecture"
	[ "$manifest_architecture" = "$arch" ] || \
		die "update manifest architecture $manifest_architecture does not match system architecture $arch"

	download_to "$appimage_url" "$new_appimage"
	actual_sha256=$(sha256sum "$new_appimage" | awk '{print $1}')
	[ "$actual_sha256" = "$sha256" ] || die "downloaded AppImage checksum mismatch"
	chmod 0755 "$new_appimage"

	(
		flock 9
		current_version=$(installed_version) || die "could not determine installed Keymasq version; refusing update"
		refuse_downgrade_unless_allowed "$current_version" "$version" "$allow_downgrade"
		previous_runtime=$(current_runtime_id || true)
		staged_target="$target.new.$$"
		trap 'rm -f "$staged_target"' EXIT HUP INT TERM
		install_file 0755 "$new_appimage" "$staged_target"
		prepare_runtime_from_appimage "$staged_target" "$actual_sha256"
		refresh_installed_integration "$target_user" "$actual_sha256"
		mv -Tf "$staged_target" "$target"
		trap - EXIT HUP INT TERM
		activate_runtime "$actual_sha256"
		write_installed_version "$version"
		prune_old_runtimes "$actual_sha256" "$previous_runtime"
	) 9>"$lock_path"

	restart_failed=0
	if [ -f "$(root_path /etc/systemd/system/keymasqd.service)" ] && \
		! systemctl try-restart keymasqd.service; then
		warn "could not restart keymasqd.service after update"
		restart_failed=1
	fi
	home=$(resolve_user_home "$target_user")
	if [ -f "$(root_path "$home/.config/systemd/user/keymasq-session.service")" ] && \
		! run_user_systemctl "$target_user" try-restart keymasq-session.service; then
		warn "could not restart keymasq-session.service for $target_user after update"
		restart_failed=1
	fi
	if [ "$restart_failed" = 1 ]; then
		die "$APP_NAME files were updated${version:+ to $version}, but one or more services failed to restart"
	fi

	log "$APP_NAME updated${version:+ to $version}"
}

resolve_appdir_python() {
	if [ -n "${APPDIR:-}" ]; then
		python_bin=
		for candidate in "$APPDIR"/shared/bin/python3.* "$APPDIR"/shared/bin/python3 "$APPDIR"/shared/bin/python; do
			if [ -x "$candidate" ]; then
				python_bin=$candidate
				break
			fi
		done
		loader=
		for candidate in "$APPDIR"/lib/ld-linux-x86-64.so.2 "$APPDIR"/lib/ld-linux-aarch64.so.1; do
			if [ -x "$candidate" ]; then
				loader=$candidate
				break
			fi
		done
		[ -n "$python_bin" ] && [ -n "$loader" ]
		return $?
	fi
	return 1
}

run_python() {
	if resolve_appdir_python; then
		(
			setup_appimage_python_environment
			"$loader" --library-path "$keymasq_library_path" "$python_bin" -P "$@"
		)
		return $?
	fi
	python -P "$@"
}

appimage_library_path() {
	if [ "${KEYMASQ_APPIMAGE_BROTWAY:-0}" = 1 ] && [ -d "$APPDIR/lib/gtk4-brotway" ]; then
		printf '%s:%s\n' "$APPDIR/lib/gtk4-brotway" "$APPDIR/lib"
	else
		printf '%s\n' "$APPDIR/lib"
	fi
}

setup_appimage_python_environment() {
	keymasq_library_path=$(appimage_library_path)
	export LD_LIBRARY_PATH="$keymasq_library_path${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
	export PYTHONHOME="$APPDIR"
	export PYTHONNOUSERSITE=true
	unset PYTHONPATH
	if [ -d "$APPDIR/lib/girepository-1.0" ]; then
		export GI_TYPELIB_PATH="$APPDIR/lib/girepository-1.0${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
	fi
	if [ -f "$APPDIR/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache" ]; then
		export GDK_PIXBUF_MODULE_FILE="$APPDIR/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache"
	fi
	if [ -d "$APPDIR/lib/gdk-pixbuf-2.0/2.10.0/loaders" ]; then
		export GDK_PIXBUF_MODULEDIR="$APPDIR/lib/gdk-pixbuf-2.0/2.10.0/loaders"
	fi
	if [ -d "$APPDIR/lib/gio/modules" ]; then
		export GIO_MODULE_DIR="$APPDIR/lib/gio/modules"
	fi
	if [ -d "$APPDIR/share/X11/xkb" ]; then
		export XKB_CONFIG_ROOT="$APPDIR/share/X11/xkb"
	fi
	case "${KEYMASQ_APPIMAGE_RENDERING:-auto}" in
		software)
			export GSK_RENDERER="${GSK_RENDERER:-cairo}"
			export GDK_DISABLE="${GDK_DISABLE:-gl,vulkan}"
			export GDK_GL="${GDK_GL:-disable}"
			export QT_QUICK_BACKEND="${QT_QUICK_BACKEND:-software}"
			;;
		auto|"")
			;;
		*)
			warn "unknown KEYMASQ_APPIMAGE_RENDERING value: $KEYMASQ_APPIMAGE_RENDERING"
			;;
	esac
	export XDG_DATA_DIRS="$APPDIR/share${XDG_DATA_DIRS:+:$XDG_DATA_DIRS}"
}

run_python_module() {
	module=$1
	shift
	if resolve_appdir_python; then
		setup_appimage_python_environment
		exec "$loader" --library-path "$keymasq_library_path" "$python_bin" -P -m "$module" "$@"
	fi
	exec python -P -m "$module" "$@"
}

dispatch_command() {
	command_name=$1
	shift
	case "$command_name" in
		keymasq)
			run_python_module keymasq "$@"
			;;
		keymasqd)
			run_python_module keymasq.keymasqd "$@"
			;;
		keymasq-session)
			run_python_module keymasq.session "$@"
			;;
		keymasq-record)
			run_python_module keymasq.record "$@"
			;;
		*)
			die "unknown command: $command_name"
			;;
	esac
}

main() {
	argv0=${ARGV0:-$0}
	basename=${argv0##*/}
	case "$basename" in
		keymasq)
			case "${1:-}" in
				--install|install|--uninstall|uninstall|--self-update|self-update)
					:
					;;
				*)
					dispatch_command "$basename" "$@"
					;;
			esac
			;;
		keymasqd|keymasq-session|keymasq-record)
			dispatch_command "$basename" "$@"
			;;
	esac

	case "${1:-keymasq}" in
		--install|install)
			shift
			install_auto "$@"
			;;
		--uninstall|uninstall)
			shift
			uninstall_keymasq "$@"
			;;
		--self-update|self-update)
			shift
			self_update "$@"
			;;
		keymasq|keymasqd|keymasq-session|keymasq-record)
			command_name=$1
			shift
			dispatch_command "$command_name" "$@"
			;;
		*)
			dispatch_command keymasq "$@"
			;;
	esac
}

main "$@"
