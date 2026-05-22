# Keymasq

[![Tests](https://github.com/nyrda/keymasq/actions/workflows/tests.yml/badge.svg)](https://github.com/nyrda/keymasq/actions/workflows/tests.yml)
[![Package](https://github.com/nyrda/keymasq/actions/workflows/package.yml/badge.svg)](https://github.com/nyrda/keymasq/actions/workflows/package.yml)

Keymasq is a Linux input remapper for keyboards, mice, and game controllers. It
lets you remap your keys, buttons, clicks, wheels, and controller inputs from
one place.

Layered profiles can switch automatically based on the app or window in focus,
so your bindings can change with the app or game you are using. Superkeys make
one key or button multi-role. Combos can span keyboards, mice, and controllers,
turning cross-device chords or short input sequences into shortcuts for anything
Keymasq can do. Macros record, edit, and replay longer input sequences.

Virtual keyboard, mouse, and gamepad output let you route input across device
types: turn a stick into mouse movement, a keyboard key into a gamepad button,
or build autoclicker and auto-fire setups with rapidfire. The GTK4 GUI handles
everyday setup and configuration, while user config stays in plain TOML for hand
editing and tooling. The CLI is available for profile control, macro playback,
and scripted workflows.

## Features

- Remap keyboard, mouse, and game controller inputs
- Full profile layering for base layouts and temporary layers
- Momentary profile activation with while-held, one-shot, action-count, and timeout modes
- Automatic profile activation based on the focused app or window
- Macro recording, timeline editing, playback, and looping
- Rapidfire actions for autoclicker and auto-fire setups
- Superkeys for one-button multi-role or multi-output behavior
- Combos for single-device, cross-device, and multi-step chords or sequences
- Analog controls for controller sticks, triggers, wheels, and axes
- Virtual keyboard, mouse, and gamepad output

## Screenshot

![Keymasq main profile view](docs/assets/screenshots/keymasq_profile.png)

## Use Cases

**Replace vendor utilities**
- Remap mouse side buttons, controller buttons, and macro keys without
  proprietary software

**Tune controllers for games**
- Route sticks, triggers, wheels, and axes to mouse, keyboard, or gamepad output
- Keep game-specific controller layouts in profiles

**Turn spare buttons into workflows**
- Make Caps Lock act as Escape, a modifier, or a superkey
- Trigger macros, commands, profile changes, or several outputs from one press

**Automate repeated input**
- Build autoclickers and auto-fire mappings with rapidfire
- Replay recorded or hand-built input sequences from keys, combos, or the CLI

**Switch by app**
- Auto-switch bindings when apps or games gain focus
- Keep separate profiles for work, desktop navigation, and games

**Navigate without leaving home row**
- Hold Caps Lock or a thumb button for WASD, Vim-style HJKL, Home/End, or scroll navigation
- Release the button to return instantly to your normal layout

**Build richer triggers**
- Use superkeys for one-button multi-role behavior
- Use combos for single-device or cross-device shortcut chords and sequences

## Desktop Support

Keymasq works on X11 and on major Wayland desktops, including GNOME, KDE
Plasma, Hyprland, Niri, COSMIC, and wlroots-based compositors such as Sway.

Window-aware profiles, pointer capture, and compositor actions depend on what
your desktop session exposes to Keymasq. GNOME requires the Keymasq GNOME
Shell bridge extension.

See [docs/WAYLAND.md](docs/WAYLAND.md) for compositor details and
[docs/GNOME.md](docs/GNOME.md) for GNOME setup.

## Quick Start

Keymasq uses two services: `keymasqd` handles the hardware (it needs elevated access to
input devices), and `keymasq-session` handles your profiles and window tracking
as your normal user. If either service is stopped, your devices work normally;
Keymasq only remaps input when both services are active.

### Arch Linux

```bash
yay -S keymasq
sudo systemctl enable --now keymasqd
systemctl --user enable --now keymasq-session
keymasq
```

### Debian

Also works on Ubuntu, Linux Mint, Pop!_OS, PikaOS, and other
Debian/Ubuntu-based distros.

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

### Fedora

COPR is the preferred Fedora channel:

```bash
sudo dnf install dnf-plugins-core
sudo dnf copr enable nyrda/keymasq
sudo dnf install keymasq
sudo systemctl enable --now keymasqd
systemctl --user enable --now keymasq-session
keymasq
```

### openSUSE / NixOS

See [docs/INSTALL.md](docs/INSTALL.md) for full instructions.

## Configuration

Keymasq is primarily configured through the GTK4 GUI. User configuration is
stored in `~/.config/keymasq/`:

```text
~/.config/keymasq/
├── hardware/
├── profiles/
├── superkeys/
├── analog_controls/
├── recording_settings.toml
└── settings.toml
```

- `hardware/` stores per-device metadata
- `profiles/` stores global profiles with one or more device layers
- `superkeys/` stores reusable multi-action key definitions
- `analog_controls/` stores reusable stick and axis behavior
- `settings.toml` and `recording_settings.toml` store user preferences

Saved macros are daemon-managed under `/var/lib/keymasq/macros/`; use the GUI
or CLI to create and edit them. See [docs/PROFILES.md](docs/PROFILES.md) for
the profile format and merge rules.

## Security

Keymasq uses a double-broker design:

- `keymasq-session` is the only client that talks to `keymasqd`
- GUI and CLI clients talk to the session broker, not directly to kernel input
  devices
- Recording and capture features are guarded by an unlock flow and owner checks

See [docs/SECURITY.md](docs/SECURITY.md) for details.

## Documentation

- Getting started: [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md)
- Installation guide: [docs/INSTALL.md](docs/INSTALL.md)
- Game controller support: [docs/GAMEPAD.md](docs/GAMEPAD.md)
- Profile system: [docs/PROFILES.md](docs/PROFILES.md)
- Actions explained: [docs/ACTIONS.md](docs/ACTIONS.md)
- Superkeys system: [docs/SUPERKEYS.md](docs/SUPERKEYS.md)
- Combo system: [docs/COMBOS.md](docs/COMBOS.md)
- Macro system: [docs/MACROS.md](docs/MACROS.md)
- GNOME setup: [docs/GNOME.md](docs/GNOME.md)
- CLI reference: [docs/CLI.md](docs/CLI.md)
- Performance: [docs/PERFORMANCE.md](docs/PERFORMANCE.md)
- Troubleshooting: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)
- Security model: [docs/SECURITY.md](docs/SECURITY.md)

## Support

Found a bug or have a question?
[Open a GitHub issue](https://github.com/nyrda/keymasq/issues).

## License

MIT License. See [LICENSE](LICENSE).
