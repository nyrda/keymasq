# Troubleshooting

This guide covers common runtime issues, log inspection, and temporary or
persistent debug logging for Keymasq.

## Service status and logs

Check the current service state first:

```bash
systemctl status keymasqd
systemctl --user status keymasq-session
```

Check the Keymasq runtime state from the user session:

```bash
keymasq status
keymasq --json status
```

Follow live logs:

```bash
journalctl -u keymasqd -f
journalctl --user -u keymasq-session -f
```

View recent logs without following:

```bash
journalctl -u keymasqd -n 200
journalctl --user -u keymasq-session -n 200
```

## Verbose logging

Both services support `-v` and `-vv`. `keymasqd` also accepts `-vvv`.

- `keymasqd -v`: more detailed daemon logging, including command flow and
  runtime state changes.
- `keymasqd -vv`: trace-level daemon logging. Use this when debugging active
  input processing or event-heavy problems. This level can expose every key or
  button event seen by the daemon, so treat the resulting logs as sensitive.
- `keymasqd -vvv`: also logs every raw hardware event before remapping, except
  pointer motion. Use it to check what a device actually reports.
- `keymasq-session -v`: more detailed session and compositor logging,
  including daemon event flow.

## Inspect a device

Use the Device Inspector from a device tab when you need to see the final
resolved mapping and the raw events coming from that device. Enable suppression
inside the inspector to test inputs without emitting remapped output. Press
Escape on any grabbed keyboard to turn suppression off.

The raw event **Move** filter includes mouse movement and absolute axes from
motion interfaces, including gyro and accelerometer events. It is off by default.
Use **Axes** for regular stick, trigger, and wheel events. Hidden motion events
do not displace regular axes from their history, and copying events respects the
selected filters. The live motion preview updates regardless of these filters.

Unknown raw axis events appear in the event stream. Add the relevant event names
and codes to the hardware setup, then reopen the inspector to see them in the
configured axes viewer.

## Run services manually with verbosity

For short debugging sessions, stop the systemd service and run the process
directly in a terminal.

System daemon:

```bash
sudo systemctl stop keymasqd
sudo -u keymasq keymasqd -v
```

Trace logging:

```bash
sudo -u keymasq keymasqd -vv
```

User session service:

```bash
systemctl --user stop keymasq-session
keymasq-session -v
```

When finished, restart the services normally:

```bash
sudo systemctl start keymasqd
systemctl --user start keymasq-session
```

If you used `keymasqd -vv`, consider clearing old Keymasq journal entries
after disabling trace logging, especially if sensitive input events may have
been logged:

```bash
sudo journalctl -u keymasqd --rotate --vacuum-time=1s
```

## Persist verbose flags with systemd overrides

Use `systemctl edit` so local debug flags survive service restarts without
modifying packaged unit files.

### keymasqd

```bash
sudo systemctl edit keymasqd
```

Add:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/keymasqd -v
```

The blank `ExecStart=` line clears the default command so the next line
replaces it.

For trace logging:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/keymasqd -vv
```

Then reload and restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart keymasqd
```

### keymasq-session

```bash
systemctl --user edit keymasq-session
```

Add:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/keymasq-session -v
```

Then reload and restart:

```bash
systemctl --user daemon-reload
systemctl --user restart keymasq-session
```

To remove the override later:

```bash
sudo systemctl revert keymasqd
systemctl --user revert keymasq-session
```

After removing a `-vv` override from `keymasqd`, you can clear old trace logs:

```bash
sudo journalctl -u keymasqd --rotate --vacuum-time=1s
```

## Common problems

### Services not running

If `keymasqd` or `keymasq-session` stops or crashes, your input devices continue
working normally. Keymasq only intercepts input when both services are running
and a profile is active. No remapping means passthrough, so your keyboard and
mouse behave as if Keymasq were not installed.

### `uinput` or input-device access problems

Symptoms:

- `keymasqd` fails to start
- remaps do not activate
- logs mention permission errors for `/dev/uinput` or `/dev/input/event*`
- device discovery or capture says no devices/interfaces were found even though
  the hardware is connected

Checks:

