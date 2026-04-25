# Generic Wayland Support

Keymasq supports any Wayland compositor that implements the
`zwlr_foreign_toplevel_manager_v1` protocol. This protocol lets Keymasq read
the active window's application ID and title, which is required for
window-aware profile activation.

## Compatible Compositors

Any compositor that implements the protocol should work. Known examples include
Sway, Wayfire, river, labwc, and other wlroots-based compositors. Sway is the
primary tested compositor for this integration.

If your compositor supports the protocol and you run into problems, please
open an issue.

## Compositors With Dedicated Support

Some compositors have their own integration in Keymasq and do **not** use
this protocol:

- **Hyprland** — uses Hyprland IPC sockets
- **Niri** — uses Niri IPC event and command sockets
- **COSMIC** — uses `ext_foreign_toplevel_list_v1` and `zcosmic_toplevel_info_v1`
- **KDE Plasma** — uses an injected KWin script over session D-Bus
- **GNOME** — uses a GNOME Shell plugin; see [GNOME.md](GNOME.md)

On KDE Plasma, GNOME, and Niri, Keymasq's compositor actions run through the
listener-specific bridge/socket instead of shelling out to external helpers.
