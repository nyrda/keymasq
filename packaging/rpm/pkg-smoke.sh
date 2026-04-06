#!/usr/bin/env bash
set -euo pipefail

assert_file() {
    local path="$1"
    test -e "$path" || {
        echo "missing required path: $path" >&2
        exit 1
    }
}

assert_cmd() {
    local name="$1"
    shift
    local output=""
    output="$("$@" 2>&1)" || {
        echo "command failed: $name" >&2
        if [[ -n "$output" ]]; then
            printf '%s\n' "$output" >&2
        fi
        exit 1
    }
}

assert_file /usr/bin/keyforge
assert_file /usr/bin/keyforged
assert_file /usr/bin/keyforge-session
assert_file /usr/bin/keyforge-record
assert_file /usr/lib/systemd/system/keyforged.service
assert_file /usr/lib/systemd/user/keyforge-session.service
assert_file /usr/lib/udev/rules.d/91-keyforge-acl.rules
assert_file /usr/lib/sysusers.d/keyforge.conf
assert_file /usr/lib/tmpfiles.d/keyforge.conf
assert_file /usr/share/polkit-1/actions/com.keyforge.record-macro.policy
assert_file /usr/share/applications/keyforge.desktop
assert_file /usr/share/metainfo/keyforge.metainfo.xml
assert_file /usr/share/icons/hicolor/scalable/apps/keyforge.svg
for size in 16 22 24 32 48 64 128 256 512; do
    assert_file "/usr/share/icons/hicolor/${size}x${size}/apps/keyforge.png"
done
assert_file /usr/share/gnome-shell/extensions/keyforge-bridge@keyforge/metadata.json
assert_file /etc/keyforge/security.toml

assert_cmd "keyforge --help" keyforge --help
assert_cmd "keyforged --help" keyforged --help
assert_cmd "keyforge-session --help" keyforge-session --help
assert_cmd "keyforge-record --help" keyforge-record --help
assert_cmd "python import" python3 -c "import keyforge, keyforge.common.models"
assert_cmd "python package css" python3 -c "from importlib import resources; assert resources.files('keyforge').joinpath('gui/style.css').is_file()"
assert_cmd "python package gui assets" python3 -c "from importlib import resources; gui = resources.files('keyforge').joinpath('gui'); assert gui.joinpath('assets', 'gamepad.svg').is_file(); assert gui.joinpath('assets', 'keyforge-keyboard-symbolic.svg').is_file(); assert gui.joinpath('assets', 'keyforge-mouse-symbolic.svg').is_file(); assert gui.joinpath('assets', 'keyforge-combos-symbolic.svg').is_file()"

if command -v systemd-sysusers >/dev/null 2>&1; then
    systemd-sysusers /usr/lib/sysusers.d/keyforge.conf >/dev/null 2>&1 || true
fi

if command -v systemd-tmpfiles >/dev/null 2>&1; then
    systemd-tmpfiles --create /usr/lib/tmpfiles.d/keyforge.conf >/dev/null 2>&1 || true
fi

assert_cmd "keyforge user exists" id keyforge
assert_file /run/keyforge
assert_file /var/lib/keyforge
