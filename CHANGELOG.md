# Changelog

All notable changes to this project will be documented in this file.

## [0.5.0] - 2026-04-17

### Changed

- Normalized the packaged desktop application identity to all-lowercase
  `tools.keymasq.keymasq` across the GTK application ID, desktop file ID,
  AppStream launchable entry, and installed icon namespace.
- Aligned packaging manifests, generated package outputs, post-install hooks,
  and smoke tests with the lowercase desktop asset names.

### Fixed

- Kept the About dialog icon and launcher metadata aligned with the shipped
  lowercase application identity on installed packages.
- Preserved packaging smoke coverage so launcher assets still track the GUI
  application identity after the rename.

## [0.3] - 2026-04-10

### Added

- Expanded superkeys with richer pattern and overload modes, so one input can
  drive more complex tap, hold, and multi-action behaviors.
- Added support for using superkey actions inside combos.
- Added first-class Niri support, including window-aware profile handling and
  Niri actions in the GUI.

### Improved

- Made combos more reliable, especially when restoring remapped trigger keys,
  handling held keys, suppressing stray repeats, and resolving overlapping
  starts.
- Improved the combo editor flow and superkey labels so advanced mappings are
  easier to understand and manage.
- Made superkeys work better with value-based inputs such as wheel directions.

## [0.2] - 2026-04-07

### Changed

- Seed a permanent editable `Default` profile on first startup so newly added
  devices can be remapped immediately.
- Hide unsupported touchpads from the Add Device flow.
- Stop offering wheel-only pseudo inputs during device setup and clean up
  deleted button mappings more predictably.

## [0.1.0] - 2026-03-28

### Added

- Initial public release of Keymasq.
- GTK4 desktop application and CLI for managing remaps, profiles, macros,
  superkeys, and combos.
- Three-process runtime split across `keymasqd`, `keymasq-session`, and
  `keymasq` for device access, session/compositor integration, and GUI/CLI
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
- Security model based on a dedicated `keymasq` system user, session-brokered
  daemon access, and guarded recording/capture flows.
