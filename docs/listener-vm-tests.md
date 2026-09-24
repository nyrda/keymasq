# Listener VM tests

Keymasq includes a NixOS VM matrix for listener integration tests.

## Support status

This page distinguishes between:

- environments covered by dedicated listener VM tests
- environments expected to work through a shared listener path, but not
  currently covered by a dedicated VM in this matrix

The first group is the tested support matrix. Treat the second group as
best-effort compatibility until it gets dedicated VM coverage.

### Tested support matrix

- GNOME
- KDE Plasma
- Hyprland
- Niri
- Sway
- COSMIC
- Generic Wayland via `zwlr_foreign_toplevel_manager_v1` (tested with Mango)
- X11

### Expected to work, but not covered by dedicated VM tests

These environments rely on listener paths that Keymasq supports, but they are
not currently part of the dedicated VM integration matrix:

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

## Passing tests

| Test | Compositor | Desktop | Status |
| ---- | ---------- | ------- | ------ |
| `listener-vm-gnome-bridge` | gnome | GNOME (bridge only) | ✓ passing |
| `listener-vm-gnome` | gnome | GNOME (full listener) | ✓ passing |
| `listener-vm-kde` | kde | KDE Plasma 6 | ✓ passing |
| `listener-vm-hyprland` | hyprland | Hyprland | ✓ passing |
| `listener-vm-niri` | niri | Niri | ✓ passing |
| `listener-vm-xfce` | x11 | XFCE | ✓ passing |
| `listener-vm-cosmic` | cosmic | COSMIC | ✓ passing |
| `listener-vm-sway` | sway | Sway | ✓ passing |
| `listener-vm-mango` | wayland | Mango (wlroots fallback) | ✓ passing |

## What the tests exercise

Each desktop test validates:

1. **Compositor detection.** `get_compositor` returns the correct compositor ID.
2. **Listener startup.** `keymasq-session` starts and the compositor-specific listener becomes active.
3. **Window open.** The test opens a GTK4 window via `window-lab`, and the listener reports its title.
4. **Focus switching.** The test opens a second window, then moves focus back to the first. The listener tracks each change.
5. **Title change.** The test retitles an existing window, and the listener picks up the new title.
6. **Window close.** The test closes a window, and the listener reports focus moving to the remaining window.
7. **Cursor position.** Where supported, the test moves the pointer to a known location and verifies that `get_cursor_position` returns integer coordinates in the expected on-screen range.
8. **Listener-scoped dispatch.** Compositor-specific tests can trigger a compositor dispatch through Keymasq and verify the observable result.

The shared desktop harness includes the cursor-position check for GNOME, KDE, Hyprland, XFCE/X11, COSMIC, Sway, Mango, and Niri. The bridge-only `listener-vm-gnome-bridge` job separately validates raw bridge pointer request/response behavior.

## Running a desktop VM test

Use the integration helper from the repository root:

```bash
./scripts/integration.sh cosmic
./scripts/integration.sh gnome
./scripts/integration.sh cosmic --repeat 3
./scripts/integration.sh listeners
```

`--repeat N` applies to every selected integration check, including individual
listener shortcuts and the `listeners` group.

List every shortcut with:

```bash
./scripts/integration.sh --help
```

The shortcuts map to the same Nix check targets shown below. `listeners` runs
the full listener VM matrix.

```bash
nix build 'path:.#checks.x86_64-linux.listener-vm-gnome-bridge'
nix build 'path:.#checks.x86_64-linux.listener-vm-gnome'
nix build 'path:.#checks.x86_64-linux.listener-vm-kde'
nix build 'path:.#checks.x86_64-linux.listener-vm-hyprland'
nix build 'path:.#checks.x86_64-linux.listener-vm-niri'
nix build 'path:.#checks.x86_64-linux.listener-vm-xfce'
nix build 'path:.#checks.x86_64-linux.listener-vm-cosmic'
nix build 'path:.#checks.x86_64-linux.listener-vm-sway'
nix build 'path:.#checks.x86_64-linux.listener-vm-mango'
```

Use the `path:` flake reference while the VM files are uncommitted. A plain `.#...` build can miss new files because it evaluates the Git snapshot.

These tests are heavy. Use a Linux host with KVM acceleration.

## Helper tools

The VM environments install:

- `keymasq-session-query` — sends commands to the session socket and prints the JSON response.
- `keymasq-listener-window-lab` — the shared GTK4 window lab wrapped with a listener-specific command name.
- `keymasq-listener-window-labctl` — shared CLI client for `window-lab` wrapped with a listener-specific command name.

