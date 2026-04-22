# Bazzite VM Compatibility Test Guide

Use this guide to answer one specific question: does the Fedora Keymasq RPM
behave cleanly when layered onto Bazzite with `rpm-ostree`?

This is not a generic feature test plan. It focuses on the parts most likely to
break on an Atomic host:

- package layering and rebooted deployments
- package assets landing in the right immutable/writable locations
- `sysusers`, `tmpfiles`, `udev`, and systemd integration
- real input-device access after boot
- service persistence across reboot and upgrade

## What counts as "compatible"

Treat Keymasq as compatible with Bazzite if all of the following are true:

- `rpm-ostree` can layer the package without dependency or scriptlet failures
- the new deployment boots cleanly
- the packaged files appear in the expected `/usr` and `/etc` locations
- the `keymasq` system user and runtime directories exist after boot
- `keymasqd` and `keymasq-session` can be enabled and started normally
- the daemon can access `/dev/uinput` and a real input device
- the GUI/CLI work and a real remap works on a passed-through device
- a repo-layered install does not break a normal `rpm-ostree upgrade`
- uninstalling the package returns the VM to a clean, bootable state

Do not treat these as failures by themselves:

- needing a reboot after install or uninstall
- having to enable the services manually
- a local RPM not receiving automatic updates later
- Bazzite warning that layered packages can complicate rebases in general

## Minimum test matrix

Run at least:

- `bazzite` KDE desktop image

Recommended:

- `bazzite-gnome` for GNOME Shell bridge verification

Optional:

- a `-deck` image if you care about Steam Gaming Mode specifically

## VM setup

Before you install anything:

- create a fresh Bazzite VM snapshot
- keep a console path open that does not depend on the remapped device
- use a passed-through USB keyboard, mouse, or gamepad for grab tests
- avoid testing exclusive grabs on the VM's only keyboard

Why this matters: packaging and service tests work fine in a plain VM, but real
input-remap validation is much more trustworthy with a physical USB device
passed through to the guest.

## Install paths to test

Test both of these separately.

### 1. Repository-backed layering

This is the best signal for a real Bazzite install because updates can come
from the configured repository later.

```bash
sudo tee /etc/yum.repos.d/keymasq.repo <<'EOF'
[keymasq]
name=Keymasq
baseurl=https://repo.keymasq.tools/fedora
enabled=1
gpgcheck=1
gpgkey=https://repo.keymasq.tools/gpg-key.asc
metadata_expire=1h
EOF

sudo rpm-ostree install keymasq
systemctl reboot
```

### 2. Local RPM layering

Use this to test the direct release artifact path.

```bash
sudo rpm --import https://repo.keymasq.tools/gpg-key.asc
sudo rpm-ostree install /var/home/$USER/Downloads/keymasq-*.fc*.rpm
systemctl reboot
```

Expected caveat: Bazzite documents that local RPM layering does not give that
package automatic updates later. That is expected behavior, not a Keymasq
compatibility bug.

## Baseline capture

Record these before the install:

```bash
rpm-ostree status
rpm -q keymasq || true
id keymasq || true
test -e /usr/bin/keymasq && echo "unexpected preinstalled binary"
```

Save the Bazzite image name shown by `rpm-ostree status`.

## Test checklist

### 1. Layering transaction and deployment

After the install and reboot:

```bash
rpm-ostree status
rpm -q keymasq
```

Pass if:

- `keymasq` is shown as layered
- the system boots into the new deployment
- there are no transaction, dependency, or scriptlet errors

### 2. Package payload

Check the main packaged assets:

```bash
test -x /usr/bin/keymasq
test -x /usr/bin/keymasqd
test -x /usr/bin/keymasq-session
test -x /usr/bin/keymasq-record
test -f /usr/lib/systemd/system/keymasqd.service
test -f /usr/lib/systemd/user/keymasq-session.service
test -f /usr/lib/udev/rules.d/91-keymasq-acl.rules
test -f /usr/lib/sysusers.d/keymasq.conf
test -f /usr/lib/tmpfiles.d/keymasq.conf
test -f /usr/share/polkit-1/actions/com.keymasq.record-macro.policy
test -f /etc/keymasq/security.toml
```

Pass if all expected files exist after the rebooted deployment comes up.

### 3. `sysusers` and `tmpfiles` behavior

Check that the system user and directories exist without manual rescue steps:

```bash
id keymasq
stat -c '%U:%G %a %n' /var/lib/keymasq
stat -c '%U:%G %a %n' /run/keymasq
```

Pass if:

- the `keymasq` user exists
- `/var/lib/keymasq` exists
- `/run/keymasq` exists

If any of these only appear after manually running `systemd-sysusers` or
`systemd-tmpfiles`, that is a real compatibility problem worth fixing.

### 4. Service enablement and persistence

Enable both services in the normal packaged way:

