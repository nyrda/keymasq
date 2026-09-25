# Wayland support

Keymasq's normal key and button remapping happens below the desktop through
`keymasqd`. That part does not depend on a Wayland compositor API, because the
desktop sees Keymasq output as normal keyboard, mouse, and gamepad input.

Wayland support matters for desktop-aware features:

- activating and deactivating profiles based on the focused app or window title
- reading the active window for the GUI and CLI
- reading the current pointer position for Natural mouse movement, macro
  recording, and point capture
- sending compositor actions such as workspace, focus, tiling, or close-window
  commands
- asking supported desktops to set the compositor cursor position explicitly

Wayland desktops expose these features differently. Some provide public
protocols, some provide compositor-specific IPC, and GNOME needs a Shell
extension because GNOME Shell does not expose the same window information as
wlroots-based compositors.

## Support matrix

| Desktop or compositor | Window profiles | Cursor feedback | Compositor actions | Notes |
| --- | --- | --- | --- | --- |
| **GNOME** | Yes | Native | [Limited allowlist](#gnome) | Requires GNOME 46 or newer and the Keymasq GNOME Shell extension. Includes a **Set Cursor** compositor action. |
| **KDE Plasma** | Yes | Native | [Limited presets](#kde-plasma) | Uses a temporary KWin script over session D-Bus. |
| **Hyprland** | Yes | Native | [Yes, including Lua dispatchers](#hyprland) | Uses Hyprland sockets. Includes a **Set Cursor** compositor action and Hyprland window tags. |
| **Niri** | Yes | [Layer-shell feedback](#layer-shell-pointer-feedback) | Yes | Uses Niri's event and command socket, with `niri msg action` fallback for custom actions. |
| **COSMIC** | Yes | [Layer-shell feedback](#layer-shell-pointer-feedback) | No | Uses COSMIC Wayland protocols for active-window tracking. |
| **Sway** | Yes | [Layer-shell feedback](#layer-shell-pointer-feedback) | Yes | Tracks windows like generic wlroots and sends actions over Sway's i3-compatible IPC socket. Includes a **Set Cursor** compositor action. |
| **Generic wlroots** | Yes | [Layer-shell feedback](#layer-shell-pointer-feedback) | No | Works on wlroots-based compositors such as Mango, Wayfire, river, and labwc. |
| **Generic layer-shell Wayland** | No | [Layer-shell feedback](#layer-shell-pointer-feedback) | No | Fallback for compositors with `zwlr_layer_shell_v1` and `zxdg_output_manager_v1` but no supported active-window protocol. |
| **X11** | Yes | Native | No | Not Wayland, but useful as a comparison point. |

### What the columns mean

**Window profiles** means Keymasq can see the focused application ID/class and
window title, then apply window-aware profile rules.

**Cursor feedback** describes how Keymasq reads the current cursor position.
Natural mouse movement uses this feedback to adjust its path toward the target.
GNOME, KDE Plasma, Hyprland, and X11 provide cursor position directly. Other
supported Wayland desktops use [layer-shell feedback](#layer-shell-pointer-feedback),
which requires compatible protocols and temporarily places transparent surfaces
over the desktop to track the pointer. Those surfaces can affect how
applications receive pointer events while movement is active. Recording, point
capture, and macros that need a known starting position also use cursor feedback.

**Compositor actions** means Keymasq can ask the desktop to perform actions such
as changing workspace, closing the focused window, toggling fullscreen, moving
focus, or tiling a window. `keymasq-session` sends these requests through direct
desktop communication such as compositor IPC or D-Bus. It does not run shell
commands as a fallback.

## Layer-shell pointer feedback

On generic Wayland compositors, `keymasq-session` reads pointer position through
an internal Wayland client. This path requires both `zwlr_layer_shell_v1` and
`zxdg_output_manager_v1`.

The session process keeps output geometry current through `zxdg_output_v1`
logical position and size events. When a cursor read or Natural mouse movement
needs realtime feedback, it temporarily maps transparent layer surfaces across
the outputs. Wayland pointer enter and motion events report surface-local
coordinates, and Keymasq converts them into global compositor coordinates by
adding the xdg-output logical origin for the focused output.

Those temporary layer surfaces participate in pointer hit-testing while they are
mapped. Keymasq therefore explicitly tears them down as soon as a Natural mouse
movement finishes, instead of waiting for the action's remaining timeout window.

When the pointer crosses from one output layer surface to another, Keymasq
switches the active output on the next pointer enter event. If the compositor
reports an output layout change while tracking is active, Keymasq invalidates
old samples until it receives a fresh pointer event for the new layout.

The GUI still uses `slurp` as a point-picking helper for Capture on
compatible Wayland compositors. The session does not use it for cursor-position
reads or Natural movement feedback.

## Absolute pointer movement

When realtime cursor feedback is available, prefer Natural mouse movement with a
high speed for reliable fixed-position cursor movement. It reads the cursor
position during the move and corrects the path until the target is reached or
the configured timeout expires.

Basic absolute movement first pushes the pointer toward the top-left corner,
then sends relative motion toward the target coordinate.

Because the desktop interprets this as ordinary mouse motion, it does not
reliably work with desktop scaling, fractional or per-monitor scaling, or
multi-monitor output layouts. Pointer acceleration, sensitivity, and other
pointer settings can also shift the final position.

For desktop UI automation on GNOME, Hyprland, or Sway, the compositor action
**Set Cursor** preset is also available when you specifically need the desktop itself
to set the cursor position.

## Desktop details

### GNOME

GNOME does not expose focused-window and pointer information through the
protocols used by wlroots compositors. Keymasq therefore uses a small GNOME
Shell extension. The extension reports the focused app and title, reads the
pointer position, moves the pointer through GNOME Shell, and handles a small
allowlist of compositor actions.

If the extension is missing, disabled, globally blocked, or running an old bridge
version, normal input remapping can still work, but GNOME window profiles,
GNOME compositor actions, and the **Set Cursor** compositor action are
unavailable until the bridge reconnects.

See [gnome.md](gnome.md) for setup and troubleshooting.

### KDE Plasma

Keymasq talks to KWin over the session D-Bus and loads a KWin JavaScript
bridge. That bridge reports the active window back to `keymasq-session`, runs a
limited set of supported KWin actions, and can temporarily load demand-scoped
cursor tracking while natural mouse movement is active.

KDE supports active-window tracking, window-aware profiles, pointer-position
reads, realtime cursor feedback for natural mouse movement, and selected
compositor actions.

Supported compositor actions include switching virtual desktops, closing the
focused window, toggling fullscreen, moving focus, moving the focused window,
quick-tiling, toggling all-desktops, and toggling show-desktop.

### Hyprland

Keymasq targets Hyprland 0.55 and newer. It uses Hyprland's event socket for
active-window updates and the command socket for queries, pointer movement, and
compositor dispatch. This gives Keymasq active-window profiles,
pointer-position reads, Lua dispatcher actions, the **Set Cursor** compositor
action, and Hyprland window tags.

Opening a layer-shell surface that takes keyboard focus, such as a fuzzel or
walker launcher, clears the active window until the surface closes or a window
takes focus again. Keymasq reports the layer's namespace instead, so profiles
can target launchers with [`layer` window rules](profiles.md#layers).
Hyprland's event socket does not report this focus change, so Keymasq asks
Hyprland's Lua API for the keyboard interactivity of each layer that opens.
With a hyprlang config the Lua API is unavailable. The previous window then
stays active while a launcher is open, and profiles with `layer` rules show as
unsupported.

Hyprland sends no event for these layer cases, so Keymasq cannot follow
them:

- A layer that opens while a window has locked the pointer, as some games do,
  does not get the keyboard. Keymasq still reports the layer.
- Clicking back into a layer that is still open after a window took focus
  gives it the keyboard again. Keymasq keeps reporting the window until the
  layer closes or reopens.
- A layer that stays open and turns on keyboard interactivity takes the
  keyboard. Keymasq notices only when the layer closes or reopens.

Tag-based profiles update when focus moves between windows with the same class
and title but different tags. Duplicate updates are suppressed only when class,
title, and tags all match the previous update.

Custom compositor actions in Keymasq use Lua dispatcher expressions in the
dispatcher field, for example `hl.dsp.focus({ workspace = "e+1" })`. Leave the
args field empty for these actions.

### Niri

Keymasq connects directly to Niri's socket. It uses one connection for the event
stream and another for command requests. This supports active-window profiles,
focused-window queries, window activation by title, and compositor actions.

Common Niri compositor actions use a direct socket path. Custom actions can fall
back to `niri msg action` syntax.

Pointer-position reads use [layer-shell feedback](#layer-shell-pointer-feedback)
when Niri exposes the required Wayland protocols.

### COSMIC

Keymasq uses `ext_foreign_toplevel_list_v1` together with
`zcosmic_toplevel_info_v1` to track the active COSMIC window. This supports
window-aware profiles and active-window queries.

Pointer-position reads use [layer-shell feedback](#layer-shell-pointer-feedback)
when COSMIC exposes the required Wayland protocols.
Keymasq does not currently expose COSMIC compositor actions.

### Sway

Keymasq connects to Sway's IPC socket from `SWAYSOCK` (or `I3SOCK`, which Sway
also sets), so the session service needs one of them in its environment. If
`systemctl --user show-environment` does not list `SWAYSOCK`, add
`exec systemctl --user import-environment SWAYSOCK WAYLAND_DISPLAY` to your
Sway config. Keymasq does not scan the runtime directory for sockets, so it
cannot attach to another Sway session.

Active-window tracking uses `zwlr_foreign_toplevel_manager_v1`, the same as
the [generic wlroots listener](#generic-wlroots-wayland). It follows keyboard
focus, so switching to an empty workspace or opening a keyboard-interactive
launcher clears the active window. Sway's IPC socket carries compositor
actions, which accept any Sway command. See [Sway actions](actions.md#sway).

**Set Cursor** runs `seat - cursor set X Y`. Sway reads those coordinates
relative to the top-left corner of the output layout, so Keymasq subtracts the
layout origin first. Coordinates stay correct when a monitor sits left of or
above the primary one.

Pointer-position reads use [layer-shell feedback](#layer-shell-pointer-feedback).
The feedback nudges the pointer by one pixel to get a sample, so a read right
after **Set Cursor** can differ from the target by one pixel.

### Generic wlroots Wayland

The generic Wayland listener works when a compositor exposes
`zwlr_foreign_toplevel_manager_v1`. This protocol lets Keymasq read the active
window's application ID and title, which is enough for window-aware profiles.

Known compatible compositors include Mango, Wayfire, river, labwc, and other
wlroots-based compositors that expose the required protocol. The VM test matrix
covers this path with Mango.

Pointer-position reads use [layer-shell feedback](#layer-shell-pointer-feedback)
when the compositor exposes the required Wayland protocols.
Generic wlroots support does not include compositor actions because there is
no shared compositor-dispatch API.

## Troubleshooting

Check what Keymasq detected:

```bash
keymasq status
```

Watch the session service logs:

```bash
journalctl --user -u keymasq-session -f
```

Common things to check:

- On GNOME, make sure the extension is installed, enabled, and loaded by the
  current GNOME Shell session. See [gnome.md](gnome.md).
- For generic Wayland pointer reads, make sure the compositor exposes
  `zwlr_layer_shell_v1` and `zxdg_output_manager_v1`.
- For GUI Capture through `slurp`, make sure `slurp` is installed and can run in
  the current Wayland session.
- If absolute mouse movement is unreliable, use Natural movement where realtime
  cursor feedback is supported. Scaling, output layout, acceleration, and
  sensitivity settings affect basic absolute movement.
- If your supported desktop is not working as described, please open an issue
  with the desktop/compositor name, version, and `keymasq-session` logs.