`window-lab` gives the test real desktop windows to observe without depending on compositor-specific tooling:

- open a window with a known title
- open a second window
- focus the first window again
- retitle the first window
- close windows

## Compositor-specific notes

### GNOME

The GNOME VM installs and enables the `gnome-bridge@keymasq.tools` Shell extension automatically. The GNOME listener depends on that bridge for active-window and pointer updates.

**Focus and title tracking.** GNOME Wayland has aggressive focus-stealing prevention. GTK's `window.present()` is not sufficient to switch focus from code. The bridge extension handles this by:

- Tracking `notify::focus-window` on `global.display` for focus changes.
- Tracking `notify::title` on the currently focused `Meta.Window` for title renames.
- Accepting `activate_title` messages from the listener, which call `meta_window.activate()` from inside the Shell process to bypass Wayland focus restrictions.

The GNOME VM test uses the `activate_title` bridge command to switch focus between windows. The same command is also available as the `activate_title` session command.

**Bridge preflight.** The dedicated `listener-vm-gnome-bridge` check starts GNOME, binds to the raw `gnome-bridge.sock`, and validates:

- extension connection
- `hello` handshake
- `focus_changed` messages
- pointer request/response
- pointer set request/response through the GNOME Shell bridge

The full `listener-vm-gnome` test exercises the keymasq-session GNOME listener end-to-end (compositor detection → bridge connection → window tracking → cursor position). The two tests are separate to avoid socket conflicts between the probe and `keymasq-session`.

**Set Cursor dispatch.** The full GNOME listener VM test exercises the
`set_cursor_position` compositor action through `keymasq-session` and verifies
that the pointer lands at the requested coordinates.

### KDE Plasma 6

