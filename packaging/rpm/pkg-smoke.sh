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

assert_file /usr/bin/keymasq
assert_file /usr/bin/keymasqd
assert_file /usr/bin/keymasq-session
assert_file /usr/bin/keymasq-record
assert_file /usr/lib/systemd/system/keymasqd.service
assert_file /usr/lib/systemd/user/keymasq-session.service
assert_file /usr/lib/udev/rules.d/91-keymasq-acl.rules
assert_file /usr/lib/sysusers.d/keymasq.conf
assert_file /usr/lib/tmpfiles.d/keymasq.conf
assert_file /usr/share/polkit-1/actions/com.keymasq.record-macro.policy
assert_file /usr/share/applications/tools.keymasq.keymasq.desktop
assert_file /usr/share/metainfo/tools.keymasq.keymasq.metainfo.xml
assert_file /usr/share/icons/hicolor/scalable/apps/tools.keymasq.keymasq.svg
for size in 16 22 24 32 48 64 128 256 512; do
    assert_file "/usr/share/icons/hicolor/${size}x${size}/apps/tools.keymasq.keymasq.png"
done
assert_file /usr/share/gnome-shell/extensions/keymasq-bridge@nyrda/metadata.json
assert_file /etc/keymasq/security.toml

assert_cmd "keymasq --help" keymasq --help
assert_cmd "keymasqd --help" keymasqd --help
assert_cmd "keymasq-session --help" keymasq-session --help
assert_cmd "keymasq-record --help" keymasq-record --help
assert_cmd "python import" python3 -c "import keymasq, keymasq.common.models"
assert_cmd "desktop identity assets aligned" python3 -c "import os; from configparser import ConfigParser; from xml.etree import ElementTree as ET; desktop_path = '/usr/share/applications/tools.keymasq.keymasq.desktop'; metainfo_path = '/usr/share/metainfo/tools.keymasq.keymasq.metainfo.xml'; desktop_id = os.path.basename(desktop_path); metainfo_id = os.path.basename(metainfo_path).removesuffix('.metainfo.xml'); app_id = desktop_id.removesuffix('.desktop'); parser = ConfigParser(interpolation=None); parser.read(desktop_path, encoding='utf-8'); entry = parser['Desktop Entry']; root = ET.parse(metainfo_path).getroot(); launchable = root.find(\"launchable[@type='desktop-id']\"); assert entry['Icon'] == app_id; assert root.findtext('id') == metainfo_id; assert launchable is not None and (launchable.text or '').strip() == desktop_id"
assert_cmd "python package css" python3 -c "from importlib import resources; assert resources.files('keymasq').joinpath('gui/style.css').is_file()"
assert_cmd "python package gui assets" python3 -c "from importlib import resources; gui = resources.files('keymasq').joinpath('gui'); assert gui.joinpath('assets', 'gamepad.svg').is_file(); assert gui.joinpath('assets', 'keymasq-keyboard-symbolic.svg').is_file(); assert gui.joinpath('assets', 'keymasq-mouse-symbolic.svg').is_file(); assert gui.joinpath('assets', 'keymasq-combos-symbolic.svg').is_file()"

if command -v systemd-sysusers >/dev/null 2>&1; then
    systemd-sysusers /usr/lib/sysusers.d/keymasq.conf >/dev/null 2>&1 || true
fi

if command -v systemd-tmpfiles >/dev/null 2>&1; then
    systemd-tmpfiles --create /usr/lib/tmpfiles.d/keymasq.conf >/dev/null 2>&1 || true
fi

assert_cmd "keymasq user exists" id keymasq
assert_file /run/keymasq
assert_file /var/lib/keymasq