```bash
sudo systemctl enable --now keymasqd
systemctl --user enable --now keymasq-session
systemctl status keymasqd
systemctl --user status keymasq-session
```

Then reboot and verify again:

```bash
systemctl status keymasqd
systemctl --user status keymasq-session
```

Pass if:

- both services start without manual unit edits
- both services stay enabled across reboot
- the user service starts again after login

### 5. Udev and device-access checks

Validate that the daemon can access `uinput` and a real input device:

```bash
ls -l /dev/uinput
getfacl /dev/uinput
sudo journalctl -u keymasqd -n 100
```

If you passed through a USB input device, inspect at least one relevant event
node:

```bash
ls -l /dev/input/by-id
getfacl /dev/input/event*
```

Pass if:

- `keymasqd` starts cleanly
- the journal does not show permission-denied failures for `/dev/uinput` or
  `/dev/input/event*`
- the passed-through device can be opened and grabbed by the daemon

### 6. CLI and GUI smoke test

Run the normal user-facing entrypoints:

```bash
keymasq --help
keymasq status
keymasq --json status
keymasq
```

Pass if the CLI works, the GUI launches, and the GUI can talk to the running
session/daemon stack.

### 7. Real remap test

Use a secondary passed-through keyboard, mouse, or gamepad. Do not use the only
keyboard that controls the VM.

Suggested low-risk test:

1. Create a temporary profile in the GUI.
2. Add the passed-through device.
3. Map an otherwise harmless key or button on that device to a clearly visible
   output.
4. Enable the profile.
5. Confirm the remapped output appears in a text field or other obvious target.
6. Disable the profile and confirm raw input returns.

Pass if Keymasq can actually grab the device and transform live input inside
the Bazzite session.

### 8. Desktop-session integration

For KDE Plasma, confirm `keymasq-session` starts cleanly and stays connected to
the desktop session:

```bash
journalctl --user -u keymasq-session -n 100
```

For GNOME, also verify the packaged bridge extension is visible:

```bash
gnome-extensions info keymasq-bridge@nyrda
gnome-extensions enable keymasq-bridge@nyrda
systemctl --user restart keymasq-session
journalctl --user -u keymasq-session -n 100
```

Pass if:

- KDE session startup shows no compositor-session failures
- on GNOME, the extension is discoverable after the post-install reboot
- GNOME window tracking works after enabling the extension

### 9. Host upgrade behavior

Run this on the repository-backed install:

```bash
sudo rpm-ostree upgrade
systemctl reboot
rpm-ostree status
rpm -q keymasq
systemctl status keymasqd
systemctl --user status keymasq-session
```

Pass if:

- the host can still upgrade normally
- Keymasq remains layered after the upgrade
- services still work after the next boot

If there is no newer Keymasq build available, a no-op host upgrade is still
useful. The question is whether layering Keymasq blocks normal Bazzite update
flow.

### 10. Uninstall behavior

Disable the services first, then remove the package:

```bash
sudo systemctl disable --now keymasqd
systemctl --user disable --now keymasq-session
sudo rpm-ostree uninstall keymasq
systemctl reboot
```

After reboot:

```bash
rpm-ostree status
rpm -q keymasq || true
systemctl status keymasqd || true
systemctl --user status keymasq-session || true
```

Pass if:

- the VM boots cleanly
- `keymasq` is no longer layered
- stale enabled services do not keep failing on every boot

Record what happens to `/etc/keymasq/security.toml`. Config retention is not
automatically a failure, but it should be noted.

## Likely failure signatures

If any of these happen, Keymasq does not currently "just work" on Bazzite:

- the install only succeeds after manual writes into `/usr`
- the first reboot comes up without the `keymasq` user or required directories
- `keymasqd` cannot access `uinput` or event devices until you manually rerun
  udev or ACL commands
- the user service never comes back after a normal login
- the package blocks ordinary `rpm-ostree upgrade` for reasons specific to
  Keymasq
- uninstall leaves a broken deployment or boot-time service failure loop

## Recommended notes template

Record results in this format for each VM:

- Bazzite image:
- Desktop session:
- Package source: repo or local RPM
- Keymasq version:
- Install transaction result:
- First boot result:
- `sysusers`/`tmpfiles` result:
- Service enable/start result:
- Device access result:
- Real remap result:
- Desktop integration result:
- Upgrade result:
- Uninstall result:
- Workarounds needed:

## References

- Bazzite package layering docs:
  https://docs.bazzite.gg/Installing_and_Managing_Software/rpm-ostree/
- rpm-ostree administrator handbook:
  https://coreos.github.io/rpm-ostree/administrator-handbook/
- Keymasq installation guide:
  [INSTALL.md](INSTALL.md)
- Keymasq troubleshooting:
  [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
- Keymasq GNOME setup:
  [GNOME.md](GNOME.md)
