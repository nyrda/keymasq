#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD_ROOT="${KEYMASQ_APPIMAGE_BUILD_ROOT:-$REPO_ROOT/build/appimage}"
APPDIR="${KEYMASQ_APPIMAGE_APPDIR:-$BUILD_ROOT/AppDir}"
WORKDIR="$BUILD_ROOT/work"
STAGING="$BUILD_ROOT/python-staging"
APPIMAGE_OUT="$BUILD_ROOT/out"
DIST_DIR="${OUTPATH:-$REPO_ROOT/dist/appimage}"
ARCH="$(uname -m)"
ANYLINUX_REV="${KEYMASQ_APPIMAGE_ANYLINUX_REV:-b3a9e985cdedf7efa81d172f182cd13983743147}"
QUICK_SHARUN_URL="${KEYMASQ_APPIMAGE_QUICK_SHARUN_URL:-https://raw.githubusercontent.com/pkgforge-dev/Anylinux-AppImages/$ANYLINUX_REV/useful-tools/quick-sharun.sh}"
QUICK_SHARUN_SHA256="${KEYMASQ_APPIMAGE_QUICK_SHARUN_SHA256:-4b765d0b38621852f5fc6cbf460641fcce6103b548582d58942abc65ab7dbd93}"
ANYLINUX_SOURCE_URL="${KEYMASQ_APPIMAGE_ANYLINUX_SOURCE_URL:-https://raw.githubusercontent.com/pkgforge-dev/Anylinux-AppImages/$ANYLINUX_REV/useful-tools/lib/anylinux.c}"
ANYLINUX_SOURCE_SHA256="${KEYMASQ_APPIMAGE_ANYLINUX_SOURCE_SHA256:-f29196b8c8e1ad8c76002eed1b9a7149000b40442514074262ede01a4f60ae05}"
SHARUN_VERSION="${KEYMASQ_APPIMAGE_SHARUN_VERSION:-2.2.3}"
APPIMAGETOOL_VERSION="${KEYMASQ_APPIMAGE_APPIMAGETOOL_VERSION:-0.3.2}"
URUNTIME_VERSION="${KEYMASQ_APPIMAGE_URUNTIME_VERSION:-0.5.8}"
DWARFS_VERSION="${KEYMASQ_APPIMAGE_DWARFS_VERSION:-0.15.3}"
BROTWAY_BUNDLE_VERSION="${KEYMASQ_APPIMAGE_BROTWAY_BUNDLE_VERSION:-keymasq-v2}"
BROTWAY_BUNDLE_NAME="${KEYMASQ_APPIMAGE_BROTWAY_BUNDLE_NAME:-gtk4-brotway-keymasq-$ARCH.tar.zst}"
BROTWAY_BUNDLE_URL="${KEYMASQ_APPIMAGE_BROTWAY_BUNDLE_URL:-https://github.com/nyrda/gtk-brotway/releases/download/$BROTWAY_BUNDLE_VERSION/$BROTWAY_BUNDLE_NAME}"
case "$ARCH" in
  x86_64)
    BROTWAY_BUNDLE_DEFAULT_SHA256="4827d8ba915f875ca0437794625b713232c670adf05bf210a9d4103dfb03e388"
    ;;
  *)
    # keymasq-v2 currently publishes only x86_64. Experimental architectures
    # must provide a matching bundle and checksum explicitly.
    BROTWAY_BUNDLE_DEFAULT_SHA256=""
    ;;
esac
BROTWAY_BUNDLE_SHA256="${KEYMASQ_APPIMAGE_BROTWAY_BUNDLE_SHA256:-$BROTWAY_BUNDLE_DEFAULT_SHA256}"

download_to() {
  local url="$1"
  local dst="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$dst"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$dst" "$url"
  else
    echo "curl or wget is required to download AppImage build inputs" >&2
    exit 1
  fi
}

verify_sha256() {
  local label="$1"
  local path="$2"
  local expected="$3"
  local actual

  actual="$(sha256sum "$path" | awk '{print $1}')"
  if [[ "$actual" != "$expected" ]]; then
    echo "$label checksum mismatch for $path" >&2
    echo "expected: $expected" >&2
    echo "actual:   $actual" >&2
    exit 1
  fi
}

