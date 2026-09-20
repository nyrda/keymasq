# Keymasq

Keymasq remaps keyboards, mice, and game controllers on Linux. You configure
it in a GUI. Profiles are layered and can activate when a given window has
focus. It also has macros, superkeys, and combos. Use it to build an
autoclicker, game auto-fire, app-specific shortcuts, and multi-step automation.

## Common use cases

- Build a Linux autoclicker with a rapidfire mouse mapping or a looped macro
- Create app-specific and game-specific profiles that activate by window
- Record and replay repeated workflows with macros
- Put tap, hold, double-tap, and one-to-many behavior on a single key with superkeys
- Trigger macros or shortcuts from cross-device combos like keyboard + mouse button

## Start here

- [Install](INSTALL.md) — package installs for Arch, Debian/Ubuntu, Fedora,
  and NixOS, plus a from-source path.
- [Getting Started](GETTING_STARTED.md) — open Keymasq, add a device, and
  create your first remap after installing.
- [Hardware Configuration](HARDWARE.md) — hardware IDs, event devices, source
  controls, and detection methods.
- [Profiles](PROFILES.md) — the layered, window-aware profile model. The other
  features all attach to profiles.
- [Actions](ACTIONS.md) — what each mapping can do.
- [Examples](EXAMPLES.md) — short recipes for common tasks.
- [Macros](MACROS.md) — create, record, and play input sequences, including autoclicker loops.

## User guide

- [Superkeys](SUPERKEYS.md) — one key fires multiple actions, or different actions for tap, hold, and double-tap.
- [Combos](COMBOS.md) — trigger actions or superkeys from any input combination, even across devices.
- [Macros](MACROS.md) — creation, recording, triggers, and playback settings.
- [Macro timeline editor](MACRO_EDITOR.md) — add actions, select and reuse sections, and adjust timing.
- [Gamepad](GAMEPAD.md) — controller remapping, analog controls, and virtual gamepads.
- [Motion Controls](MOTION_CONTROLS.md) — controller gyro setup, normalization, and outputs.
- [Device Inspector](DEVICE_INSPECTOR.md) — inspect final mappings, raw events,
  and configured analog inputs for one device.

## Desktop support

- [Wayland](WAYLAND.md) — compositor integrations and fallbacks.
- [GNOME](GNOME.md) — Shell bridge extension setup.

## Reference

- [Security model](SECURITY.md) — daemon/session split, capture unlock flow,
  owner checks.
- [Troubleshooting](TROUBLESHOOTING.md) — diagnostics for common problems.

---

Keymasq is MIT-licensed. The source is on
[GitHub](https://github.com/nyrda/keymasq).
