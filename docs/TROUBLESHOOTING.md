# Troubleshooting

This guide covers common runtime issues, log inspection, and temporary or
persistent debug logging for Keymasq.

## Service status and logs

Check the current service state first:

```bash
systemctl status keymasqd
systemctl --user status keymasq-session
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

### `uinput` or input-device access problems

Symptoms:

- `keymasqd` fails to start
- remaps do not activate
- logs mention permission errors for `/dev/uinput` or `/dev/input/event*`

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

### Polkit or recording unlock problems

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
gnome-extensions info keymasq-bridge@nyrda
journalctl --user -u keymasq-session -n 100
```

Important:

- after installing the Keymasq package into an already running GNOME session,
  log out and back in before enabling the GNOME Shell bridge extension
- if `gnome-extensions enable keymasq-bridge@nyrda` says the extension does
  not exist, GNOME Shell has usually not rescanned extensions yet; log out and
  back in, then run the enable command again
- restarting `keymasq-session` alone is not always enough if GNOME Shell has
  not reloaded the extension into the current session yet

See [docs/GNOME.md](docs/GNOME.md) for the bridge installation and verification
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