download_verified() {
  local label="$1"
  local url="$2"
  local expected="$3"
  local dst="$4"
  local tmp="$dst.download"

  rm -f "$tmp"
  download_to "$url" "$tmp"
  verify_sha256 "$label" "$tmp" "$expected"
  mv -f "$tmp" "$dst"
}

resolve_bundle_python() {
  python - <<'PY'
from pathlib import Path
import shutil

for name in ("python3", "python"):
    path = shutil.which(name)
    if path:
        print(Path(path).resolve())
        break
else:
    raise SystemExit("python is required")
PY
}

resolve_runtime_site_packages() {
  "$PYTHON_EXE" - <<'PY'
from pathlib import Path
import site
import sysconfig

candidates = [
    *site.getsitepackages(),
    sysconfig.get_path("purelib"),
    sysconfig.get_path("platlib"),
]
seen: set[Path] = set()
for raw in candidates:
    if not raw:
        continue
    path = Path(raw)
    if path in seen:
        continue
    seen.add(path)
    if path.exists() and path.name == "site-packages":
        print(path)
        break
else:
    raise SystemExit("failed to resolve Python site-packages")
PY
}

resolve_version() {
  "$PYTHON_EXE" - <<'PY'
from pathlib import Path
import tomllib

print(tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]["version"])
PY
}

resolve_quick_sharun() {
  local dst="$BUILD_ROOT/quick-sharun"
  local candidate
  if [[ -n "${KEYMASQ_APPIMAGE_QUICK_SHARUN:-}" ]]; then
    verify_quick_sharun "$KEYMASQ_APPIMAGE_QUICK_SHARUN"
    printf '%s\n' "$KEYMASQ_APPIMAGE_QUICK_SHARUN"
    return 0
  fi
  if command -v quick-sharun >/dev/null 2>&1; then
    candidate="$(command -v quick-sharun)"
    verify_quick_sharun "$candidate"
    printf '%s\n' "$candidate"
    return 0
  fi
  download_verified quick-sharun "$QUICK_SHARUN_URL" "$QUICK_SHARUN_SHA256" "$dst"
  chmod 0755 "$dst"
  printf '%s\n' "$dst"
}

verify_quick_sharun() {
  local path="$1"

  verify_sha256 quick-sharun "$path" "$QUICK_SHARUN_SHA256"
}

