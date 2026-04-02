# GNOME Support

Keyforge supports GNOME 46 and newer through a GNOME Shell extension bridge.

The extension sends active-window changes and pointer coordinates to `keyforge-session`
over a local Unix socket:

- socket: `$XDG_RUNTIME_DIR/keyforge/gnome-bridge.sock`
- protocol: newline-delimited JSON

## Why a bridge is needed

GNOME does not expose the same toplevel protocols used by wlroots, COSMIC or Hyprland.
The extension runs inside GNOME Shell and forwards:

- focused window changes (`app_id`, `wm_class`, `title`)
- pointer position requests

If the extension is missing or disconnected, Keyforge soft-degrades and keeps running,
but window-based matching and pointer reads are unavailable until the bridge reconnects.

## Install the extension

Packaged installs already include the extension files. They do not enable the
GNOME Shell extension for the user. On GNOME, you must enable the extension
explicitly after installation.

Enable it with:

```bash
gnome-extensions enable keyforge-bridge@keyforge
```

After enabling the extension, restart your GNOME Shell session. Logout/login is
the safest path. Then restart the Keyforge session service:

```bash
systemctl --user restart keyforge-session
```

## Verify

Check extension status:

```bash
gnome-extensions info keyforge-bridge@keyforge
```

Watch session logs:

```bash
journalctl --user -u keyforge-session -f
```

On success, you should see GNOME listener startup and active-window updates when switching windows.

## Manual install from checkout

If you are running from a local checkout or another manual install, copy the
extension into your user extension directory first, then enable it:

```bash
mkdir -p ~/.local/share/gnome-shell/extensions/keyforge-bridge@keyforge
cp -r gnome-extension/keyforge-bridge@keyforge/* ~/.local/share/gnome-shell/extensions/keyforge-bridge@keyforge/
gnome-extensions enable keyforge-bridge@keyforge
```
