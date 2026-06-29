#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <Keymasq.AppImage>" >&2
  exit 2
fi

appimage="$(realpath "$1")"
if [[ ! -x "$appimage" ]]; then
  echo "AppImage is not executable: $appimage" >&2
  exit 1
fi

export APPIMAGE_EXTRACT_AND_RUN=1
export NO_AT_BRIDGE=1
unset DISPLAY
unset WAYLAND_DISPLAY
unset XDG_CURRENT_DESKTOP
unset DESKTOP_SESSION

"$appimage" --help >/dev/null
"$appimage" keymasq --help >/dev/null
"$appimage" keymasq-record --help >/dev/null

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
(
  cd "$tmp_dir"
  "$appimage" --appimage-extract >/dev/null
)
appdir=
for candidate in "$tmp_dir/AppDir" "$tmp_dir/squashfs-root"; do
  if [[ -d "$candidate" ]]; then
    appdir="$candidate"
    break
  fi
done
if [[ -z "$appdir" ]]; then
  echo "AppImage extraction did not produce AppDir or squashfs-root" >&2
  exit 1
fi

python_bin=
for candidate in "$appdir"/shared/bin/python3.* "$appdir"/shared/bin/python3 "$appdir"/shared/bin/python; do
  if [[ -x "$candidate" ]]; then
    python_bin="$candidate"
    break
  fi
done
if [[ -z "$python_bin" ]]; then
  echo "bundled Python was not found in extracted AppImage" >&2
  exit 1
fi

loader=
for candidate in "$appdir"/lib/ld-linux-x86-64.so.2 "$appdir"/lib/ld-linux-aarch64.so.1; do
  if [[ -x "$candidate" ]]; then
    loader="$candidate"
    break
  fi
done
if [[ -z "$loader" ]]; then
  echo "bundled dynamic loader was not found in extracted AppImage" >&2
  exit 1
fi

if [[ ! -x "$appdir/bin/slurp" ]]; then
  echo "bundled slurp launcher was not found in extracted AppImage" >&2
  exit 1
fi

export APPDIR="$appdir"
export LD_LIBRARY_PATH="$appdir/lib"
export PYTHONHOME="$appdir"
export PYTHONNOUSERSITE=true
export GI_TYPELIB_PATH="$appdir/lib/girepository-1.0"
export GIO_MODULE_DIR="$appdir/lib/gio/modules"
export XDG_DATA_DIRS="$appdir/share"
if [[ -f "$appdir/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache" ]]; then
  export GDK_PIXBUF_MODULE_FILE="$appdir/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache"
fi
if [[ -d "$appdir/lib/gdk-pixbuf-2.0/2.10.0/loaders" ]]; then
  export GDK_PIXBUF_MODULEDIR="$appdir/lib/gdk-pixbuf-2.0/2.10.0/loaders"
fi
if [[ -d "$appdir/share/X11/xkb" ]]; then
  export XKB_CONFIG_ROOT="$appdir/share/X11/xkb"
fi

"$loader" --library-path "$appdir/lib" "$python_bin" -P - <<'PY'
import pyexpat
import xml.etree.ElementTree as ET

import gi
import uvloop

ET.fromstring("<keymasq />")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: F401,E402
PY

"$appdir/bin/slurp" -h >/dev/null
