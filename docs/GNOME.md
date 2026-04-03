# GNOME Support

Keyforge supports GNOME 46 and newer through a GNOME Shell extension.

## Enable the extension

Packaged installs already include the extension files but do not enable it
automatically. You need to enable it once after installation.

```bash
gnome-extensions enable keyforge-bridge@keyforge
```

After enabling the extension, log out and back in. Then restart the Keyforge
session service:

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

On success, you should see GNOME listener startup and active-window updates
when switching windows.

## What the extension does

GNOME does not expose window information the same way other Wayland compositors
do. The extension runs inside GNOME Shell and forwards the focused window name
and pointer position to Keyforge so that window-aware profiles and pointer
features work.

If the extension is missing or disconnected, Keyforge keeps running but
window-based profile activation is unavailable until the extension reconnects.

## Manual install from checkout

If you are running from a local checkout or another manual install, copy the
extension into your user extension directory first, then enable it:

```bash
mkdir -p ~/.local/share/gnome-shell/extensions/keyforge-bridge@keyforge
cp -r gnome-extension/keyforge-bridge@keyforge/* ~/.local/share/gnome-shell/extensions/keyforge-bridge@keyforge/
gnome-extensions enable keyforge-bridge@keyforge
```
