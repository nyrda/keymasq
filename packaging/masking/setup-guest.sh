#!/usr/bin/env bash
# Run only in a snapshotted test VM with Keymasq installed.
set -euo pipefail

[[ $EUID -eq 0 ]]
systemd-detect-virt --vm >/dev/null
source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
test_dir=/usr/local/lib/keymasq-package-test
state_dir=/var/lib/keymasq-package-test
test_user=keymasq-masktest
test_uid=1999

as_user() {
    runuser -u "$test_user" -- env \
        XDG_RUNTIME_DIR="/run/user/$test_uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$test_uid/bus" "$@"
}

test_policy() {
    cat > /etc/keymasq/security.toml <<EOF
daemon_allowed_uids = [$test_uid]
session_allowed_uids = [$test_uid]
EOF
}

start_session() {
    as_user systemctl --user daemon-reload
    if as_user systemctl --user is-failed --quiet keymasq-session.service; then
        as_user systemctl --user reset-failed keymasq-session.service
    fi
    as_user systemctl --user restart keymasq-session.service
}

case "${1:-}" in
    prepare)
        # Refuse to overwrite an existing account, home, or fixture installation.
        if getent passwd "$test_user" >/dev/null || getent passwd "$test_uid" >/dev/null; then
            echo "Test account $test_user and UID $test_uid must be unused" >&2
            exit 1
        fi
        [[ ! -e $state_dir && ! -e $test_dir ]]
        command -v getfacl >/dev/null
        test -f /usr/lib/systemd/system/keymasq-hardware@.service
        useradd -m -d "$state_dir" -s /bin/bash -u "$test_uid" -G input "$test_user"
        install -d -m 755 "$test_dir"
        install -m 644 "$source_dir/guest.py" "$test_dir/guest.py"
        fixture="$source_dir/uhid.py"
        if [[ ! -f $fixture ]]; then
            fixture="$source_dir/../../nix/daemon-session-integration-test/uhid.py"
        fi
        install -m 644 "$fixture" "$test_dir/uhid.py"
        cp -a /etc/keymasq/security.toml "$test_dir/security.original.toml"
        systemctl is-active keymasqd.service > "$test_dir/daemon.original-state" || true
        systemctl is-enabled keymasqd.service > "$test_dir/daemon.original-enablement" || true
        test_policy
        # Only the synthetic Bluetooth model used by this fixture.
        cat > /etc/udev/rules.d/70-keymasq-package-fixture.rules <<'EOF'
SUBSYSTEM=="hidraw", KERNELS=="0005:2DC8:6012.*", GROUP="input", MODE="0660", TAG+="uaccess"
EOF
        cat > /etc/systemd/system/keymasq-package-fixture.service <<EOF
[Unit]
Description=Keymasq package test controllers
Before=keymasqd.service
[Service]
ExecStart=/usr/bin/python3 $test_dir/guest.py serve
[Install]
WantedBy=multi-user.target
EOF
        modprobe uhid
        modprobe uinput
        modprobe joydev
        udevadm control --reload-rules
        systemctl daemon-reload
        systemctl start keymasq-package-fixture.service
        udevadm settle
        systemctl restart keymasqd.service
        loginctl enable-linger "$test_user"
        systemctl start "user@$test_uid.service"
        start_session
        ;;
    resume)
        # RPM removal saves the temporary test policy as .rpmsave. Reapply it
        # before restarting the reinstalled application.
        test_policy
        systemctl restart keymasqd.service
        start_session
        ;;
    check)
        as_user python3 "$test_dir/guest.py" "${2:?missing check command}"
        ;;
    cleanup)
        # First restore through the installed application to disable its saved mask.
        as_user python3 "$test_dir/guest.py" restore
        as_user systemctl --user stop keymasq-session.service
        systemctl stop keymasqd.service keymasq-package-fixture.service
        cp -a "$test_dir/security.original.toml" /etc/keymasq/security.toml
        rm /etc/udev/rules.d/70-keymasq-package-fixture.rules
        rm /etc/systemd/system/keymasq-package-fixture.service
        udevadm control --reload-rules
        systemctl daemon-reload
        case "$(cat "$test_dir/daemon.original-enablement")" in
            enabled) systemctl enable keymasqd.service ;;
            disabled) systemctl disable keymasqd.service ;;
        esac
        if [[ $(cat "$test_dir/daemon.original-state") == active ]]; then
            systemctl start keymasqd.service
        fi
        loginctl disable-linger "$test_user"
        loginctl terminate-user "$test_user"
        # Leave the account, baseline and fixture files for examination. Their
        # removal, along with disk rollback, is an explicit VM cleanup step.
        ;;
    *)
        echo 'usage: setup-guest.sh prepare|resume|check COMMAND|cleanup' >&2
        exit 2
        ;;
esac
