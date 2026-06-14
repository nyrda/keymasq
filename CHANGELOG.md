# Changelog

## 0.18.0 - 2026-06-14

### Improved

- Expanded game controller support with remappable sticks, triggers, wheels,
  generic axes, and configurable virtual gamepad outputs.
- Added reusable Analog Controls for routing analog inputs to mouse movement,
  digital actions, or tuned gamepad output.
- Added temporary profile layers with while-held, one-shot, action-count, and
  timeout lifetimes.
- Added Repeat Last Action for replaying the last repeatable input or action
  with category filters.
- Added Natural mouse move actions for reliable absolute cursor positioning
  through realtime cursor feedback.
- Reworked macro recording around explicit temporary slots that can be
  replayed, saved later, or deleted, with recording opt-in managed from
  Settings.
- Improved the GTK app with reorderable/restored tabs, fuzzy search in selector
  dialogs, and runtime inspectors for devices and combos.

## 0.17.1

### Fixed

- Added a confirmation prompt when closing the Super Keys dialog with unsaved
  changes.

## 0.17.0

### Improved

- Added on-press and on-release pulse actions to overload superkeys.

## 0.16.0

### Improved

- Added a live diagnostics dialog in the GUI for monitoring keymasqd latency
  snapshots.
- Diagnostics now support category filtering, reset controls, a documentation
  link, and live refresh status in the GUI, alongside matching CLI category
  filters.
- Package upgrades now restart services.

## 0.15.0

### Improved

- Added a feedback dialog for submitting bugs, ideas, or questions, with
  optional diagnostics.
- Moved profile settings into a dedicated Adwaita dialog with per-device grab
  toggles, an active-profile summary, and quicker access from the profile
  picker.
- Improved CLI responsiveness by deferring heavy imports and offloading macro
  compilation from the session thread.
- Refreshed device mapping cards with clearer state styling, better text
  handling, and general window polish.

## 0.14.0

### Fixed

- Configuration now honors `XDG_CONFIG_HOME` instead of always using the
  default user config directory.

### Packaging

- First public release.
- Fedora COPR and AUR packaging are now available.

## 0.13.2

### Improved

- Added small cosmetic polish alongside internal fixes and restructuring.

## 0.13.1

### Fixed

- Fixed documentation links so they point to version-aware keymasq.tools URLs.

## 0.13.0

### Improved

- Added profile lifecycle macros that can run when a profile activates or
  deactivates.
- Macro timelines can now include compositor actions.
- The GUI now warns before primary mouse button remaps instead of blocking
  them outright.
- Improved key selector, input picker, device tab, macro dialog, and Super Key
  dialog layouts, including grave key support in keyboard layouts.

## 0.12.0

### Improved

- Type actions can now use a 0 ms hold time.
- Added a dedicated media remapping tab.

## 0.11.0

### Improved

- Added guided GNOME setup dialogs with DBus bridge detection and clearer
  recovery when GNOME integration needs setup, restart, or logout.
- Added a Ctrl+Alt+Esc emergency combo: one tap cancels macro playback, and a
  double tap triggers an emergency reset.
- Improved macro recording saves with pending-save protection and compressed
  macro storage.
- Newly created profiles are now selected immediately and applied to the
  running session.
- Rapidfire mappings now support zero-hold timing and clamp timing fields to
  valid values.

## 0.10.1

### Improved

- Added device renaming in device tabs.

## 0.10.0

### Improved

- Added ad hoc macro CLI commands for one-off macro playback and compilation.
- Removed the extra dialog before the recording Polkit prompt.

## 0.9.0

### Improved

- Added scroll wheel remapping support
- Added scroll wheel combo support
- Improved macro recording and saving flows
- Moved superkey mappings into a dedicated selector tab for clearer editing.
- Added Unicode support for type macros

## 0.8.1

### Improved

- Added native cursor positioning for GNOME and X11 so absolute pointer moves
  work directly on those desktops instead of falling back to the virtual mouse
  path.

## 0.8.0

### Improved

- Reworked macro recording and unlock flows with clearer source selection,
  saved recording preferences, and better handling around locked recording
  triggers.
- Let Add Device and Add Input capture continue after unlocking instead of
  forcing the user to restart those flows.
