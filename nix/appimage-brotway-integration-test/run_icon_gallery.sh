#!/bin/sh
set -eu

die() {
	printf 'run_icon_gallery: %s\n' "$*" >&2
	exit 1
}

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
gallery=${KEYMASQ_ICON_GALLERY_SCRIPT:-$script_dir/icon_gallery.py}
appdir=${KEYMASQ_APPDIR:-/opt/keymasq/runtime/current}
address=${KEYMASQ_ICON_GALLERY_ADDRESS:-0.0.0.0}
port=${KEYMASQ_ICON_GALLERY_PORT:-18101}

[ -f "$gallery" ] || die "gallery script not found: $gallery"
[ -x "$appdir/bin/gtk4-brotway-run" ] || die "Brotway launcher not found in $appdir"

python_bin=
for candidate in "$appdir"/shared/bin/python3.* "$appdir"/shared/bin/python3 "$appdir"/shared/bin/python; do
	if [ -x "$candidate" ]; then
		python_bin=$candidate
		break
	fi
done
[ -n "$python_bin" ] || die "bundled Python interpreter not found"

loader=
for candidate in "$appdir"/lib/ld-linux-x86-64.so.2 "$appdir"/lib/ld-linux-aarch64.so.1; do
	if [ -x "$candidate" ]; then
		loader=$candidate
		break
	fi
done
[ -n "$loader" ] || die "bundled dynamic loader not found"

library_path=$appdir/lib/gtk4-brotway:$appdir/lib
export APPDIR="$appdir"
export PYTHONHOME="$appdir"
export PYTHONNOUSERSITE=true
unset PYTHONPATH
export GI_TYPELIB_PATH="$appdir/lib/girepository-1.0"
export GIO_MODULE_DIR="$appdir/lib/gio/modules"
export XDG_DATA_DIRS="$appdir/share${XDG_DATA_DIRS:+:$XDG_DATA_DIRS}"
if [ -d "$appdir/share/X11/xkb" ]; then
	export XKB_CONFIG_ROOT="$appdir/share/X11/xkb"
fi

set -- "$python_bin" -P "$gallery"
if [ -n "${KEYMASQ_ICON_GALLERY_RESULT:-}" ]; then
	set -- "$@" --result-json "$KEYMASQ_ICON_GALLERY_RESULT"
fi
if [ -n "${KEYMASQ_ICON_GALLERY_QUIT_AFTER:-}" ]; then
	set -- "$@" --quit-after "$KEYMASQ_ICON_GALLERY_QUIT_AFTER"
fi

exec "$appdir/bin/gtk4-brotway-run" \
	--address "$address" \
	--port "$port" \
	"$loader" --library-path "$library_path" "$@"
