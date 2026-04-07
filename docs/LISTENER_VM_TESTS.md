# Listener VM Tests

Keyforge includes a NixOS VM matrix for listener integration tests.

## Support Status

### Supported and integration-tested

- GNOME
- KDE Plasma
- Hyprland
- Niri
- COSMIC
- Generic Wayland via `zwlr_foreign_toplevel_manager_v1` (tested with Sway)
- X11

### Supported but not covered by listener integration tests

These environments rely on supported listener paths, but do not currently have
dedicated listener VM coverage in this matrix:

- river
- treeland
- wayfire
- Mir / Louvre-style sessions
- labwc
- Jay

### Explicitly unsupported

- Weston
- Muffin
- gamescope sessions

## Passing Tests

| Test | Compositor | Desktop | Status |
| ---- | ---------- | ------- | ------ |
| `listener-vm-gnome-bridge` | gnome | GNOME (bridge only) | ✓ passing |
| `listener-vm-gnome` | gnome | GNOME (full listener) | ✓ passing |
| `listener-vm-kde` | kde | KDE Plasma 6 | ✓ passing |
| `listener-vm-hyprland` | hyprland | Hyprland | ✓ passing |
| `listener-vm-niri` | niri | Niri | ✓ passing |
| `listener-vm-xfce` | x11 | XFCE | ✓ passing |
| `listener-vm-cosmic` | cosmic | COSMIC | ✓ passing |
| `listener-vm-sway` | wayland | Sway (wlroots fallback) | ✓ passing |

## What The Tests Exercise

Each desktop test validates:

1. **Compositor detection** — `get_compositor` returns the correct compositor ID.
2. **Listener startup** — `keyforge-session` starts and the compositor-specific listener becomes active.
3. **Window open** — a GTK4 window is opened via `window-lab`; the listener reports its title.
4. **Focus switching** — a second window is opened, then focus moves back to the first; the listener tracks each change.
5. **Title change** — an existing window is retitled; the listener picks up the new title.
6. **Window close** — a window is closed; the listener reports focus moving to the remaining window.
7. **Cursor position** — where supported, the test moves the pointer to a known location and verifies that `get_cursor_position` returns integer coordinates in the expected on-screen range.
8. **Listener-scoped dispatch** — compositor-specific tests can trigger a compositor dispatch through Keyforge and verify the observable result.

The shared desktop harness includes the cursor-position check for GNOME, KDE, Hyprland, XFCE/X11, COSMIC, and Sway. Niri support is enabled in Keyforge again, and the VM harness attempts the same cursor query there, but it still tolerates the known NixOS VM-specific `slurp` failure mode where the session logs `stderr=no wl_output`. The bridge-only `listener-vm-gnome-bridge` job separately validates raw bridge pointer request/response behavior.

## Running A Desktop VM Test

```bash
nix build 'path:.#checks.x86_64-linux.listener-vm-gnome-bridge'
nix build 'path:.#checks.x86_64-linux.listener-vm-gnome'
nix build 'path:.#checks.x86_64-linux.listener-vm-kde'
nix build 'path:.#checks.x86_64-linux.listener-vm-hyprland'
nix build 'path:.#checks.x86_64-linux.listener-vm-niri'
nix build 'path:.#checks.x86_64-linux.listener-vm-xfce'
nix build 'path:.#checks.x86_64-linux.listener-vm-cosmic'
nix build 'path:.#checks.x86_64-linux.listener-vm-sway'
```

Use the `path:` flake reference while the VM files are uncommitted. A plain `.#...` build can miss new files because it evaluates the Git snapshot.

These tests are heavy. A Linux host with KVM acceleration is strongly recommended.

## Helper Tools

The VM environments install:

- `keyforge-session-query` — sends commands to the session socket and prints the JSON response.
- `keyforge-listener-window-lab` — small GTK4 app controlled over a Unix socket.
- `keyforge-listener-window-labctl` — CLI client for `window-lab`.

`window-lab` gives the test real desktop windows to observe without depending on compositor-specific tooling:

- open a window with a known title
- open a second window
- focus the first window again
- retitle the first window
- close windows

## Compositor-Specific Notes

### GNOME

The GNOME VM installs and enables the `keyforge-bridge@keyforge` Shell extension automatically. The GNOME listener depends on that bridge for active-window and pointer updates.

**Focus and title tracking**: GNOME Wayland has aggressive focus-stealing prevention. GTK's `window.present()` is not sufficient to programmatically switch focus. The bridge extension handles this by:

- Tracking `notify::focus-window` on `global.display` for focus changes.
- Tracking `notify::title` on the currently focused `Meta.Window` for title renames.
- Accepting `activate_title` messages from the listener, which call `meta_window.activate()` from inside the Shell process to bypass Wayland focus restrictions.

The `activate_title` bridge command is used by the GNOME VM test to switch focus between windows, and is also exposed as the `activate_title` session command.

**Bridge preflight**: The dedicated `listener-vm-gnome-bridge` check starts GNOME, binds to the raw `gnome-bridge.sock`, and validates:

