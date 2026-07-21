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
if [[ ! -x "$appdir/bin/waypipe" ]]; then
  echo "bundled waypipe launcher was not found in extracted AppImage" >&2
  exit 1
fi
if [[ ! -x "$appdir/bin/gtk4-brotway-run" ]]; then
  echo "bundled Brotway launcher was not found in extracted AppImage" >&2
  exit 1
fi
for executable in gtk4-broadwayd gtk4-brotway-run gtk4-brotway-debugmenu; do
  if [[ ! -x "$appdir/lib/gtk4-brotway/$executable" ]]; then
    echo "bundled Brotway executable was not found: $executable" >&2
    exit 1
  fi
done
if [[ ! -e "$appdir/lib/gtk4-brotway/libgtk-4.so.1" ]]; then
  echo "bundled Brotway GTK library was not found" >&2
  exit 1
fi
if [[ ! -f "$appdir/share/doc/keymasq/gtk4-brotway-manifest.json" ]]; then
  echo "bundled Brotway provenance manifest was not found" >&2
  exit 1
fi
if [[ ! -f "$appdir/share/icons/Keymasq/index.theme" ]]; then
  echo "private Keymasq icon theme was not found in extracted AppImage" >&2
  exit 1
fi
if [[ ! -f "$appdir/share/keymasq/appimage/gui-icon-names.txt" ]]; then
  echo "AppImage GUI icon manifest was not found" >&2
  exit 1
fi
python_package_manifest="$appdir/share/keymasq/appimage/python-runtime-site-packages.txt"
if [[ ! -f "$python_package_manifest" ]]; then
  echo "AppImage Python runtime package manifest was not found" >&2
  exit 1
fi
site_packages="$(find "$appdir/lib" -type d -name site-packages -print -quit)"
if [[ -z "$site_packages" ]]; then
  echo "bundled Python site-packages was not found" >&2
  exit 1
fi

unexpected_python_packages=()
while IFS= read -r package_name; do
  case "$package_name" in
    keymasq|python_keymasq-*.dist-info)
      continue
      ;;
  esac
  package_allowed=0
  while IFS= read -r pattern; do
    [[ -n "$pattern" && "$pattern" != \#* ]] || continue
    if [[ "$package_name" == $pattern ]]; then
      package_allowed=1
      break
    fi
  done < "$python_package_manifest"
  if [[ "$package_allowed" = 0 ]]; then
    unexpected_python_packages+=("$package_name")
  fi
done < <(find "$site_packages" -mindepth 1 -maxdepth 1 -printf '%f\n' | sort)
if [[ ${#unexpected_python_packages[@]} -ne 0 ]]; then
  printf 'undeclared Python packages found in AppImage: %s\n' \
    "${unexpected_python_packages[*]}" >&2
  exit 1
fi

export APPDIR="$appdir"
export LD_LIBRARY_PATH="$appdir/lib"
export PYTHONHOME="$appdir"
export PYTHONNOUSERSITE=true
unset PYTHONPATH
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

import cairo
import dbus_next
import evdev
import gi
import six
import tomli_w
import uvloop
from Xlib import X, display  # noqa: F401

ET.fromstring("<keymasq />")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: F401,E402
PY

"$appdir/bin/slurp" -h >/dev/null
"$appdir/bin/waypipe" --help >/dev/null
# The pinned Brotway launcher promises that --help exits without starting a
# daemon or requiring a display. Keep this smoke check in sync when repinning.
"$appdir/bin/gtk4-brotway-run" --help >/dev/null

(
  export LD_LIBRARY_PATH="$appdir/lib/gtk4-brotway:$appdir/lib"
  "$loader" --library-path "$appdir/lib/gtk4-brotway:$appdir/lib" "$python_bin" -P - <<'PY'
import os
from importlib import resources
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

appdir = Path(os.environ["APPDIR"])
manifest = appdir / "share/keymasq/appimage/gui-icon-names.txt"
icon_names = manifest.read_text(encoding="utf-8").splitlines()
theme = Gtk.IconTheme.new()
theme.set_search_path([str(appdir / "share/icons")])
theme.set_theme_name("Keymasq")

missing = [name for name in icon_names if not theme.has_icon(name)]
assert not missing, f"private AppImage icon theme is missing: {missing}"
for name in icon_names:
    paintable = theme.lookup_icon(
        name,
        [],
        32,
        1,
        Gtk.TextDirection.NONE,
        Gtk.IconLookupFlags.PRELOAD,
    )
    icon_file = paintable.get_file()
    assert icon_file is not None, f"private icon did not resolve to a file: {name}"
    icon_path = icon_file.get_path()
    assert icon_path is not None and icon_path.endswith(".png"), (name, icon_path)
    texture = Gdk.Texture.new_from_filename(icon_path)
    assert texture.get_width() > 0 and texture.get_height() > 0, name

gamepad = resources.files("keymasq").joinpath("gui/assets/gamepad.png")
with resources.as_file(gamepad) as gamepad_path:
    gamepad_texture = Gdk.Texture.new_from_filename(str(gamepad_path))
assert (gamepad_texture.get_width(), gamepad_texture.get_height()) == (660, 430)
PY
)
