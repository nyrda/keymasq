#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <AppDir>" >&2
  exit 2
fi

appdir="$1"
stock="$appdir/lib/libgtk-4.so.1"
overlay="$appdir/lib/gtk4-brotway/libgtk-4.so.1"

for path in "$stock" "$overlay"; do
  if [[ ! -f "$path" ]]; then
    echo "Brotway GTK ABI check input does not exist: $path" >&2
    exit 1
  fi
done
for command in nm readelf strings; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "$command is required to check the Brotway GTK ABI" >&2
    exit 1
  fi
done

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

exported_symbols() {
  LC_ALL=C nm -D --defined-only "$1" \
    | awk 'NF == 3 { sub(/@.*/, "", $3); print $3 }' \
    | LC_ALL=C sort -u
}

imported_symbols() {
  LC_ALL=C nm -D --undefined-only "$1" \
    | awk 'NF >= 2 { sub(/@.*/, "", $NF); print $NF }' \
    | LC_ALL=C sort -u
}

if ! exported_symbols "$stock" > "$work/stock"; then
  echo "failed to read exported symbols from $stock" >&2
  exit 1
fi
if ! exported_symbols "$overlay" > "$work/overlay"; then
  echo "failed to read exported symbols from $overlay" >&2
  exit 1
fi

consumers=0
required=0
unresolved=0
report=

check_consumer() {
  local label="$1"
  local imports="$2"
  local needed
  local missing

  needed="$(LC_ALL=C comm -12 "$imports" "$work/stock")"
  [[ -n "$needed" ]] || return 0
  consumers=$((consumers + 1))
  required=$((required + $(printf '%s\n' "$needed" | wc -l)))
  missing="$(printf '%s\n' "$needed" | LC_ALL=C comm -23 - "$work/overlay")"
  [[ -n "$missing" ]] || return 0
  unresolved=$((unresolved + $(printf '%s\n' "$missing" | wc -l)))
  report+="  $label:"$'\n'
  while IFS= read -r symbol; do
    report+="    $symbol"$'\n'
  done <<< "$missing"
}

if ! find -H "$appdir" -path "$appdir/lib/gtk4-brotway" -prune -o -type f \
  \( -name '*.so' -o -name '*.so.*' -o -perm /111 \) -print0 > "$work/candidates"; then
  echo "failed to list ELF candidates in $appdir" >&2
  exit 1
fi
while IFS= read -r -d '' elf; do
  [[ "$elf" -ef "$stock" ]] && continue
  magic=
  LC_ALL=C read -r -n 4 magic < "$elf" || true
  [[ "$magic" == $'\x7fELF' ]] || continue
  if ! dynamic="$(LC_ALL=C readelf --wide --dynamic "$elf")"; then
    echo "failed to read the dynamic section of $elf" >&2
    exit 1
  fi
  grep -qF 'Shared library: [libgtk-4.so.1]' <<< "$dynamic" || continue
  if ! imported_symbols "$elf" > "$work/imports"; then
    echo "failed to read imported symbols from $elf" >&2
    exit 1
  fi
  check_consumer "${elf#"$appdir"/}" "$work/imports"
done < <(sort -z "$work/candidates")

# GObject introspection resolves typelib entry points from libgtk-4.so.1 with dlsym.
for typelib in "$appdir"/lib/girepository-1.0/*.typelib; do
  [[ -f "$typelib" ]] || continue
  LC_ALL=C strings "$typelib" | tr ',' '\n' | LC_ALL=C sort -u > "$work/imports"
  grep -qxF 'libgtk-4.so.1' "$work/imports" || continue
  check_consumer "${typelib#"$appdir"/}" "$work/imports"
done

if [[ "$consumers" -eq 0 ]]; then
  echo "no bundled consumer of libgtk-4.so.1 was found in $appdir/lib" >&2
  exit 1
fi
if [[ "$unresolved" -ne 0 ]]; then
  echo "Brotway GTK overlay does not provide every libgtk-4 symbol the AppImage imports." >&2
  echo "bundled GTK: $stock" >&2
  echo "overlay GTK: $overlay" >&2
  echo "unresolved symbols per consumer:" >&2
  printf '%s' "$report" >&2
  echo "Publish a gtk-brotway overlay built from the bundled GTK release and update the pin in make-appimage.sh." >&2
  exit 1
fi

echo "Brotway GTK ABI: $consumers consumers make $required libgtk-4 symbol imports, all exported by the overlay"