- extension connection
- `hello` handshake
- `focus_changed` messages
- pointer request/response

The full `listener-vm-gnome` test exercises the keyforge-session GNOME listener end-to-end (compositor detection → bridge connection → window tracking → cursor position). The two tests are separate to avoid socket conflicts between the probe and `keyforge-session`.

### KDE Plasma 6

The KDE test does not use a generic Wayland foreign-toplevel protocol. It exercises the real KDE listener path in [keyforge/session/listeners/kde.py](../keyforge/session/listeners/kde.py):

- `keyforge-session` connects to `org.kde.KWin` over the session D-Bus
- it calls `org.kde.kwin.Scripting.loadScript`
- it injects a temporary KWin JavaScript plugin
- that plugin reports active-window changes back to `keyforge-session` over the exported `keyforge.kde.Listener` D-Bus interface

The VM test now asserts both:

- `org.kde.KWin` is present on the session bus
- `keyforge-session` logs `KDE listener script loaded`, proving the KWin script/plugin path is active

Window switching in the test still uses the GTK lab app's normal activation path, but the observed active-window updates come from the injected KWin script, not from `zwlr_foreign_toplevel_manager_v1`.

### Hyprland

The Hyprland test uses the Hyprland listener which connects to `.socket2.sock` for `activewindow>>` events and `.socket.sock` for IPC commands. UWSM (Universal Wayland Session Manager) handles session setup and exports `HYPRLAND_INSTANCE_SIGNATURE` to the systemd user environment.

**Focus switching**: The test uses `hyprctl dispatch focuswindow title:<name>` to switch focus, which is Hyprland's native IPC mechanism.

**Window tags**: Hyprland is the only compositor in the matrix that supports window tags. The test verifies that `get_active_window` returns a `tags` field (currently `[]` for the test windows).

### Niri

The Niri test uses the dedicated Niri listener which connects to `$NIRI_SOCKET` directly. As with the upstream Niri IPC design, Keyforge uses two separate connections: one event-stream socket for focused-window tracking and one command socket for compositor actions.

**Focus switching**: The test activates windows through Keyforge's Niri listener path (`activate_title`) so the listener's cached focused-window state stays coherent even when the VM seat does not report a focused Niri window.

**Dispatch path**: The test sends `dispatch_compositor` through the session socket for the Niri `toggle_window_floating` dispatcher and verifies that the Beta window's `is_floating` state changes through `niri msg --json windows`.

**Cursor position**: Keyforge now advertises the normal `slurp`-backed cursor path for Niri again. The VM harness waits for `WAYLAND_DISPLAY` and `NIRI_SOCKET` in the systemd user environment before restarting `keyforge-session`, but the current NixOS listener VM can still hit a `slurp` failure with `stderr=no wl_output`. When that exact failure is observed in the service journal, the test logs it and continues rather than failing the whole Niri matrix job.

### COSMIC

The COSMIC test uses the COSMIC listener backed by `ext_foreign_toplevel_list_v1` and `zcosmic_toplevel_info_v1` Wayland protocols. The compositor (`cosmic-comp`) is Smithay-based and implements the `xdg-activation-v1` protocol, so GTK's `window.present()` works for focus switching without compositor-specific helpers.

Note: the COSMIC VM briefly shows `com.system76.CosmicInitialSetup` as the active window before test windows appear. The test tolerates this by polling until the expected title is observed.

### Sway (wlroots fallback)

The Sway test validates the wlroots fallback listener (`WlrootsWaylandListener`) which uses `zwlr_foreign_toplevel_manager_v1`. This is the generic Wayland listener that works on any compositor implementing the wlroots foreign-toplevel protocol. The compositor is detected as `"wayland"`.

**Focus switching**: The test uses `swaymsg "[title=<name>] focus"` to switch focus via Sway's native IPC, with `SWAYSOCK` extracted from the systemd user environment.

### XFCE (X11)

The XFCE test uses the X11 listener backed by `python-xlib`. The listener reads `_NET_ACTIVE_WINDOW` from the X root window and watches `PropertyNotify` events for title and class changes. On X11, GTK's `window.present()` works for focus switching, so no compositor-specific activation helpers are needed.

## Focus Switching Summary

| Compositor | `present()` works? | Activation method |
| ---------- | ------------------ | ----------------- |
| GNOME | no | bridge `activate_title` → `meta_window.activate()` |
| KDE | yes | GTK activation; listener events come from injected KWin script over D-Bus |
| Hyprland | no | `hyprctl dispatch focuswindow title:<name>` |
| Niri | no | Keyforge `activate_title` -> Niri `FocusWindow { id }` |
| COSMIC | yes | GTK `window.present()` |
| Sway | no | `swaymsg "[title=<name>] focus"` |
| X11/XFCE | yes | GTK `window.present()` |

## Next Iteration

The matrix is designed to be extended with:

- screenshots or video capture on failure
- workspace switching checks
- Hyprland window tag assertions beyond the empty default
- richer GNOME bridge assertions beyond startup and focus propagation
- COSMIC initial-setup suppression for cleaner test output