prepare_verified_appimage_inputs() {
  local vendor_dir="$BUILD_ROOT/verified-inputs"
  local anylinux_source="$vendor_dir/anylinux.c"
  local sharun="$vendor_dir/sharun"
  local appimagetool="$vendor_dir/appimagetool"
  local uruntime="$vendor_dir/uruntime"
  local mkdwarfs="$vendor_dir/mkdwarfs"
  local sharun_url
  local sharun_sha256
  local appimagetool_url
  local appimagetool_sha256
  local uruntime_url
  local uruntime_sha256
  local mkdwarfs_url
  local mkdwarfs_sha256

  case "$ARCH" in
    x86_64)
      sharun_sha256="${KEYMASQ_APPIMAGE_SHARUN_SHA256:-9befc93354f595bf1b6d2898cf745aab93838079a63400af1136d6b072ec13fa}"
      appimagetool_sha256="${KEYMASQ_APPIMAGE_APPIMAGETOOL_SHA256:-44bedf8868cf6ff9f90654ad7260e6d1c0966d20276dced41fa8450f18eb2214}"
      uruntime_sha256="${KEYMASQ_APPIMAGE_URUNTIME_SHA256:-7edba92a430f71a2dcfe161ef9d143d0379894443666769ecf8884f41034d236}"
      mkdwarfs_sha256="${KEYMASQ_APPIMAGE_DWARFS_SHA256:-30c868737fdf7b4167b35650839bcbce0fd73d203f83702bc0afa7eaa2e1bded}"
      ;;
    aarch64)
      sharun_sha256="${KEYMASQ_APPIMAGE_SHARUN_SHA256:-3b4bfc45fe13634fbcf8d647e08ef4ac69bc55b38234944e00da588820e4090c}"
      appimagetool_sha256="${KEYMASQ_APPIMAGE_APPIMAGETOOL_SHA256:-c7c7678c1370f9390fd4d4d3beb08109680090dcdb673ee212367c6872084201}"
      uruntime_sha256="${KEYMASQ_APPIMAGE_URUNTIME_SHA256:-6031dade6e86841a3bfa0d11fe2ae51611f242b7f670a5e3e4f4e7d3e89280d1}"
      mkdwarfs_sha256="${KEYMASQ_APPIMAGE_DWARFS_SHA256:-87a514821c762371a50dac7a271a6c78af3a23cc0c7cc47571713f8fb6707684}"
      ;;
    *)
      echo "No verified AppImage helper inputs are configured for $ARCH." >&2
      exit 1
      ;;
  esac

  sharun_url="${KEYMASQ_APPIMAGE_SHARUN_URL:-https://github.com/pkgforge-dev/sharun/releases/download/$SHARUN_VERSION/sharun-$ARCH}"
  appimagetool_url="${KEYMASQ_APPIMAGE_APPIMAGETOOL_URL:-https://github.com/pkgforge-dev/appimagetool/releases/download/$APPIMAGETOOL_VERSION/appimagetool-$ARCH-linux}"
  uruntime_url="${KEYMASQ_APPIMAGE_URUNTIME_URL:-https://github.com/VHSgunzo/uruntime/releases/download/v$URUNTIME_VERSION/uruntime-appimage-dwarfs-lite-$ARCH}"
  mkdwarfs_url="${KEYMASQ_APPIMAGE_DWARFS_URL:-https://github.com/mhx/dwarfs/releases/download/v$DWARFS_VERSION/dwarfs-universal-$DWARFS_VERSION-Linux-$ARCH}"

  install -d -m 0755 "$vendor_dir" "$APPDIR/lib"
  download_verified anylinux.c "$ANYLINUX_SOURCE_URL" "$ANYLINUX_SOURCE_SHA256" "$anylinux_source"
  download_verified sharun "$sharun_url" "$sharun_sha256" "$sharun"
  download_verified appimagetool "$appimagetool_url" "$appimagetool_sha256" "$appimagetool"
  download_verified uruntime "$uruntime_url" "$uruntime_sha256" "$uruntime"
  download_verified mkdwarfs "$mkdwarfs_url" "$mkdwarfs_sha256" "$mkdwarfs"

  install -m 0755 "$sharun" "$APPDIR/sharun"
  cc -shared -fPIC -O2 "$anylinux_source" -o "$APPDIR/lib/anylinux.so"
  chmod 0755 "$appimagetool" "$uruntime" "$mkdwarfs"
  APPIMAGETOOL="$appimagetool"
  RUNTIME="$uruntime"
  DWARFS_CMD="$mkdwarfs"
  export APPIMAGETOOL RUNTIME DWARFS_CMD
}

remove_generated_hardcoded_path_mapping() {
  local hook="$APPDIR/bin/01-path-mapping-hardcoded.hook"
  [[ -f "$hook" ]] || return 0

  "$PYTHON_EXE" - "$APPDIR" "$hook" <<'PY'
from pathlib import Path
import re
import sys

appdir = Path(sys.argv[1])
hook = Path(sys.argv[2])
hook_text = hook.read_text(encoding="utf-8")
replacements: list[tuple[bytes, bytes]] = []

for kind in ("bin", "lib", "share"):
    match = re.search(rf"^_tmp_{kind}=(.*)$", hook_text, re.MULTILINE)
    if match is None:
        continue
    token = match.group(1).strip().strip("\"'")
    if not token:
        continue
    generated = f"/tmp/{token}".encode()
    original = f"/usr/{kind}".encode()
    if len(generated) != len(original):
        raise SystemExit(
            f"cannot restore quick-sharun {kind} mapping with unequal path lengths"
        )
    replacements.append((generated, original))

for root in (appdir / "lib", appdir / "shared/bin"):
    if not root.is_dir():
        continue
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        content = path.read_bytes()
        restored = content
        for generated, original in replacements:
            restored = restored.replace(generated, original)
        if restored != content:
            path.write_bytes(restored)

hook.unlink()

for root in (appdir / "lib", appdir / "shared/bin"):
    if not root.is_dir():
        continue
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        content = path.read_bytes()
        for generated, _original in replacements:
            if generated in content:
                raise SystemExit(f"quick-sharun path mapping remains in {path}")
PY
}

