# GNOME Support

Keymasq supports GNOME 46 and newer through a GNOME Shell extension. Tested on
GNOME 46 (Ubuntu 24.04 LTS) and GNOME 50. The extension uses stable Shell APIs
with no breaking changes across this range.

GNOME 45 and older are not supported.

## Enable the extension

Packaged installs already include the extension files but do not enable it
automatically. You need to enable it once after installation.

The Keymasq GUI can guide this setup. It sends setup actions to
`keymasq-session`, and the session process talks to GNOME Shell over DBus; the
GUI does not run `gnome-extensions` or inspect the desktop environment itself.

If you install the package while already logged into GNOME, the files may be on
disk before the current GNOME Shell session notices the new extension. In that
case, `gnome-extensions enable gnome-bridge@keymasq.tools` can fail with
`Extension "gnome-bridge@keymasq.tools" does not exist` even though the files are
present under `/usr/share/gnome-shell/extensions/`.

For packaged installs, log out and back in once after installing Keymasq so
GNOME Shell rescans system extensions. Then enable the bridge:

```bash
gnome-extensions enable gnome-bridge@keymasq.tools
```

If you want to verify that GNOME Shell sees it before enabling, check:

```bash
gnome-extensions info gnome-bridge@keymasq.tools
```

After enabling the extension, restart the Keymasq session service:

```bash
systemctl --user restart keymasq-session
```

## Verify

Check extension status:

```bash
gnome-extensions info gnome-bridge@keymasq.tools
```

Watch session logs:

```bash
journalctl --user -u keymasq-session -f
```

On success, you should see GNOME listener startup and active-window updates
when switching windows.

## What the extension does

GNOME does not expose window information the same way other Wayland compositors
do. The extension runs inside GNOME Shell and forwards the focused window name
and pointer position to Keymasq so that window-aware profiles and pointer
features work. It also accepts an allowlisted pointer-position request from
`keymasq-session`, letting absolute mouse moves and macro playback ask GNOME
Shell to position the cursor through Mutter instead of relying on uinput
relative-motion fallback.

The same bridge also handles GNOME compositor actions. These are allowlisted
bridge RPCs, not arbitrary shell commands. Keymasq currently supports:

- `workspace` with `next`, `prev`, or a 1-based workspace number
- `move_to_workspace` with `next`, `prev`, or a 1-based workspace number
- `close_active`
- `fullscreen` with `toggle`, `on`, or `off`
- `maximize` with `toggle`, `on`, or `off`

If the extension is missing or disconnected, Keymasq keeps running but
window-based profile activation is unavailable until the extension reconnects.
GNOME compositor actions and compositor-side cursor positioning are also
unavailable until the bridge reconnects.

If Keymasq updates the installed bridge files but your current GNOME session is
still running the old extension code, Keymasq now shows a warning telling you
to log out and back in. That reloads GNOME Shell and starts the updated bridge.

## Manual install from checkout

If you are running from a local checkout or another manual install, copy the
extension into your user extension directory first. If GNOME Shell does not see
it immediately, log out and back in before enabling it:

```bash
mkdir -p ~/.local/share/gnome-shell/extensions/gnome-bridge@keymasq.tools
cp -r gnome-extension/gnome-bridge@keymasq.tools/* ~/.local/share/gnome-shell/extensions/gnome-bridge@keymasq.tools/
gnome-extensions enable gnome-bridge@keymasq.tools
```
