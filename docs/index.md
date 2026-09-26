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

- [Install](install.md) — package installs for Arch, Debian/Ubuntu, Fedora,
  and NixOS, plus a from-source path.
- [Getting Started](getting-started.md) — open Keymasq, add a device, and
  create your first remap after installing.
- [Hardware Configuration](hardware.md) — hardware IDs, event devices, source
  controls, and detection methods.
- [Profiles](profiles.md) — the layered, window-aware profile model. The other
  features all attach to profiles.
- [Actions](actions.md) — what each mapping can do.
- [Examples](examples.md) — short recipes for common tasks.
- [Macros](macros.md) — create, record, and play input sequences, including autoclicker loops.

## User guide

- [Superkeys](superkeys.md) — one key fires multiple actions, or different actions for tap, hold, and double-tap.
- [Combos](combos.md) — trigger actions or superkeys from any input combination, even across devices.
- [Rollover groups](rollover.md) — let only one of several held keys drive its mapping, for example two keys on one stick axis.
- [Macros](macros.md) — creation, recording, triggers, and playback settings.
- [Macro timeline editor](macro-editor.md) — add actions, select and reuse sections, and adjust timing.
- [Gamepad](gamepad.md) — controller remapping, analog controls, and virtual gamepads.
- [Motion Controls](motion-controls.md) — controller gyro setup, normalization, and outputs.
- [Device Inspector](device-inspector.md) — inspect final mappings, raw events,
  and configured analog inputs for one device.

## Desktop support

- [Wayland](wayland.md) — compositor integrations and fallbacks.
- [GNOME](gnome.md) — Shell bridge extension setup.

## Reference

- [Security model](security.md) — daemon/session split, capture unlock flow,
  owner checks.
- [Troubleshooting](troubleshooting.md) — diagnostics for common problems.

---

Keymasq is MIT-licensed. The source is on
[GitHub](https://github.com/nyrda/keymasq).