require_python_modules() {
  "$PYTHON_EXE" - <<'PY'
import build
import cairo
import dbus_next
import evdev
import gi
import installer
import tomli_w
import uvloop
import Xlib

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
PY
}

copy_source_tree() {
  rsync -a \
    --exclude .git \
    --exclude .direnv \
    --exclude .mypy_cache \
    --exclude .pytest_cache \
    --exclude .ruff_cache \
    --exclude .venv \
    --exclude build \
    --exclude dist \
    --exclude pkg \
    "$REPO_ROOT/" "$WORKDIR/source/"

  cat >"$WORKDIR/source/keymasq/common/build_paths.py" <<'PY'
KEYMASQ_RECORD_HELPER_PATH = "/opt/keymasq/bin/keymasq-record"
SLURP_PATH = "/opt/keymasq/runtime/current/bin/slurp"
PY
}

install_runtime_files() {
  local launcher="$WORKDIR/keymasq-appimage-runtime"

  install -Dm755 "$REPO_ROOT/packaging/appimage/runtime/keymasq-appimage-runtime.sh" "$launcher"
  for name in keymasq keymasqd keymasq-session keymasq-record; do
    install -Dm755 "$launcher" "$APPDIR/bin/$name"
  done

  cp -a "$REPO_ROOT/packaging/appimage/assets/." "$APPDIR/share/keymasq/appimage/"
  cp -a "$REPO_ROOT/udev/91-keymasq-acl.rules" "$APPDIR/share/keymasq/appimage/"
  cp -a "$REPO_ROOT/udev/99-keymasq-hide-grabbed.rules" "$APPDIR/share/keymasq/appimage/"
  cp -a "$REPO_ROOT/assets/tools.keymasq.keymasq.svg" "$APPDIR/share/keymasq/appimage/"
}

resolve_brotway_bundle() {
  local dst="$BUILD_ROOT/verified-inputs/$BROTWAY_BUNDLE_NAME"
  local local_bundle="${KEYMASQ_APPIMAGE_BROTWAY_BUNDLE:-}"

  if [[ -z "$BROTWAY_BUNDLE_SHA256" ]]; then
    echo "KEYMASQ_APPIMAGE_BROTWAY_BUNDLE_SHA256 is required for Brotway bundles" >&2
    return 1
  fi

  if [[ -n "$local_bundle" ]]; then
    local_bundle="$(realpath "$local_bundle")"
    [[ -f "$local_bundle" ]] || {
      echo "local Brotway bundle does not exist: $local_bundle" >&2
      return 1
    }
    verify_sha256 "gtk-brotway bundle" "$local_bundle" "$BROTWAY_BUNDLE_SHA256"
    printf '%s\n' "$local_bundle"
    return 0
  fi

  download_verified \
    "gtk-brotway bundle" \
    "$BROTWAY_BUNDLE_URL" \
    "$BROTWAY_BUNDLE_SHA256" \
    "$dst"
  printf '%s\n' "$dst"
}

