# Keymasq

[![Tests](https://github.com/nyrda/keymasq/actions/workflows/tests.yml/badge.svg)](https://github.com/nyrda/keymasq/actions/workflows/tests.yml)
[![Package](https://github.com/nyrda/keymasq/actions/workflows/package.yml/badge.svg)](https://github.com/nyrda/keymasq/actions/workflows/package.yml)

Remap keys, buttons, and gamepad inputs on Linux—with per-app profiles,
macros, and graphical setup and configuration.

Keymasq supports keyboard, mouse, and gamepad remapping with layered profiles,
window-aware activation, macros, superkeys, and combos. Use it for workflows
like a Linux autoclicker, game auto-fire, app-specific shortcuts, and
multi-step automation.

## Features

- Remap keyboard, mouse, and gamepad inputs
- Layer per-device mappings in global profiles
- Auto-activate profiles based on the active window
- Record, edit, and play macros
- Build autoclicker and auto-fire setups with rapidfire actions or looped macros
- Superkeys: one key fires multiple actions, or different actions for tap vs hold vs double-tap
- Combos: trigger actions or superkeys from any input combination—even across devices

## Screenshot

![Keymasq main profile view](docs/assets/screenshots/keymasq_profile.png)

## Use Cases

**Remap without vendor software**
- Remap mouse side buttons and thumb buttons without proprietary apps

**Reclaim underused keys**
- Turn Caps Lock into Escape, a modifier, or both at the same time with a superkey
- Repurpose function keys or other rarely-used keys

**Automate repetitive inputs**
- Build a Linux autoclicker with rapidfire or a looped macro
- Record input sequences and replay them from a key, combo, or CLI command
- Fire multiple actions from a single button press

**Per-app profiles**
- Auto-switch layouts when specific apps or games gain focus
- Keep separate bindings for different workflows
- Game-specific mappings that only activate while playing

**Advanced input options**
- Superkeys: assign different actions to tap, hold, double-tap, or tap-hold
- Combos: trigger actions from multi-key or cross-device combinations
- Multi-step combos: replace awkward shortcuts like Meta+Shift+X with easier sequences like Meta+X → 1

## Desktop Support

Keymasq supports current Linux desktop environments on both Wayland and X11.

- **Wayland**: supported on Hyprland, Niri, KDE Plasma, COSMIC, GNOME, and
  wlroots-based compositors that expose `zwlr_foreign_toplevel_manager_v1`
  (for example Sway, Wayfire, river, and labwc)
- **X11**: supported on standard X11 desktop sessions
- **GNOME**: requires the GNOME Shell bridge extension; see
  [docs/GNOME.md](docs/GNOME.md)
- **Wayland compositor details**: see [docs/WAYLAND.md](docs/WAYLAND.md)

Keymasq auto-detects the current session and uses the appropriate compositor
integration or Wayland fallback at runtime.


## Quick Start

### Arch Linux

```bash
yay -S keymasq
sudo systemctl enable --now keymasqd
systemctl --user enable --now keymasq-session
keymasq
```

### Debian

Also works on Ubuntu, Linux Mint, Pop!_OS, PikaOS, and other apt-based distros.

```bash
curl -fsSL https://repo.keymasq.tools/gpg-key.asc \
  | sudo gpg --dearmor -o /etc/apt/keyrings/keymasq.gpg
echo "deb [signed-by=/etc/apt/keyrings/keymasq.gpg arch=all] https://repo.keymasq.tools/debian stable main" \
  | sudo tee /etc/apt/sources.list.d/keymasq.list
sudo apt update && sudo apt install keymasq
sudo systemctl enable --now keymasqd
systemctl --user enable --now keymasq-session
keymasq
```

### Fedora / openSUSE / NixOS

See [docs/INSTALL.md](docs/INSTALL.md) for full instructions.

## Configuration

Keymasq is primarily configured through the GTK4 GUI. Configuration data is
stored in `~/.config/keymasq/`:

```text
~/.config/keymasq/
├── hardware/
│   └── <hardware_id>.toml
└── profiles/
    └── <profile_name>.toml
```

- `hardware/` stores per-device metadata
- `profiles/` stores global profiles with one or more device layers

See [docs/PROFILES.md](docs/PROFILES.md) for the profile format and merge
rules.

## Security

Keymasq uses a double-broker design:

- `keymasq-session` is the only client that talks to `keymasqd`
- GUI and CLI clients talk to the session broker, not directly to kernel input
  devices
- Recording and capture features are guarded by an unlock flow and owner checks

See [docs/SECURITY.md](docs/SECURITY.md) for details.

## Documentation

- Installation guide: [docs/INSTALL.md](docs/INSTALL.md)
- Profile system: [docs/PROFILES.md](docs/PROFILES.md)
- Actions explained: [docs/ACTIONS.md](docs/ACTIONS.md)
- Superkeys system: [docs/SUPERKEYS.md](docs/SUPERKEYS.md)
- Combo system: [docs/COMBOS.md](docs/COMBOS.md)
- Macro system: [docs/MACROS.md](docs/MACROS.md)
- GNOME setup: [docs/GNOME.md](docs/GNOME.md)
- Troubleshooting: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)
- Security model: [docs/SECURITY.md](docs/SECURITY.md)

## Support

Found a bug or have a question?
[Open a GitHub issue](https://github.com/nyrda/keymasq/issues).

## License

MIT License. See [LICENSE](LICENSE).