```bash
systemctl status keymasqd
journalctl -u keymasqd -n 100
ls -l /dev/uinput
```

What to verify:

- the `keymasqd` service is running as the `keymasq` user
- udev rules were installed
- the service has permission to access `/dev/uinput` and the input event devices
- no other input remapping tool has already grabbed the device, because only
  one program can exclusively hold a device at a time

### A device does not appear in hardware setup

The **+** dialog lists input devices the daemon can open. It leaves out:

- devices that already have a hardware config. Open that device's tab and use
  **Hardware Settings** to add more of its event devices
- Keymasq's own virtual keyboards, mice, and gamepads
- event devices that report no vendor and product ID
- touchpads, unless **Show raw evdev devices** is checked

If the list is empty or the dialog reports a permission error, see
[`uinput` or input-device access problems](#uinput-or-input-device-access-problems).
While Steam holds the Steam Deck controller, the kernel driver removes its
input devices. Use [hardware masking](hardware-masking.md) for it.

### A game or Steam ignores the remap

Symptoms:

- a controller remap works on the desktop but not in a game
- a game sees both the physical controller and the remapped one, or doubled
  input

Keymasq hides a grabbed controller's evdev and joystick nodes. Steam and games
that use SDL's HID drivers can still read the physical controller through
`hidraw`, which bypasses the remap. Turn on
[hardware masking](hardware-masking.md) for that controller, so only Keymasq
can open it.

### Native gyro reports permission denied

Native motion reads `/dev/hidrawN` as the daemon user `keymasq`. Access granted
only to your desktop user is insufficient. Check the node named in the daemon
log with `getfacl /dev/hidrawN`. Hidraw devices should have a
`user:keymasq:r--` entry. Install the updated package rules, or rebuild the
NixOS configuration with the updated Keymasq module/package, then restart the
daemon. The startup hook reapplies native ACLs to connected controllers.
Reconnecting also applies the rule. A manual ACL is temporary and disappears
when the device is recreated.

### Steam Deck touch inputs do nothing

Steam Deck touch loads a small program into the kernel through a
`keymasq-hardware@<request-id>.service` job. When it fails, the daemon log
shows `Device read error on /dev/keymasq-sources/steam-deck-touch/...` with the
reason. Check that the kernel is Linux 6.11 or later with `CONFIG_HID_BPF`
(Debian 13's stable kernel and Ubuntu 24.04's GA kernel lack it), and that
`systemctl cat keymasq-hardware@.service` lists `CAP_BPF CAP_PERFMON` in
`CapabilityBoundingSet`. `journalctl -u 'keymasq-hardware@*'` shows the job.
If Steam holds the controller, the kernel driver removes its input devices and
the touch source cannot pair with it; use hardware masking for the Steam Deck.

### Source hiding jobs fail

Hiding or restoring a grabbed gamepad source runs `udevadm trigger` as a
bounded `keymasq-hardware@<request-id>.service` root job that the daemon
starts (see [security.md](security.md)). `keymasqd` itself holds no
capabilities. The job needs the `keymasq-hardware@.service` template and the
`49-keymasq-hardware.rules` Polkit rule that lets the `keymasq` user start it.
Both ship with every package and with the NixOS module.

The symptoms are that grabbed gamepads stay visible to games (or stay hidden
after release), and the daemon log shows `udev trigger job failed ...` with a
hint pointing at this section, while remapping, macros, and grabbing work
normally.

Checks:

```bash
systemctl cat keymasq-hardware@.service
journalctl -u 'keymasq-hardware@*' -n 50
journalctl -u polkit -n 50
systemctl show keymasqd -p CapabilityBoundingSet -p DevicePolicy
```

A missing template unit or a Polkit refusal of
`org.freedesktop.systemd1.manage-units` for the `keymasq` user means the
package files were not installed or a local override removed them. The daemon's
`CapabilityBoundingSet` should be empty and `DevicePolicy` should be `closed`.
Do not add capabilities in a drop-in override, because no Keymasq feature needs
them. Each job triggers udev and then waits for a bounded `udevadm settle`.
If udev does not settle, recovery keeps its journal and package removal is
refused until recovery succeeds.

### Daemon ownership conflicts

`keymasqd` accepts exactly one `keymasq-session` connection at a time. The
first allowed session connection becomes the daemon owner, and the daemon
rejects every later client until the owner disconnects. See the daemon
single-owner model in [security.md](security.md).

Symptoms:

- `keymasq-session` logs show it connecting to `keymasqd` and immediately
  disconnecting, in a retry loop
- the GUI reports no daemon connection even though `keymasqd` is running
- `keymasqd` logs show `Denied client ... owner already held by ...`

Checks:

```bash
journalctl -u keymasqd -n 100 | grep -i "owner"
systemctl --user status keymasq-session
```

The daemon logs every ownership transition with `uid`, `pid`, and
`connection`. The denial line names both the rejected client and the current
owner, so the owner's `pid` tells you exactly which process is holding the
daemon:

```bash
ps -o user,pid,cmd -p <owner_pid>
```

Common causes:

**Fast user switching.** The first user's `keymasq-session` keeps daemon
ownership while their session is still alive in the background, so the daemon
rejects the second user's broker, which retries until the first user logs out
fully. This is the intended single-seat behavior. Switching users without
logging out does not hand over the daemon.

**Stale session process.** A leftover `keymasq-session` from a previous
desktop session (crashed logout, lingering user services, a manually started
`dev-session.sh` run) still holds ownership. Identify it with the owner `pid`
from the denial log line, then stop it:

```bash
systemctl --user stop keymasq-session   # as the user owning the stale process
# or, for a manually started process:
kill <owner_pid>
```

The daemon releases devices and frees ownership on that disconnect, and your
current session's broker reconnects automatically within its retry backoff
(at most 30 seconds).

**Repeated systemd restarts.** If `keymasqd` restarts, every session broker
disconnects and reconnects with backoff, and the first one back claims
ownership. If `keymasq-session` restarts repeatedly (crash loop), ownership
changes hands with each restart. Check `journalctl --user -u keymasq-session`
for the underlying crash instead of treating the ownership messages as the
fault. Paired
claim/release lines with increasing `connection` numbers are the normal trace
of restarts, not a conflict.

A short passthrough window after an owner disconnect is expected. The daemon
ends captures, discards pending recordings, and releases all grabbed devices
before the next owner can claim. Remapping resumes
when the session reconnects and reapplies profiles.

### Duplicate hardware cannot be identified reliably

Some devices do not expose enough stable identity data for Linux to distinguish
two physical units of the same model. This usually happens when both devices
report the same USB vendor ID, product ID, name, and serial number. Keymasq can
keep separate numbered hardware IDs such as `045e:02a1` and `045e:02a1@2`, but
those IDs are also profile/config keys. Changing a hardware ID requires the
matching profile device layer to use the same ID.

Symptoms:

- two identical devices appear as one configurable device
- mappings swap between identical receivers or devices after reconnecting
- `/dev/input/by-id/` links look identical except for interface suffixes, or the
  serial number is missing or all zeroes

First inspect the kernel-provided paths:

```bash
ls -l /dev/input/by-id/
ls -l /dev/input/by-path/
```

Prefer `/dev/input/by-id/...` when it uniquely identifies the physical device.
If `by-id` cannot distinguish the devices, manually use `/dev/input/by-path/...`
instead. `by-path` is stable across reboots as long as the device stays in the
same USB or PCI path. If you move the receiver or device to another port, the
`by-path` link changes and you must update the config again.

If Linux does not expose a `/dev/input/by-id/...` link at all, Product detection
uses a logical path such as `keymasq:2dc8:3106` instead of the unstable
`/dev/input/eventN` node. This is not a real filesystem path. At runtime,
`keymasqd` resolves it by matching live evdev devices with the configured
vendor/product IDs and interface metadata such as type, `phys`, and
capabilities.

For model-matched gamepads, IDs such as `045e:02a1`, `045e:02a1@2`, and
`045e:02a1@8` are distinct profile/config keys, not physical slot selectors. At
runtime, each active config grabs the first unclaimed matching controller. Extra
matching controllers that are not named by an active profile remain ungrabbed
and visible to the system. The number is still not a serial number, and it does
not promise a specific USB receiver slot.

Edit the affected hardware file:

```bash
ls ~/.config/keymasq/hardware/
$EDITOR ~/.config/keymasq/hardware/<hardware_id>.toml
```

Change the `path` field inside each `[[hardware.evdev.devices]]` entry from a
bad or ambiguous `by-id` path to the matching `by-path` link:

```toml
[[hardware.evdev.devices]]
path = "/dev/input/by-path/pci-0000:00:14.0-usb-0:3:1.0-event-kbd"
type = "keyboard"
id = "kbd"
```

For multi-interface devices, update every relevant entry in the hardware file
and keep the existing `id` values unchanged. Profile mappings refer to those
`id` values and to the hardware ID, not to the path string.

After editing, `keymasq-session` should reload the hardware configuration
automatically. If the edited TOML has a syntax or load error, Keymasq keeps the
previous active configuration, logs the error, and shows a desktop notification.
Fix the file and save it again to retry the reload. You can also force a reload
without restarting services:

```bash
systemctl --user kill --signal=HUP keymasq-session
```

SIGHUP reloads use a 500 ms debounce. Additional SIGHUP requests received while
the reload is pending or running are dropped instead of queued.

### `keymasq-session` user service does not start

Symptoms:

- GUI opens but shows no active session state
- profile activation does not react to window changes
- `systemctl --user status keymasq-session` shows failures

Checks:

```bash
systemctl --user status keymasq-session
journalctl --user -u keymasq-session -n 100
```

If the user service was installed or changed manually, reload it:

```bash
systemctl --user daemon-reload
systemctl --user restart keymasq-session
```

### Macro recording does not start

Symptoms:

- the record controls in Macro Manager are disabled
- a recording trigger shows a "Macro recording disabled" notification
- `keymasq status` prints `macro recording: disabled by policy`

Checks:

```bash
keymasq status
grep -A3 recording_guard /etc/keymasq/security.toml
```

What to verify:

- `[recording_guard] macro_recording_allowed` is `true` or absent in
  `/etc/keymasq/security.toml`
- `keymasqd` and `keymasq-session` were restarted after the policy changed

Warnings about ignored `unlock_required` or `macro_edit_requires_unlock` keys
are harmless. Keymasq no longer uses them, and you can delete them.

### GNOME bridge problems

Symptoms:

- GNOME session is detected but active window tracking does not work
- logs mention a missing or disconnected bridge
- the bridge is still not detected immediately after installing or enabling the
  extension
- Keymasq shows a banner telling you to log out and back in to reload the
  updated GNOME bridge

Checks:

```bash
gnome-extensions info gnome-bridge@keymasq.tools
journalctl --user -u keymasq-session -n 100
```

Important:

- after installing the Keymasq package into an already running GNOME session,
  log out and back in before enabling the GNOME Shell bridge extension
- if `gnome-extensions enable gnome-bridge@keymasq.tools` says the extension does
  not exist, GNOME Shell has usually not rescanned extensions yet, so log out
  and back in, then run the enable command again
- restarting `keymasq-session` alone is not always enough if GNOME Shell has
  not reloaded the extension into the current session yet

See [gnome.md](gnome.md) for the bridge installation and verification
steps.

### Unsupported or partially supported compositor setup

Symptoms:

- window-based profile activation does not work
- compositor-specific actions are unavailable
- session logs show missing protocol or listener errors

Checks:

```bash
keymasq status
journalctl --user -u keymasq-session -n 100
```

`keymasq status` names the detected compositor and marks it `[unsupported]`
when Keymasq has no integration for it. The `listener` line shows whether
window tracking is active.

Typical causes:

- generic Wayland compositor does not expose
  `zwlr_foreign_toplevel_manager_v1`
- GNOME bridge is not enabled
- compositor-specific integration is missing from the current session

## When collecting a bug report

Include:

- distro and version
- desktop environment or compositor
- package install or source install
- `systemctl status keymasqd`
- `systemctl --user status keymasq-session`
- relevant `journalctl` output
- whether the issue reproduces with `-v` or `-vv`
