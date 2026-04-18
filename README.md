# Keymasq

[![Tests](https://github.com/nyrda/keymasq/actions/workflows/tests.yml/badge.svg)](https://github.com/nyrda/keymasq/actions/workflows/tests.yml)
[![Quality](https://github.com/nyrda/keymasq/actions/workflows/quality.yml/badge.svg)](https://github.com/nyrda/keymasq/actions/workflows/quality.yml)
[![Package](https://github.com/nyrda/keymasq/actions/workflows/package.yml/badge.svg)](https://github.com/nyrda/keymasq/actions/workflows/package.yml)

Keymasq is a Linux input remapper with a simple graphical interface.

It supports keyboard, mouse, and gamepad remapping with layered profiles,
window-aware activation, macros, superkeys, and combos. It can also be used
for practical workflows like a Linux autoclicker, game auto-fire, app-specific
shortcuts, and multi-step automation.

## Features

- Remap keyboard, mouse, and gamepad inputs
- Layer per-device mappings in global profiles
- Auto-activate profiles based on the active window
- Record, edit, and play macros
- Build autoclicker and auto-fire setups with rapidfire actions or looped macros
- Assign multiple actions to a single key with superkeys
- Trigger actions with multi-input combos

## Common Use Cases

- Build a Linux autoclicker from a rapidfire mouse mapping or a looped macro
- Create app-specific or game-specific profiles that enable automatically
- Record repeated input sequences and replay them from a key, combo, or CLI command
- Turn one button into tap, hold, double-tap, or one-to-many actions with superkeys
- Trigger shortcuts or macros from multi-input combos across devices


## Screenshot

Main profile view in the GTK4 application:

![Keymasq main profile view](docs/assets/screenshots/keymasq_profile.png)

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


## Architecture

```text
┌─────────────────┐
│  GUI / CLI      │  `keymasq`
└────────┬────────┘
         │ session socket
┌────────▼────────┐
│ keymasq-session│  per-user broker
└────────┬────────┘
         │ daemon socket
┌────────▼────────┐
│   keymasqd     │  privileged daemon
└─────────────────┘
```

- `keymasqd` handles evdev/uinput access, macro storage, recording, and device
  control
- `keymasq-session` owns compositor integration, profile resolution, and the
  user-session boundary
- `keymasq` opens the GUI by default in a desktop session and exposes CLI
  subcommands when invoked with arguments

The daemon runs as a dedicated `keymasq` system user, not as root.

## Quick Start

Packaged installs are the recommended path for normal use.

## Install

Installation instructions for supported distributions and NixOS are in
[docs/INSTALL.md](docs/INSTALL.md).

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

## Components

| Command | Description |
| --- | --- |
| `keymasqd` | Privileged system daemon |
| `keymasq-session` | Per-user session broker |
| `keymasq` | GUI by default, CLI with subcommands |
| `keymasq-record` | Privileged recording helper used by the GUI |

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

## License

MIT License. See [LICENSE](LICENSE).
