# Keymasq

Remap keys, buttons, and gamepad inputs on Linux—with per-app profiles,
macros, and graphical setup and configuration.

Keymasq supports keyboard, mouse, and gamepad remapping with layered profiles,
window-aware activation, macros, superkeys, and combos. Use it for workflows
like a Linux autoclicker, game auto-fire, app-specific shortcuts, and
multi-step automation.

## Common use cases

- Build a Linux autoclicker with a rapidfire mouse mapping or a looped macro
- Create app-specific and game-specific profiles that activate by window
- Record and replay repeated workflows with macros
- Put tap, hold, double-tap, and one-to-many behavior on a single key with superkeys
- Trigger macros or shortcuts from cross-device combos like keyboard + mouse button

## Start here

- [Install](INSTALL.md) — package installs for Arch, Debian/Ubuntu, Fedora,
  and NixOS, plus a from-source path.
- [Profiles](PROFILES.md) — the layered, window-aware profile model that
  everything else builds on.
- [Actions](ACTIONS.md) — what each mapping can do.
- [Macros](MACROS.md) — record, edit, and play back input sequences, including autoclicker-style loops.

## User guide

- [Superkeys](SUPERKEYS.md) — one key fires multiple actions, or different actions for tap vs hold vs double-tap.
- [Combos](COMBOS.md) — trigger actions or superkeys from any input combination, even across devices.
- [Macros](MACROS.md) — record, edit, and play back input sequences.
- [Gamepad](GAMEPAD.md) — controller remapping, including analog triggers.

## Desktop support

- [Wayland](WAYLAND.md) — compositor integrations and fallbacks.
- [GNOME](GNOME.md) — Shell bridge extension setup.

## Reference

- [Security model](SECURITY.md) — daemon/session split, unlock flow,
  owner checks.
- [Troubleshooting](TROUBLESHOOTING.md) — diagnostics for common problems.

---

Keymasq is MIT-licensed. Source is on
[GitHub](https://github.com/nyrda/keymasq).
