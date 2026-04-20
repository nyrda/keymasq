# Changelog

All notable changes to this project will be documented in this file.

## [0.7.0] - 2026-04-20

### Added

- Added `keymasq status --json` so automation and scripts can inspect runtime
  state without scraping human-readable CLI output.

### Improved

- Routed absolute mouse move actions through compositor-aware cursor position
  setters so screen-position workflows behave consistently across session
  backends.
- Tightened slurp capture handling and macro editor updates for point-capture
  and absolute-position editing flows.

### Changed

- Removed obsolete CLI device and hardware commands.
- Split GitHub Actions checks into faster static, typecheck, non-GUI, and GUI
  paths with leaner cached Nix shells.

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
