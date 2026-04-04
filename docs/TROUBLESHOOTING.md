# Troubleshooting

This guide covers common runtime issues, log inspection, and temporary or
persistent debug logging for Keyforge.

## Service status and logs

Check the current service state first:

```bash
systemctl status keyforged
systemctl --user status keyforge-session
```

Follow live logs:

```bash
journalctl -u keyforged -f
journalctl --user -u keyforge-session -f
```

View recent logs without following:

```bash
journalctl -u keyforged -n 200
journalctl --user -u keyforge-session -n 200
```

## Verbose logging

Both services support `-v` and `-vv`.

- `keyforged -v`: more detailed daemon logging, including command flow and
  runtime state changes.
- `keyforged -vv`: trace-level daemon logging. Use this when debugging active
  input processing or event-heavy problems. This level can expose every key or
  button event seen by the daemon, so treat the resulting logs as sensitive.
- `keyforge-session -v`: more detailed session and compositor logging,
  including daemon event flow.

## Run services manually with verbosity

For short debugging sessions, stop the systemd service and run the process
directly in a terminal.

System daemon:

```bash
sudo systemctl stop keyforged
sudo -u keyforge keyforged -v
```

Trace logging:

```bash
sudo -u keyforge keyforged -vv
```

User session service:

```bash
systemctl --user stop keyforge-session
keyforge-session -v
```

When finished, restart the services normally:

```bash
sudo systemctl start keyforged
systemctl --user start keyforge-session
```

If you used `keyforged -vv`, consider clearing old Keyforge journal entries
after disabling trace logging, especially if sensitive input events may have
been logged:

```bash
sudo journalctl -u keyforged --rotate --vacuum-time=1s
```

## Persist verbose flags with systemd overrides

Use `systemctl edit` so local debug flags survive service restarts without
modifying packaged unit files.

### keyforged

```bash
sudo systemctl edit keyforged
```

Add:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/keyforged -v
```

The blank `ExecStart=` line clears the default command so the next line
replaces it.

For trace logging:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/keyforged -vv
```

Then reload and restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart keyforged
```

### keyforge-session

```bash
systemctl --user edit keyforge-session
```

Add:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/keyforge-session -v
```

Then reload and restart:

```bash
systemctl --user daemon-reload
systemctl --user restart keyforge-session
```

To remove the override later:

```bash
sudo systemctl revert keyforged
systemctl --user revert keyforge-session
```

After removing a `-vv` override from `keyforged`, you can clear old trace logs:

```bash
sudo journalctl -u keyforged --rotate --vacuum-time=1s
```

## Common problems

### `uinput` or input-device access problems

Symptoms:

- `keyforged` fails to start
- remaps do not activate
- logs mention permission errors for `/dev/uinput` or `/dev/input/event*`

Checks:

```bash
systemctl status keyforged
journalctl -u keyforged -n 100
ls -l /dev/uinput
```

What to verify:

- the `keyforged` service is running as the `keyforge` user
- udev rules were installed
- the service has permission to access `/dev/uinput` and the input event devices
- no other input remapping tool has already grabbed the device — only one program can exclusively
  hold a device at a time

### `keyforge-session` user service does not start

Symptoms:

- GUI opens but shows no active session state
- profile activation does not react to window changes
- `systemctl --user status keyforge-session` shows failures

Checks:

```bash
systemctl --user status keyforge-session
journalctl --user -u keyforge-session -n 100
```

If the user service was installed or changed manually, reload it:

```bash
systemctl --user daemon-reload
systemctl --user restart keyforge-session
```

### Polkit or recording unlock problems

Symptoms:

- recording or capture actions fail
- no polkit prompt appears when expected
- logs mention authorization or helper failures

Checks:

```bash
journalctl -u keyforged -n 100
journalctl --user -u keyforge-session -n 100
```

What to verify:

- the polkit policy file is installed
- the desktop session has a working authentication agent
- the packaged or installed `keyforge-record` helper is present and executable

### GNOME bridge problems

Symptoms:

- GNOME session is detected but active window tracking does not work
- logs mention a missing or disconnected bridge
- the bridge is still not detected immediately after installing or enabling the
  extension

Checks:

```bash
gnome-extensions info keyforge-bridge@keyforge
journalctl --user -u keyforge-session -n 100
```

Important:

- after installing the Keyforge package into an already running GNOME session,
  log out and back in before enabling the GNOME Shell bridge extension
- if `gnome-extensions enable keyforge-bridge@keyforge` says the extension does
  not exist, GNOME Shell has usually not rescanned extensions yet; log out and
  back in, then run the enable command again
- restarting `keyforge-session` alone is not always enough if GNOME Shell has
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
journalctl --user -u keyforge-session -n 100
keyforge profiles list
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
- `systemctl status keyforged`
- `systemctl --user status keyforge-session`
- relevant `journalctl` output
- whether the issue reproduces with `-v` or `-vv`
