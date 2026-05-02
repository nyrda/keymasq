# Changelog

This file tracks notable user-facing changes from the `0.8.0` milestone
forward.

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
