#!/bin/sh
set -eu

die() {
	printf 'gtk4-brotway-debugmenu: %s\n' "$*" >&2
	exit 1
}

appdir=${APPDIR:-}
if [ -z "$appdir" ]; then
	appdir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
fi

brotway_prefix=$appdir/lib/gtk4-brotway
debugmenu=$brotway_prefix/gtk4-brotway-debugmenu
[ -x "$debugmenu" ] || die "missing bundled Brotway debug menu"

loader=
for candidate in "$appdir"/lib/ld-linux-x86-64.so.2 "$appdir"/lib/ld-linux-aarch64.so.1; do
	if [ -x "$candidate" ]; then
		loader=$candidate
		break
	fi
done
[ -n "$loader" ] || die "missing bundled dynamic loader"

library_path=$brotway_prefix:$appdir/lib
export APPDIR="$appdir"
export BROTWAY_PREFIX="$brotway_prefix"
export KEYMASQ_APPIMAGE_BROTWAY=1
export GDK_BACKEND="${GDK_BACKEND:-broadway}"

exec "$loader" --library-path "$library_path" "$debugmenu" "$@"
