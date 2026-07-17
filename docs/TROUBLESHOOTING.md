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

Both services support `-v` and `-vv`.

- `keymasqd -v`: more detailed daemon logging, including command flow and
  runtime state changes.
- `keymasqd -vv`: trace-level daemon logging. Use this when debugging active
  input processing or event-heavy problems. This level can expose every key or
  button event seen by the daemon, so treat the resulting logs as sensitive.
- `keymasq-session -v`: more detailed session and compositor logging,
  including daemon event flow.

## Inspect a Device

Use the Device Inspector from a device tab when you need to see the final
resolved mapping and the raw events coming from that device. Enable suppression
inside the inspector to test inputs without emitting remapped output. Press
Escape on any grabbed keyboard to turn suppression off.

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
and a profile is active. No remapping means passthrough—your keyboard and mouse
behave as if Keymasq were not installed.

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
- no other input remapping tool has already grabbed the device — only one program can exclusively
  hold a device at a time

### Missing CAP_DAC_OVERRIDE capability

`keymasqd` needs the `CAP_DAC_OVERRIDE` capability for gamepad source
hide/restore and force-feedback passthrough (see
[SECURITY.md](SECURITY.md)). The shipped `keymasqd.service` grants it; a local
override or hand-written unit that drops `AmbientCapabilities` breaks exactly
those features while everything else keeps working.

Symptoms: grabbed gamepads stay visible to games (or stay hidden after
release), and logs show `udevadm trigger failed ... permission denied` with a
hint pointing at this section — while remapping, macros, and grabbing work
normally.

Checks:

```bash
systemctl show keymasqd -p AmbientCapabilities -p CapabilityBoundingSet
systemctl cat keymasqd   # look for drop-in overrides removing the capability
```

Both values should be `cap_dac_override`. If a drop-in override in
`/etc/systemd/system/keymasqd.service.d/` clears them, remove or fix the
override and run `systemctl daemon-reload && systemctl restart keymasqd`. Do
not widen the set beyond `CAP_DAC_OVERRIDE`; no Keymasq feature needs more.

### Daemon ownership conflicts

`keymasqd` accepts exactly one `keymasq-session` connection at a time. The
first allowed session connection becomes the daemon owner; every later client
is rejected until the owner disconnects. See the Daemon Single-Owner Model in
[SECURITY.md](SECURITY.md).

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
ownership while their session is still alive in the background, so the second
user's broker is rejected and retries until the first user logs out fully.
This is the intended single-seat behavior. Switching users without logging out
does not hand over the daemon.

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

**Repeated systemd restarts.** If `keymasqd` restarts, every session broker is
disconnected and reconnects with backoff; the first one back claims ownership.
If `keymasq-session` restarts repeatedly (crash loop), ownership churns with
it — check `journalctl --user -u keymasq-session` for the underlying crash
rather than treating the ownership messages as the fault. Paired
claim/release lines with increasing `connection` numbers are the normal trace
of restarts, not a conflict.

A short passthrough window after an owner disconnect is expected: the daemon
clears the runtime capture unlock, discards pending recordings, and releases
all grabbed devices before the next owner can claim, and remapping resumes
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
the reload is pending or running are dropped rather than queued.

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

### Touchpad does not appear in Add Device

Current behavior:

- touchpads are detected but intentionally hidden from the Add Device flow
- Keymasq does not support touchpad remapping yet, so the GUI will not offer a
  touchpad as addable hardware

### Polkit or capture unlock problems

Symptoms:

- recording or capture actions fail
- no polkit prompt appears when expected
- logs mention authorization or helper failures

Checks:

```bash
journalctl -u keymasqd -n 100
journalctl --user -u keymasq-session -n 100
```

What to verify:

- the polkit policy file is installed
- the desktop session has a working authentication agent
- the packaged or installed `keymasq-record` helper is present and executable

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
  not exist, GNOME Shell has usually not rescanned extensions yet; log out and
  back in, then run the enable command again
- restarting `keymasq-session` alone is not always enough if GNOME Shell has
  not reloaded the extension into the current session yet

See [GNOME.md](GNOME.md) for the bridge installation and verification
steps.

### Unsupported or partially supported compositor setup

Symptoms:

- window-based profile activation does not work
- compositor-specific actions are unavailable
- session logs show missing protocol or listener errors

Checks:

```bash
journalctl --user -u keymasq-session -n 100
keymasq profiles list
```

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
