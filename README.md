# Keymasq

[![Tests](https://github.com/nyrda/keymasq/actions/workflows/tests.yml/badge.svg)](https://github.com/nyrda/keymasq/actions/workflows/tests.yml)
[![Package](https://github.com/nyrda/keymasq/actions/workflows/package.yml/badge.svg)](https://github.com/nyrda/keymasq/actions/workflows/package.yml)

Keymasq is an input customization tool for Linux, built around a full input
remapper for keyboards, mice, and game controllers. One tool covers keys,
buttons, clicks, wheels, sticks, and triggers. Remap a single key, turn a
stick into mouse movement, send a keyboard key to a virtual gamepad, or switch
bindings automatically based on the focused app, all from the same layered
profiles.

Keymasq works at the evdev layer, below the compositor, so a mapping behaves
the same in every app.

![Keymasq main window showing a mouse profile with mapped side buttons](docs/assets/screenshots/keymasq_profile.png)

You set things up in the GTK4 GUI. Keymasq stores the configuration as plain
TOML, so you can also edit it by hand or with scripts. The CLI activates and
deactivates profiles and plays macros.

## Features

- Remap keyboard, mouse, and game controller inputs, including mouse side
  buttons and vendor macro keys, without proprietary software
- Hardware masking that blocks other apps from reading physical devices
  while Keymasq keeps access for customization
- Layered profiles that activate and deactivate based on the focused app or
  game, so work, desktop, and per-game bindings stay separate
- Momentary layers with while-held, one-shot, action-count, and timeout modes
  for temporary WASD, HJKL, Home/End, or scroll navigation
- Superkeys that give one key several roles, such as tap for Escape and hold
  for Ctrl
- Combos as chords or multi-step sequences, across one or more devices
- Global hotkeys from those combos in any app, on X11 and Wayland
- Macro recording, timeline editing, playback, and looping, replayable from a
  key, a combo, or the CLI
- Rapidfire for autoclicker and auto-fire mappings
- Repeat Last Action, which re-runs your most recent input from a spare button
- Analog controls that route sticks, triggers, wheels, and axes to mouse,
  keyboard, or gamepad output
- Motion controls that map a PlayStation, Nintendo, or Steam controller's gyro
  or tilt to mouse movement or stick output, with guided calibration to cancel
  drift
- Custom virtual controllers built from a gamepad or flight stick template

## Macro timeline editor

Recorded input opens on a timeline, with separate tracks for keyboard, mouse,
and gamepad actions, and one for mouse movement, waits, and commands. A key
rectangle spans its own press and release.

Select a range to rescale its timing, for example 50% to play it twice as
fast. You can also insert, paste, and trim actions at an exact millisecond.

![Macro timeline editor with a time selection and timing tools](docs/assets/screenshots/macro_edit_selection_timing.png)

See [docs/macro-editor.md](docs/macro-editor.md) for the editor and
[docs/macros.md](docs/macros.md) for recording and playback.

## Desktop support

Keymasq supports X11 and Wayland, including GNOME, KDE Plasma, Hyprland,
Niri, Sway, COSMIC, and other wlroots compositors. Profiles activate and
deactivate based on the focused app or window title, with active profiles
layered together.

GNOME requires the Keymasq GNOME Shell extension for desktop
integration. The GUI guides you through setup.

Desktop actions such as switching workspaces or tiling windows vary
by desktop. See the [support matrix](docs/wayland.md) for details
and [GNOME setup](docs/gnome.md) for the extension.

## Quick start

Install the package for your distro, then
[start the services](#start-the-services). On systemd systems, the AppImage
installs Keymasq and starts the services for you.

### Arch Linux

```bash
yay -S keymasq
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
```

### Fedora

Install from COPR:

```bash
sudo dnf install dnf-plugins-core
sudo dnf copr enable nyrda/keymasq
sudo dnf install keymasq
```

### openSUSE / NixOS

See [docs/install.md](docs/install.md) for full instructions, including the
service setup.

### SteamOS / Steam Deck

For SteamOS and other distros without a native package, use the AppImage. It
installs itself. On systemd systems it also starts the services for you. On
non-systemd systems it installs the core files and writes instructions for
setting up the daemon under your service manager.

Download `Keymasq-*-x86_64.AppImage` from the
[releases page](https://github.com/nyrda/keymasq/releases), then:

```bash
chmod +x Keymasq-*-x86_64.AppImage
./Keymasq-*-x86_64.AppImage --install
```

The installer asks for your password. A stock Steam Deck has no user password
yet, so set one first with `passwd`.

See [docs/steamos.md](docs/steamos.md) for details.

### Start the services

On systemd systems, start the daemon, start the session service, then launch
the GUI:

```bash
sudo systemctl enable --now keymasqd
systemctl --user enable --now keymasq-session
keymasq
```

On non-systemd systems, follow the service-manager instructions generated by
the AppImage installer.

`keymasqd` handles the hardware and needs elevated access to input devices.
`keymasq-session` runs as your normal user and handles your profiles and window
tracking. Keymasq only remaps input while both services are active, so if you
stop either one, your devices behave as they did before. The GUI is for setup
and the CLI is for scripts, and neither has to stay open for your mappings to
work.

## Configuration

You configure Keymasq mainly through the GTK4 GUI. It stores your configuration
as plain TOML in `~/.config/keymasq/`:

- `hardware/` stores per-device metadata
- `profiles/` stores global profiles with one or more device layers
- `superkeys/` stores reusable multi-action key definitions
- `analog_controls/` stores reusable stick and axis behavior
- `settings.toml` and `recording_settings.toml` store user preferences

The daemon keeps saved macros under `/var/lib/keymasq/macros/`. Use the GUI
or CLI to create and edit them. See [docs/hardware.md](docs/hardware.md) for
hardware configuration and [docs/profiles.md](docs/profiles.md) for the
profile format and merge rules.

## Security

Only `keymasq-session` talks to the privileged daemon. The GUI and CLI talk to
that session broker and never open kernel input devices themselves.

Macro recording requires explicit opt-in. Capture features require a temporary,
process-bound unlock by default.

See [docs/security.md](docs/security.md) for details.

## Documentation

The full documentation is rendered at
[keymasq.tools](https://keymasq.tools/), and the sources are in
[docs/](docs/).

- [Getting started](docs/getting-started.md), your first remap
- [Installation guide](docs/install.md), every distro and the service setup
- [Profiles](docs/profiles.md) and [Actions](docs/actions.md), the core model
- [Game controller support](docs/gamepad.md), remapping, analog controls, and
  virtual gamepads
- [Motion controls](docs/motion-controls.md), gyro and tilt setup, calibration,
  and outputs
- [CLI reference](docs/cli.md)
- [Troubleshooting](docs/troubleshooting.md)

## Contributing

Contributions are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md) for
guidelines and [DEVELOPMENT.md](DEVELOPMENT.md) for the Nix-based development
environment.

## Support

Report bugs and ask questions in
[GitHub issues](https://github.com/nyrda/keymasq/issues).
[SUPPORT.md](SUPPORT.md) lists what to include in a report.

## License

MIT License. See [LICENSE](LICENSE).
