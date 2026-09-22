#!/usr/bin/env bash
# Destructive package lifecycle test. Use a disposable or snapshotted VM.
# Run from a repository checkout inside the guest, with two increasing package
# revisions that both support masking:
# sudo bash packaging/masking/run-guest.sh debian /var/tmp/baseline.deb /var/tmp/candidate.deb
# Formats: debian, fedora, opensuse, pacman. Use matching package files.
# Requires systemd, Python 3, getfacl, and the uhid, uinput, joydev kernel modules.
# The account keymasq-masktest and UID 1999 must be unused.
# Checks install, active-mask upgrade/removal, and reinstall. Removal must restore
# physical input before reboot or device recreation. Success leaves the candidate
# installed and test files/account for inspection; failure preserves its state.
# Save logs before rolling back the VM snapshot.
set -euo pipefail

if [[ $# -ne 3 ]]; then
    echo 'usage: run-guest.sh debian|fedora|opensuse|pacman BASELINE_PACKAGE CANDIDATE_PACKAGE' >&2
    exit 2
fi
[[ $EUID -eq 0 ]]
systemd-detect-virt --vm >/dev/null
format=$1
baseline=$(realpath "$2")
candidate=$(realpath "$3")
test -f "$baseline"
test -f "$candidate"
if getent passwd keymasq-masktest >/dev/null || getent passwd 1999 >/dev/null; then
    echo 'Test account keymasq-masktest and UID 1999 must be unused' >&2
    exit 1
fi
source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install_package() {
    case "$format" in
        debian) DEBIAN_FRONTEND=noninteractive apt-get install -y "$1" ;;
        fedora) dnf install -y --nogpgcheck "$1" ;;
        opensuse) zypper --non-interactive --no-gpg-checks install --allow-unsigned-rpm "$1" ;;
        pacman) pacman -U --noconfirm "$1" ;;
        *) echo "Unsupported format: $format" >&2; exit 2 ;;
    esac
}

version() {
    case "$format" in
        debian) dpkg-query -W -f='${Version}\n' keymasq ;;
        fedora|opensuse) rpm -q keymasq ;;
        pacman) pacman -Q keymasq ;;
    esac
}

setup() { bash "$source_dir/setup-guest.sh" "$@"; }
check() { setup check "$1"; }

install_package "$baseline"
baseline_version=$(version)
setup prepare
check ready
check baseline
keymasq-record unlock-runtime --uid 1999 --ttl 120
check mask
old_pid=$(systemctl show keymasqd.service -p MainPID --value)

install_package "$candidate"
candidate_version=$(version)
test "$candidate_version" != "$baseline_version"
check ready
new_pid=$(systemctl show keymasqd.service -p MainPID --value)
test "$new_pid" != 0
test "$new_pid" != "$old_pid"
check masked
printf 'PASS upgrade: %s -> %s; daemon PID %s -> %s\n' \
    "$baseline_version" "$candidate_version" "$old_pid" "$new_pid"

# Do not stop the daemon or call recovery here. The real removal hook must do it.
case "$format" in
    debian) DEBIAN_FRONTEND=noninteractive apt-get remove -y keymasq ;;
    fedora) dnf remove -y --setopt=clean_requirements_on_remove=False keymasq ;;
    opensuse) zypper --non-interactive remove keymasq ;;
    pacman) pacman -R --noconfirm keymasq ;;
esac
udevadm settle
# Must pass in the same boot, while the same UHID devices still exist.
check uninstalled
echo 'PASS uninstall: masking removed and fresh physical input restored before reboot'

# Also test a fresh install after removal. Saved user policy should still work.
install_package "$candidate"
setup resume
check ready
check masked
setup cleanup
echo 'PASS reinstall and cleanup: saved mask reapplied, then explicitly disabled'