The KDE test does not use a generic Wayland foreign-toplevel protocol. It exercises the real KDE listener path in [keymasq/session/listeners/kde.py](https://github.com/nyrda/keymasq/blob/master/keymasq/session/listeners/kde.py):

- `keymasq-session` connects to `org.kde.KWin` over the session D-Bus
- it calls `org.kde.kwin.Scripting.loadScript`
- it injects a temporary KWin JavaScript plugin
- that plugin reports active-window changes back to `keymasq-session` over the exported `keymasq.kde.Listener` D-Bus interface

The VM test asserts both:

- `org.kde.KWin` is present on the session bus
- `keymasq-session` logs `KDE listener script loaded`, proving the KWin script/plugin path is active

Window switching in the test still uses the GTK lab app's normal activation path, but the observed active-window updates come from the injected KWin script, not from `zwlr_foreign_toplevel_manager_v1`.

### Hyprland

The Hyprland test uses the Hyprland listener, which connects to `.socket2.sock` for `activewindow>>` events and `.socket.sock` for IPC commands. UWSM (Universal Wayland Session Manager) handles session setup and exports `HYPRLAND_INSTANCE_SIGNATURE` to the systemd user environment.

**Focus switching.** The test uses
`hyprctl dispatch 'hl.dsp.focus({ window = "title:<name>" })'` to switch focus,
which is Hyprland's native IPC mechanism.

**Window tags.** Hyprland is the only compositor in the matrix that supports window tags. The test verifies that `get_active_window` returns a `tags` field (currently `[]` for the test windows).

**Set Cursor dispatch.** The Hyprland VM test exercises the
`set_cursor_position` compositor action through the Hyprland listener and
confirms that the compositor reports the exact requested coordinates afterward.

### Niri

The Niri test uses the dedicated Niri listener, which connects to `$NIRI_SOCKET` directly. As with the upstream Niri IPC design, Keymasq uses two separate connections. One is an event-stream socket for focused-window tracking, and the other is a command socket for compositor actions.

**Focus switching.** The test activates windows through Keymasq's Niri listener path (`activate_title`) so the listener's cached focused-window state stays consistent even when the VM seat does not report a focused Niri window.

**Dispatch path.** The test sends `dispatch_compositor` through the session socket for the Niri `toggle-window-floating` action and verifies that the Beta window's `is_floating` state changes through `niri msg --json windows`.

**Cursor position.** The Niri VM test uses the native layer-shell cursor feedback
path, just like Sway and COSMIC when they expose `zwlr_layer_shell_v1` and
`zxdg_output_manager_v1`. The test grabs the QEMU AT keyboard so keymasqd
creates uinput devices (including `keymasq-mouse`), then verifies that
`get_cursor_position` returns valid on-screen coordinates. A retry loop (up to 3
attempts) handles VM timing variance where the compositor may need extra time to
register the new uinput mouse or where the temporary layer surfaces are not ready
before the synthetic cursor sample nudge runs.

**Software renderer patch.** Niri (Smithay) rejects software EGL renderers (`llvmpipe`) in `src/backend/tty.rs`, which prevents it from creating any `wl_output` in a VM without a real GPU. The test uses a patched niri (`niriPatched` in `listener-vm-matrix.nix`) that disables this check. `niriExpectedVersion` pins the niri version. A Nix assertion fails evaluation if nixpkgs ships a different version, and a build-time grep guard fails the build if the patch target moves. See the `niriPatched` comments in `listener-vm-matrix.nix` for the update procedure.

### COSMIC

The COSMIC test uses the COSMIC listener backed by `ext_foreign_toplevel_list_v1` and `zcosmic_toplevel_info_v1` Wayland protocols. The compositor (`cosmic-comp`) is Smithay-based and implements the `xdg-activation-v1` protocol, so GTK's `window.present()` works for focus switching without compositor-specific helpers.

The COSMIC VM briefly shows `com.system76.CosmicInitialSetup` as the active window before test windows appear. The test tolerates this by polling until it observes the expected title.

### Sway

The Sway test uses the dedicated listener in [keymasq/session/listeners/sway.py](https://github.com/nyrda/keymasq/blob/master/keymasq/session/listeners/sway.py), which speaks Sway's i3-compatible IPC protocol. Keymasq detects the compositor as `"sway"`. The VM exports `SWAYSOCK` to the systemd user environment.

**Focus switching.** New windows get focus from the window manager. The test switches back to the first window through Keymasq's `activate_title`, which sends `[con_id=<id>] focus` over IPC.

**Dispatch path.** After the window checks, the test sends `dispatch_compositor` through the session socket and verifies each result with `swaymsg`:

- `exec touch /tmp/keymasq-exec-probe` runs and creates the probe file
- `floating toggle` moves the Beta window into the floating layer and back
- `workspace number 4` focuses an empty workspace, and the listener then reports no active window
- `workspace back_and_forth` returns to Beta

**Set Cursor dispatch.** The test runs the `set_cursor_position` compositor action. Sway reads the pointer through layer-shell feedback, which nudges it one pixel to get a sample, so the check allows one pixel of difference.

### Mango (wlroots fallback)

The Mango test validates the wlroots fallback listener (`WlrootsWaylandListener`), which uses `zwlr_foreign_toplevel_manager_v1`. This is the generic Wayland listener that works on any compositor implementing the wlroots foreign-toplevel protocol. Mango has no dedicated Keymasq listener, so Keymasq detects the compositor as `"wayland"`.

**Software rendering.** The VM has no GPU. The test sets `WLR_RENDERER_ALLOW_SOFTWARE=1` so Mango's GLES renderer runs on llvmpipe.

**Focus switching.** Mango ignores GTK's `present()`. The test looks up the window id with `mmsg get all-clients` and focuses it with `mmsg dispatch focusid client,<id>`, using `MANGO_INSTANCE_SIGNATURE` from the systemd user environment.

### XFCE (X11)

The XFCE test uses the X11 listener backed by `python-xlib`. The listener reads `_NET_ACTIVE_WINDOW` from the X root window and watches `PropertyNotify` events for title and class changes. On X11, GTK's `window.present()` works for focus switching, so the test needs no compositor-specific activation helpers.

**Cursor position.** The X11 listener reads cursor coordinates with
`query_pointer()`. Absolute mouse workflows use keymasqd's virtual mouse
movement path.

## Focus switching summary

| Compositor | `present()` works? | Activation method |
| ---------- | ------------------ | ----------------- |
| GNOME | no | bridge `activate_title` → `meta_window.activate()` |
| KDE | yes | GTK activation. Listener events come from the injected KWin script over D-Bus |
| Hyprland | no | `hyprctl dispatch 'hl.dsp.focus({ window = "title:<name>" })'` |
| Niri | no | Keymasq `activate_title` → Niri `FocusWindow { id }` |
| COSMIC | yes | GTK `window.present()` |
| Sway | no | Keymasq `activate_title` → `[con_id=<id>] focus` |
| Mango | no | `mmsg dispatch focusid client,<id>` |
| X11/XFCE | yes | GTK `window.present()` |

## Next iteration

The matrix can grow to include:

- screenshots or video capture on failure
- workspace switching checks
- Hyprland window tag assertions beyond the empty default
- more GNOME bridge assertions beyond startup and focus propagation
- COSMIC initial-setup suppression for cleaner test output
