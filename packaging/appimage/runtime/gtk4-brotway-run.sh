#!/bin/sh
set -eu

die() {
	printf 'gtk4-brotway-run: %s\n' "$*" >&2
	exit 1
}

appdir=${APPDIR:-}
if [ -z "$appdir" ]; then
	appdir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
fi

brotway_prefix=$appdir/lib/gtk4-brotway
brotway_launcher=$brotway_prefix/gtk4-brotway-run
[ -x "$brotway_launcher" ] || die "missing bundled Brotway launcher"

loader=
for candidate in "$appdir"/lib/ld-linux-x86-64.so.2 "$appdir"/lib/ld-linux-aarch64.so.1; do
	if [ -x "$candidate" ]; then
		loader=$candidate
		break
	fi
done
[ -n "$loader" ] || die "missing bundled dynamic loader"

python_bin=
for candidate in "$appdir"/shared/bin/python3.* "$appdir"/shared/bin/python3 "$appdir"/shared/bin/python; do
	if [ -x "$candidate" ]; then
		python_bin=$candidate
		break
	fi
done
[ -n "$python_bin" ] || die "missing bundled Python interpreter"

brotway_port=${BROTWAY_PORT:-}
brotway_address=${BROTWAY_ADDRESS:-127.0.0.1}
brotway_display=${BROTWAY_DISPLAY:-:5}
brotway_auto=0
brotway_help=0
brotway_option=
brotway_scanning=1
for argument do
	if [ -n "$brotway_option" ]; then
		case "$brotway_option" in
		port) brotway_port=$argument ;;
		address) brotway_address=$argument ;;
		display) brotway_display=$argument ;;
		esac
		brotway_option=
		continue
	fi
	[ "$brotway_scanning" = 1 ] || continue
	case "$argument" in
	--port) brotway_option=port ;;
	--address) brotway_option=address ;;
	--display) brotway_option=display ;;
	--auto) brotway_auto=1 ;;
	-h|--help) brotway_help=1 ;;
	--) brotway_scanning=0 ;;
	-*) ;;
	*) brotway_scanning=0 ;;
	esac
done
[ -z "$brotway_option" ] || die "missing value for --$brotway_option"

if [ "$brotway_auto" = 0 ] && [ "$brotway_help" = 0 ]; then
	if [ -z "$brotway_port" ]; then
		display_number=${brotway_display#:}
		case "$display_number" in
		''|*[!0-9]*) die "invalid Broadway display: $brotway_display" ;;
		esac
		brotway_port=$((8080 + display_number))
	fi
	case "$brotway_port" in
	''|*[!0-9]*) die "invalid Brotway port: $brotway_port" ;;
	esac
	if ! "$loader" --library-path "$appdir/lib" "$python_bin" -P - \
		"$brotway_address" "$brotway_port" <<'PY'
import socket
import sys

address, port_text = sys.argv[1:]
port = int(port_text)
family = socket.AF_INET6 if ":" in address else socket.AF_INET
sock = socket.socket(family, socket.SOCK_STREAM)
try:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((address, port))
    sock.listen(1)
except OSError:
    raise SystemExit(1)
finally:
    sock.close()
PY
	then
		die "port $brotway_port is already in use on $brotway_address"
	fi
fi

library_path=$brotway_prefix:$appdir/lib
export APPDIR="$appdir"
export BROTWAY_PREFIX="$brotway_prefix"
export BROTWAY_LOADER="$loader"
export BROTWAY_LIBRARY_PATH="$library_path"
export BROTWAY_HELPER_PATH="$appdir/bin"
export BROTWAY_ADDRESS="$brotway_address"
export BROTWAY_DISPLAY="$brotway_display"
export KEYMASQ_APPIMAGE_BROTWAY=1
# Broadway has its own software GSK renderer. In particular, the generic
# AppImage `software` mode selects GTK's Cairo renderer, which does not speak
# the Broadway scene-graph protocol correctly.
export KEYMASQ_APPIMAGE_RENDERING=auto
export GSK_RENDERER=broadway
# Keymasq's no-argument entrypoint uses display presence to select the GUI.
# Never inherit a host X11 or desktop display into this Brotway-only path.
export DISPLAY="$brotway_display"
# Do not export the full AppImage library path here. The target may first pass
# through a host shell, which must not load unrelated AppImage libraries.
# BROTWAY_LIBRARY_PATH gives the bundled loader the same path explicitly.
export PATH="$appdir/bin${PATH:+:$PATH}"

exec "$loader" --library-path "$library_path" "$brotway_launcher" "$@"