install_brotway_runtime() {
  local bundle
  local bundle_root="$WORKDIR/gtk-brotway-bundle"

  if ! command -v bsdtar >/dev/null 2>&1; then
    echo "bsdtar is required to extract the Brotway bundle" >&2
    return 1
  fi

  bundle="$(resolve_brotway_bundle)"

  rm -rf "$bundle_root"
  install -d -m 0755 "$bundle_root"
  bsdtar -xf "$bundle" -C "$bundle_root"

  local brotway_root="$bundle_root/lib/gtk4-brotway"
  for executable in gtk4-broadwayd gtk4-brotway-run gtk4-brotway-debugmenu; do
    [[ -x "$brotway_root/$executable" ]] || {
      echo "Brotway bundle is missing executable: lib/gtk4-brotway/$executable" >&2
      return 1
    }
  done
  [[ -e "$brotway_root/libgtk-4.so.1" ]] || {
    echo "Brotway bundle is missing lib/gtk4-brotway/libgtk-4.so.1" >&2
    return 1
  }
  [[ -f "$bundle_root/manifest.json" ]] || {
    echo "Brotway bundle is missing manifest.json" >&2
    return 1
  }
  [[ -f "$bundle_root/share/licenses/gtk4-brotway/COPYING" ]] || {
    echo "Brotway bundle is missing its license" >&2
    return 1
  }

  install -d -m 0755 \
    "$APPDIR/lib/gtk4-brotway" \
    "$APPDIR/share/licenses/gtk4-brotway" \
    "$APPDIR/share/doc/keymasq"
  cp -a "$brotway_root/." "$APPDIR/lib/gtk4-brotway/"
  install -Dm644 \
    "$bundle_root/share/licenses/gtk4-brotway/COPYING" \
    "$APPDIR/share/licenses/gtk4-brotway/COPYING"
  install -Dm644 "$bundle_root/manifest.json" \
    "$APPDIR/share/doc/keymasq/gtk4-brotway-manifest.json"
  install -Dm755 \
    "$REPO_ROOT/packaging/appimage/runtime/gtk4-brotway-run.sh" \
    "$APPDIR/bin/gtk4-brotway-run"
  install -Dm755 \
    "$REPO_ROOT/packaging/appimage/runtime/gtk4-brotway-debugmenu.sh" \
    "$APPDIR/bin/gtk4-brotway-debugmenu"
}

verify_brotway_glibc_compatibility() {
  local brotway_gtk="$APPDIR/lib/gtk4-brotway/libgtk-4.so.1"
  local appimage_libc="$APPDIR/lib/libc.so.6"

  bash "$REPO_ROOT/packaging/appimage/check-glibc-compatibility.sh" \
    "$brotway_gtk" "$appimage_libc"
}

ensure_bundled_python_command() {
  local versioned
  versioned="$("$PYTHON_EXE" - <<'PY'
import sys

print(f"python{sys.version_info.major}.{sys.version_info.minor}")
PY
)"
  install -d -m 0755 "$APPDIR/shared/bin"

  if [[ ! -x "$APPDIR/shared/bin/$versioned" ]]; then
    if [[ -x "$APPDIR/shared/bin/python3" ]]; then
      ln -sfn python3 "$APPDIR/shared/bin/$versioned"
    elif [[ -x "$APPDIR/shared/bin/python" ]]; then
      ln -sfn python "$APPDIR/shared/bin/$versioned"
    else
      install -Dm755 "$PYTHON_EXE" "$APPDIR/shared/bin/$versioned"
    fi
  fi

  if [[ ! -e "$APPDIR/shared/bin/python3" ]]; then
    ln -sfn "$versioned" "$APPDIR/shared/bin/python3"
  fi
}

find_bundled_python_lib_dir() {
  find "$APPDIR/lib" -maxdepth 1 -type d -name 'python3*' -print -quit
}

