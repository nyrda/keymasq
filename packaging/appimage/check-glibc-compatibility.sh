#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <ELF consumer> <libc.so.6>" >&2
  exit 2
fi

consumer="$1"
libc="$2"

for path in "$consumer" "$libc"; do
  if [[ ! -f "$path" ]]; then
    echo "glibc compatibility input does not exist: $path" >&2
    exit 1
  fi
done
if ! command -v readelf >/dev/null 2>&1; then
  echo "readelf is required to check AppImage glibc compatibility" >&2
  exit 1
fi

if ! consumer_info="$(LC_ALL=C readelf --wide --version-info "$consumer")"; then
  echo "failed to read ELF version requirements from $consumer" >&2
  exit 1
fi
if ! libc_info="$(LC_ALL=C readelf --wide --version-info "$libc")"; then
  echo "failed to read ELF version definitions from $libc" >&2
  exit 1
fi

required_versions="$({
  printf '%s\n' "$consumer_info" | awk '
    /^Version needs section/ { in_needs = 1; next }
    /^Version definition section/ { in_needs = 0 }
    in_needs {
      for (field = 1; field < NF; field++) {
        if ($field == "Name:" && $(field + 1) ~ /^GLIBC_/) {
          print $(field + 1)
        }
      }
    }
  '
} | sort -u)"
provided_versions="$({
  printf '%s\n' "$libc_info" | awk '
    /^Version definition section/ { in_definitions = 1; next }
    /^Version needs section/ { in_definitions = 0 }
    in_definitions {
      for (field = 1; field < NF; field++) {
        if ($field == "Name:" && $(field + 1) ~ /^GLIBC_/) {
          print $(field + 1)
        }
      }
    }
  '
} | sort -u)"

if [[ -z "$required_versions" ]]; then
  echo "no GLIBC symbol requirements found in $consumer" >&2
  exit 1
fi
if [[ -z "$provided_versions" ]]; then
  echo "no GLIBC symbol definitions found in $libc" >&2
  exit 1
fi

missing_versions="$(comm -23 \
  <(printf '%s\n' "$required_versions") \
  <(printf '%s\n' "$provided_versions"))"
if [[ -n "$missing_versions" ]]; then
  echo "Brotway GTK is incompatible with the AppImage libc." >&2
  echo "required by: $consumer" >&2
  echo "provided by: $libc" >&2
  echo "missing GLIBC symbol versions:" >&2
  while IFS= read -r version; do
    printf '  %s\n' "$version" >&2
  done <<< "$missing_versions"
  exit 1
fi

requirement_count="$(printf '%s\n' "$required_versions" | wc -l)"
echo "Brotway glibc compatibility: $requirement_count required symbol versions are available"
