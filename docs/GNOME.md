# GNOME Support

Keyforge supports GNOME 46 and newer through a GNOME Shell extension.

## Enable the extension

Packaged installs already include the extension files but do not enable it
automatically. You need to enable it once after installation.

If you install the package while already logged into GNOME, the files may be on
disk before the current GNOME Shell session notices the new extension. In that
case, `gnome-extensions enable keyforge-bridge@keyforge` can fail with
`Extension "keyforge-bridge@keyforge" does not exist` even though the files are
present under `/usr/share/gnome-shell/extensions/`.

For packaged installs, log out and back in once after installing Keyforge so
GNOME Shell rescans system extensions. Then enable the bridge:

```bash
gnome-extensions enable keyforge-bridge@keyforge
```

If you want to verify that GNOME Shell sees it before enabling, check:

```bash
gnome-extensions info keyforge-bridge@keyforge
```

After enabling the extension, restart the Keyforge session service:

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

The same bridge also handles GNOME compositor actions. These are allowlisted
bridge RPCs, not arbitrary shell commands. Keyforge currently supports:

- `workspace` with `next`, `prev`, or a 1-based workspace number
- `move_to_workspace` with `next`, `prev`, or a 1-based workspace number
- `close_active`
- `fullscreen` with `toggle`, `on`, or `off`
- `maximize` with `toggle`, `on`, or `off`

If the extension is missing or disconnected, Keyforge keeps running but
window-based profile activation is unavailable until the extension reconnects.
GNOME compositor actions are also unavailable until the bridge reconnects.

## Manual install from checkout

If you are running from a local checkout or another manual install, copy the
extension into your user extension directory first. If GNOME Shell does not see
it immediately, log out and back in before enabling it:

```bash
mkdir -p ~/.local/share/gnome-shell/extensions/keyforge-bridge@keyforge
cp -r gnome-extension/keyforge-bridge@keyforge/* ~/.local/share/gnome-shell/extensions/keyforge-bridge@keyforge/
gnome-extensions enable keyforge-bridge@keyforge
```