copy_python_packages() {
  local python_lib_dir="$1"
  local bundled_site="$python_lib_dir/site-packages"
  local manifest="$REPO_ROOT/packaging/appimage/assets/python-runtime-site-packages.txt"
  local pattern
  local source
  local staged_site
  local runtime_site
  local -a matches

  staged_site="$(find "$STAGING" -type d -name site-packages -print -quit)"
  if [[ -z "$staged_site" ]]; then
    echo "failed to locate staged site-packages" >&2
    return 1
  fi
  [[ -f "$manifest" ]] || {
    echo "AppImage Python runtime package manifest is missing: $manifest" >&2
    return 1
  }

  runtime_site="$(resolve_runtime_site_packages)"
  case "$bundled_site" in
    "$APPDIR"/lib/python*/site-packages) ;;
    *)
      echo "refusing to replace unexpected bundled site-packages: $bundled_site" >&2
      return 1
      ;;
  esac
  rm -rf "$bundled_site"
  mkdir -p "$bundled_site"
  while IFS= read -r pattern; do
    [[ -n "$pattern" && "$pattern" != \#* ]] || continue
    matches=()
    mapfile -t matches < <(compgen -G "$runtime_site/$pattern" || true)
    if [[ ${#matches[@]} -eq 0 ]]; then
      echo "required AppImage Python runtime package is missing: $pattern" >&2
      return 1
    fi
    for source in "${matches[@]}"; do
      cp -aL "$source" "$bundled_site/"
    done
  done < "$manifest"
  chmod -R u+w "$bundled_site"
  cp -a "$staged_site"/keymasq "$bundled_site/"
  cp -a "$staged_site"/python_keymasq-*.dist-info "$bundled_site/"
}

install_appimage_icons() {
  local python_lib_dir="$1"
  local package_assets="$python_lib_dir/site-packages/keymasq/gui/assets"
  local manifest="$REPO_ROOT/packaging/appimage/assets/gui-icon-names.txt"
  local theme_root="$APPDIR/share/icons/Keymasq"
  local source_root="$WORKDIR/appimage-icon-sources"
  local icon_name
  local source_name
  local source_svg
  local staged_svg
  local size
  local output_dir

  for command in gtk-update-icon-cache rsvg-convert; do
    if ! command -v "$command" >/dev/null 2>&1; then
      echo "$command is required to build the AppImage icon payload" >&2
      return 1
    fi
  done
  [[ -f "$manifest" ]] || {
    echo "AppImage GUI icon manifest is missing: $manifest" >&2
    return 1
  }
  [[ -d /usr/share/icons/Adwaita ]] || {
    echo "Adwaita icon sources are required to build the AppImage icon payload" >&2
    return 1
  }

  install -d -m 0755 "$source_root" "$theme_root"
  install -Dm644 \
    "$REPO_ROOT/packaging/appimage/assets/keymasq-icon-theme.index.theme" \
    "$theme_root/index.theme"
  for size in 16 24 32 48 64 96; do
    install -d -m 0755 "$theme_root/${size}x${size}/apps"
  done

  while IFS= read -r icon_name; do
    [[ -n "$icon_name" ]] || continue
    if [[ "$icon_name" == tools.keymasq.keymasq ]]; then
      for size in 16 24 32 48 64 96; do
        rsvg-convert \
          --width "$size" --height "$size" --keep-aspect-ratio \
          --output "$theme_root/${size}x${size}/apps/$icon_name.png" \
          "$REPO_ROOT/assets/tools.keymasq.keymasq.svg"
      done
      continue
    fi

    source_name="$icon_name"
    case "$icon_name" in
      applications-games|edit-delete|input-gaming|input-keyboard|input-mouse|input-tablet|list-add|preferences-desktop-keyboard-shortcuts)
        source_name="$icon_name-symbolic"
        ;;
    esac
    case "$source_name" in
      keymasq-*-symbolic)
        source_svg="$package_assets/$source_name.svg"
        ;;
      *)
        source_svg="$(find /usr/share/icons/Adwaita -type f -name "$source_name.svg" -print -quit)"
        ;;
    esac
    [[ -f "$source_svg" ]] || {
      echo "no symbolic source found for AppImage GUI icon: $icon_name" >&2
      return 1
    }

    staged_svg="$source_root/$icon_name.svg"
    cp "$source_svg" "$staged_svg"
    for size in 16 24 32 48 64 96; do
      output_dir="$theme_root/${size}x${size}/apps"
      "$PYTHON_EXE" "$REPO_ROOT/packaging/appimage/encode-symbolic-icon.py" \
        "$staged_svg" "$output_dir/$icon_name.symbolic.png" "$size"
      [[ -f "$output_dir/$icon_name.symbolic.png" ]] || {
        echo "symbolic icon encoder did not produce $icon_name at ${size}x${size}" >&2
        return 1
      }
    done
  done < "$manifest"

  rsvg-convert \
    --width 660 --height 430 --keep-aspect-ratio \
    --output "$package_assets/gamepad.png" \
    "$package_assets/gamepad.svg"
  [[ -s "$package_assets/gamepad.png" ]] || {
    echo "failed to rasterize the AppImage gamepad artwork" >&2
    return 1
  }

  # Direct package search paths take precedence over the selected icon theme.
  # Remove AppImage-local SVG variants so every GUI icon uses the private PNG
  # payload and never depends on a host SVG loader.
  rm -f "$package_assets"/*-symbolic.svg "$package_assets/gamepad.svg"
  gtk-update-icon-cache --force "$theme_root"
}

copy_host_library() {
  local dep="$1"
  local dst

  [[ -f "$dep" ]] || return 0
  case "$dep" in
    "$APPDIR"/*)
      return 0
      ;;
  esac

  dst="$APPDIR/lib/$(basename "$dep")"
  if [[ ! -e "$dst" ]]; then
    cp -L "$dep" "$dst"
    chmod u+w "$dst"
  fi
}

bundle_elf_dependencies() {
  local deps_file="$BUILD_ROOT/elf-deps.txt"
  local copied
  local dep
  local file_type
  local root

  if ! command -v file >/dev/null 2>&1; then
    echo "file is required to identify AppImage ELF dependencies" >&2
    return 1
  fi

  for _ in 1 2 3; do
    : > "$deps_file"
    for root in "$@"; do
      [[ -d "$root" ]] || continue
      while IFS= read -r -d '' elf; do
        if ! file_type="$(file -Lb "$elf")"; then
          echo "failed to identify AppImage dependency candidate: $elf" >&2
          return 1
        fi
        [[ "$file_type" == ELF\ * ]] || continue
        ldd "$elf" 2>/dev/null | awk '
          $2 == "=>" && $3 ~ /^\// { print $3 }
          $1 ~ /^\// { print $1 }
        ' >> "$deps_file" || true
      done < <(
        find "$root" -type f \
          \( -name '*.so' -o -name '*.so.*' -o -perm /111 \) -print0
      )
    done

    sort -u "$deps_file" -o "$deps_file"
    copied=0
    while IFS= read -r dep; do
      [[ -f "$dep" ]] || continue
      if [[ ! -e "$APPDIR/lib/$(basename "$dep")" ]]; then
        copy_host_library "$dep"
        copied=1
      fi
    done < "$deps_file"

    [[ "$copied" = 1 ]] || break
  done
}

bundle_typelib_libraries() {
  local names_file="$BUILD_ROOT/typelib-libraries.txt"
  local name
  local dep
  local root

  [[ -d "$APPDIR/lib/girepository-1.0" ]] || return 0
  compgen -G "$APPDIR/lib/girepository-1.0/*.typelib" >/dev/null || return 0
  strings "$APPDIR"/lib/girepository-1.0/*.typelib \
    | tr ',' '\n' \
    | awk '/^lib.*\.so(\.|$)/ { print }' \
    | sort -u > "$names_file"

  while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    dep=
    for root in /usr/lib /usr/lib64 /lib /lib64; do
      [[ -d "$root" ]] || continue
      dep="$(find "$root" -maxdepth 1 \( -type f -o -type l \) -name "$name" -print -quit 2>/dev/null || true)"
      [[ -z "$dep" ]] || break
    done
    [[ -n "$dep" ]] || continue
    copy_host_library "$dep"
  done < "$names_file"
}

remove_bundled_graphics_drivers() {
  rm -rf \
    "$APPDIR/lib/dri" \
    "$APPDIR/lib/gbm" \
    "$APPDIR/share/drirc.d" \
    "$APPDIR/share/glvnd" \
    "$APPDIR/share/vulkan"
}

if [[ "$ARCH" != "x86_64" && "${KEYMASQ_APPIMAGE_ALLOW_EXPERIMENTAL_ARCH:-0}" != 1 ]]; then
  echo "Only x86_64 AppImages are enabled for now." >&2
  exit 1
fi

PYTHON_EXE="${KEYMASQ_APPIMAGE_PYTHON:-$(resolve_bundle_python)}"
version="$(cd "$REPO_ROOT" && resolve_version)"

require_python_modules

if [[ -d "$BUILD_ROOT" ]]; then
  chmod -R u+w "$BUILD_ROOT" 2>/dev/null || true
fi
rm -rf "$BUILD_ROOT"
mkdir -p "$WORKDIR" "$APPDIR/bin" "$APPDIR/share/keymasq/appimage" "$APPIMAGE_OUT" "$DIST_DIR"
quick_sharun="$(resolve_quick_sharun)"
prepare_verified_appimage_inputs

copy_source_tree
"$PYTHON_EXE" -m build --wheel --no-isolation --outdir "$WORKDIR/wheel" "$WORKDIR/source"
"$PYTHON_EXE" -m installer --destdir "$STAGING" "$WORKDIR"/wheel/*.whl
install_runtime_files

export APPDIR
export OUTPATH="$APPIMAGE_OUT"
export DESKTOP="$REPO_ROOT/packaging/appimage/assets/keymasq.desktop"
export ICON="$REPO_ROOT/assets/tools.keymasq.keymasq.svg"
export MAIN_BIN=keymasq
export DEPLOY_PYTHON=1
export DEPLOY_GTK=1
export DEPLOY_GDK=1
export GTK_DIR=gtk-4.0
export DEPLOY_OPENGL=0
export DEPLOY_VULKAN=0
export ALWAYS_SOFTWARE="${KEYMASQ_APPIMAGE_ALWAYS_SOFTWARE:-${ALWAYS_SOFTWARE:-0}}"
export VERSION="$version"
export STRACE_MODE="${STRACE_MODE:-0}"
export ADD_HOOKS=
export GTK_CLASS_FIX=0
export OPTIMIZE_LAUNCH=0
export PATH_MAPPING=

"$quick_sharun" \
  "$APPDIR/bin/keymasq" \
  "$APPDIR/bin/keymasqd" \
  "$APPDIR/bin/keymasq-session" \
  "$APPDIR/bin/keymasq-record" \
  "$PYTHON_EXE" \
  "$(command -v openssl)" \
  "$(command -v slurp)" \
  "$(command -v waypipe)"

chmod -R u+w "$APPDIR"
remove_generated_hardcoded_path_mapping
install_brotway_runtime
verify_brotway_glibc_compatibility
ensure_bundled_python_command

python_lib_dir="$(find_bundled_python_lib_dir)"
if [[ -z "$python_lib_dir" ]]; then
  echo "failed to locate bundled Python lib directory in $APPDIR/lib" >&2
  exit 1
fi
copy_python_packages "$python_lib_dir"
install_appimage_icons "$python_lib_dir"
bundle_typelib_libraries
bundle_elf_dependencies "$python_lib_dir" "$APPDIR/lib" "$APPDIR/bin"
remove_bundled_graphics_drivers

# The installed runtime is executed by unprivileged services even though the
# AppImage is assembled as root in CI. Preserve executable files and make the
# complete read-only payload traversable and readable by those users.
chmod -R a+rX "$APPDIR"

"$quick_sharun" --make-appimage

desired="$DIST_DIR/Keymasq-${version}-${ARCH}.AppImage"
produced_count="$(find "$APPIMAGE_OUT" -maxdepth 1 -type f -name '*.AppImage' | wc -l)"
if [[ "$produced_count" -ne 1 ]]; then
  echo "expected exactly one AppImage in $APPIMAGE_OUT, found $produced_count" >&2
  find "$APPIMAGE_OUT" -maxdepth 1 -type f -name '*.AppImage' -print >&2
  exit 1
fi
produced="$(find "$APPIMAGE_OUT" -maxdepth 1 -type f -name '*.AppImage' -print -quit)"
install -Dm755 "$produced" "$desired"
ls -l "$desired"
