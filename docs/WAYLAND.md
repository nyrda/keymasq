# Generic Wayland Support

Keyforge supports any Wayland compositor that implements the
`zwlr_foreign_toplevel_manager_v1` protocol. This protocol lets Keyforge read
the active window's application ID and title, which is required for
window-aware profile activation.

## Tested Compositors

| Compositor | Status  |
| ---------- | ------- |
| Sway       | Tested  |
| Niri       | Tested  |

## Other Compositors

Any compositor that implements this protocol should work out of the box. Known
examples include Wayfire, river, labwc, and wlroots-based compositors like dwl. If your compositor supports
the protocol and you run into problems, please open an issue.

## Compositors With Dedicated Support

Some compositors have their own integration in Keyforge and do **not** use
this protocol:

- **Hyprland** — uses Hyprland IPC sockets
- **COSMIC** — uses `ext_foreign_toplevel_list_v1` and `zcosmic_toplevel_info_v1`
- **KDE Plasma** — uses an injected KWin script over session D-Bus
- **GNOME** — uses a GNOME Shell plugin; see [GNOME.md](GNOME.md)

On KDE Plasma, Keyforge's compositor actions run through the same KWin script bridge instead of shelling out to `qdbus` or external helpers.
