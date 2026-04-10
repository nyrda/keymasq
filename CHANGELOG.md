# Changelog

All notable changes to this project will be documented in this file.

## [0.3] - 2026-04-10

### Added

- Add first-class Niri compositor support, including focused-window tracking,
  window switching actions, slurp compatibility updates, and raw
  `niri msg action` dispatch from Keyforge actions.
- Expand superkeys into explicit pattern and overload modes with ordered action
  bundles across the GUI, runtime, storage, and docs.

### Changed

- Enforce the newer superkey schema consistently, tighten overload validation,
  skip invalid nested superkeys during execution, and show fuller action labels
  with rapidfire state in the GUI.
- Make superkey bindings value-aware so the runtime can distinguish the same
  evdev code by value or direction, including wheel-style inputs.
- Split stable tagged releases from manual prerelease packaging so GitHub
  releases, signing, and downstream publishing only run from stable `v*` tags.

### Fixed

- Restore the superkey dialog construction path and the macro manager close
  handler.

## [0.2] - 2026-04-07

### Changed

- Seed a permanent editable `Default` profile on first startup so newly added
  devices can be remapped immediately.
- Hide unsupported touchpads from the Add Device flow.
- Stop offering wheel-only pseudo inputs during device setup and clean up
  deleted button mappings more predictably.

## [0.1.0] - 2026-03-28

### Added

- Initial public release of Keyforge.
- GTK4 desktop application and CLI for managing remaps, profiles, macros,
  superkeys, and combos.
- Three-process runtime split across `keyforged`, `keyforge-session`, and
  `keyforge` for device access, session/compositor integration, and GUI/CLI
  control.
- Global profile system with per-device layers and focused-window-based profile
  activation.
- Keyboard, mouse, and gamepad remapping support, including macro recording and
  playback.
- Superkeys for assigning multiple actions to one input and multi-step combo
  actions.
- Desktop integration for Hyprland, KDE Plasma, COSMIC, GNOME (via the GNOME
  Shell bridge), generic Wayland with
  `zwlr_foreign_toplevel_manager_v1`, and X11.
- Packaged install paths for Nix/NixOS, Arch Linux, Debian/Ubuntu/Mint,
  Fedora, and openSUSE.
- Security model based on a dedicated `keyforge` system user, session-brokered
  daemon access, and guarded recording/capture flows.
