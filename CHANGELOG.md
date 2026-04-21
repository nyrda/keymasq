# Changelog

All notable changes to this project will be documented in this file.

Before the first public release, Keymasq used internal milestone versions.
Those milestones are not listed here as public releases. The notes below
summarize user-visible changes accumulated toward the first public release.

## [0.8.0] - 2026-04-21

### Added

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
- Expanded superkeys with richer pattern and overload modes, so one input can
  drive more complex tap, hold, and multi-action behaviors.
- Added support for using superkey actions inside combos.
- Added first-class Niri support, including window-aware profile handling and
  Niri actions in the GUI.
- Added `keymasq status --json` so automation and scripts can inspect runtime
  state without scraping human-readable CLI output.
- Added saved macro recording preferences with separate remapped-output and
  direct-input source selection.

### Improved

- Made combos more reliable, especially when restoring remapped trigger keys,
  handling held keys, suppressing stray repeats, and resolving overlapping
  starts.
- Improved the combo editor flow and superkey labels so advanced mappings are
  easier to understand and manage.
- Made superkeys work better with value-based inputs such as wheel directions.
- Routed absolute mouse move actions through compositor-aware cursor position
  setters so screen-position workflows behave consistently across session
  backends.
- Tightened slurp capture handling and macro editor updates for point-capture
  and absolute-position editing flows.
- Reworked macro recording and macro manager flows with clearer source
  selection, more direct controls, and cleaner recording unlock handling.
- Let Add Device and Add Input capture flows continue cleanly after unlocking.

### Changed

- Seed a permanent editable `Default` profile on first startup so newly added
  devices can be remapped immediately.
- Hide unsupported touchpads from the Add Device flow.
- Stop offering wheel-only pseudo inputs during device setup and clean up
  deleted button mappings more predictably.
- Removed obsolete CLI device and hardware commands.
