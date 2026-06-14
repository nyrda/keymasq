# Wayland Support

Keymasq's normal key and button remapping happens below the desktop through
`keymasqd`. That part does not depend on a Wayland compositor API: the desktop
sees Keymasq output as normal keyboard, mouse, and gamepad input.

Wayland support matters for desktop-aware features:

- switching profiles based on the focused app or window title
- reading the active window for the GUI and CLI
- reading the current pointer position for macro recording and point capture
- moving the pointer to a screen position through absolute mouse actions
- sending compositor actions such as workspace, focus, tiling, or close-window
  commands
- asking supported desktops to set the compositor cursor position explicitly

Wayland desktops expose these features differently. Some provide public
protocols, some provide compositor-specific IPC, and GNOME needs a Shell
extension because GNOME Shell does not expose the same window information as
wlroots-based compositors.

## Support Matrix

| Desktop or compositor | Window profiles | Pointer position | Absolute mouse action | Compositor actions | Notes |
| --- | --- | --- | --- | --- | --- |
| **GNOME Wayland** | Yes | Native | Virtual mouse | [Limited allowlist](#gnome-wayland) | Requires GNOME 46 or newer and the Keymasq GNOME Shell extension. Includes a **Set Cursor** compositor action. |
| **KDE Plasma Wayland** | Yes | Native | Virtual mouse | [Limited presets](#kde-plasma-wayland) | Uses a temporary KWin script over session D-Bus. |
| **Hyprland** | Yes | Native | Virtual mouse | Yes | Uses Hyprland sockets. Includes a **Set Cursor** compositor action and Hyprland window tags. |
| **Niri** | Yes | [Slurp-assisted](#slurp-assisted-pointer-capture) | Virtual mouse | Yes | Uses Niri's event and command socket, with `niri msg action` fallback for custom actions. |
| **COSMIC Wayland** | Yes | [Slurp-assisted](#slurp-assisted-pointer-capture) | Virtual mouse | No | Uses COSMIC Wayland protocols for active-window tracking. |
| **Sway and generic wlroots** | Yes | [Slurp-assisted](#slurp-assisted-pointer-capture) | Virtual mouse | No | Works on wlroots-based compositors such as Sway, Wayfire, river, and labwc. |
| **X11** | Yes | Native | Virtual mouse | No | Not Wayland, but useful as a comparison point. |

### What the columns mean

**Window profiles** means Keymasq can see the focused application ID/class and
window title, then apply window-aware profile rules.

**Pointer position** means Keymasq can ask where the pointer currently is. This
is used by recording, point capture, and macros that need a known starting
position. Some compositors use a slurp-assisted path for this; see
[Slurp-Assisted Pointer Capture](#slurp-assisted-pointer-capture).

**Absolute mouse action** means Keymasq can attempt to move the pointer toward a
requested screen coordinate by sending normal motion through its virtual mouse
device. See [Absolute Pointer Movement](#absolute-pointer-movement).

**Compositor actions** means Keymasq can ask the desktop to perform actions such
as changing workspace, closing the focused window, toggling fullscreen, moving
focus, or tiling a window. `keymasq-session` sends these requests through direct
desktop communication such as compositor IPC or D-Bus. It does not run shell
commands as a fallback.

## Slurp-Assisted Pointer Capture

`slurp` is a small Wayland selection tool. It opens a temporary Wayland layer
surface and returns the point or region selected by a pointer click. Keymasq uses
it only on compatible Wayland compositors where this layer-shell based selection
works.

For slurp-assisted pointer reads, `keymasq-session` starts `slurp` in point mode
with an invisible overlay. After the overlay has time to appear,
`keymasq-session` asks `keymasqd` to send a tiny mouse nudge and a left-click
through the virtual mouse. `slurp` receives that click at the current pointer
location, prints the selected coordinates, and `keymasq-session` parses them.

This path is used for pointer-position reads on Niri, COSMIC, and generic
wlroots Wayland. It is different from exact pointer movement: `slurp` helps
Keymasq read where the pointer is, but it does not provide a native compositor
API for moving the pointer.

## Absolute Pointer Movement

Absolute mouse actions use Keymasq's virtual mouse device. This does not
teleport the pointer or ask the compositor to place it at an exact coordinate.
It sends normal relative mouse motion through `keymasqd`: four movement events
in two batches. First it sends a very large negative X movement and a very large
negative Y movement to push the pointer toward the top-left corner. Then it
sends a positive X movement and a positive Y movement toward the target
coordinate.

Because this is interpreted as ordinary mouse motion, it does not reliably work
with desktop scaling, fractional or per-monitor scaling, or multi-monitor output
layouts. Pointer acceleration, sensitivity, and other pointer settings can also
shift the final position.

When realtime cursor feedback is available, prefer Natural mouse movement with a
high speed for reliable fixed-position cursor movement. Natural movement still
uses virtual mouse motion, but it reads the cursor position during the move and
corrects the path until the target is reached or the configured timeout expires.

For desktop UI automation on GNOME or Hyprland, the compositor action **Set
Cursor** preset is also available when you specifically need the desktop itself
to set the cursor position. That path is independent from absolute mouse
actions.

## Desktop Details

### GNOME Wayland

GNOME does not expose focused-window and pointer information through the
protocols used by wlroots compositors. Keymasq therefore uses a small GNOME
Shell extension. The extension reports the focused app and title, reads the
pointer position, moves the pointer through GNOME Shell, and handles a small
allowlist of compositor actions.

If the extension is missing, disabled, globally blocked, or running an old bridge
version, normal input remapping can still work, but GNOME window profiles,
GNOME compositor actions, and the **Set Cursor** compositor action are
unavailable until the bridge reconnects.

See [GNOME.md](GNOME.md) for setup and troubleshooting.

### KDE Plasma Wayland

Keymasq talks to KWin over the session D-Bus and loads a KWin JavaScript
bridge. That bridge reports the active window back to `keymasq-session`, runs a
limited set of supported KWin actions, and can temporarily load demand-scoped
cursor tracking while natural mouse movement is active.

KDE supports active-window tracking, window-aware profiles, pointer-position
reads, realtime cursor feedback for natural mouse movement, and selected
compositor actions. Absolute mouse actions use Keymasq's virtual mouse device.

Supported compositor actions include switching virtual desktops, closing the
focused window, toggling fullscreen, moving focus, moving the focused window,
quick-tiling, toggling all-desktops, and toggling show-desktop.

### Hyprland

Keymasq uses Hyprland's event socket for active-window updates and the command
socket for queries, pointer movement, and compositor dispatch. This gives
Keymasq active-window profiles, pointer-position reads, Hyprland dispatchers,
the **Set Cursor** compositor action, and Hyprland window tags.

Custom compositor actions use Hyprland dispatcher names and arguments. For
example, the GUI presets are built on the same dispatcher mechanism as
`hyprctl dispatch`.

### Niri

Keymasq connects directly to Niri's socket. It uses one connection for the event
stream and another for command requests. This supports active-window profiles,
focused-window queries, window activation by title, and compositor actions.

Common Niri compositor actions use a direct socket path. Custom actions can fall
back to `niri msg action` syntax.

Pointer-position reads use the [slurp-assisted capture path](#slurp-assisted-pointer-capture).
Absolute mouse actions use Keymasq's virtual mouse device.

### COSMIC Wayland

Keymasq uses `ext_foreign_toplevel_list_v1` together with
`zcosmic_toplevel_info_v1` to track the active COSMIC window. This supports
window-aware profiles and active-window queries.

Pointer-position reads use the [slurp-assisted capture path](#slurp-assisted-pointer-capture).
Absolute mouse actions use Keymasq's virtual mouse device. Keymasq does not
currently expose COSMIC compositor actions.

### Sway and Generic wlroots Wayland

The generic Wayland listener works when a compositor exposes
`zwlr_foreign_toplevel_manager_v1`. This protocol lets Keymasq read the active
window's application ID and title, which is enough for window-aware profiles.

Known compatible compositors include Sway, Wayfire, river, labwc, and other
wlroots-based compositors that expose the required protocol. Sway is the primary
tested compositor for this path.

Pointer-position reads use the [slurp-assisted capture path](#slurp-assisted-pointer-capture).
Absolute mouse actions use Keymasq's virtual mouse device. Generic wlroots
support does not include compositor actions because there is no shared
compositor-dispatch API.

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
  current GNOME Shell session. See [GNOME.md](GNOME.md).
- For slurp-assisted pointer capture, make sure `slurp` is installed and can run
  in the current Wayland session.
- If absolute mouse movement is unreliable, use Natural movement where realtime
  cursor feedback is supported. Absolute movement uses normal virtual mouse
  motion and is affected by scaling, output layout, acceleration, and
  sensitivity settings.
- If your supported desktop is not working as described, please open an issue
  with the desktop/compositor name, version, and `keymasq-session` logs.
