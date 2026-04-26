# Changelog

This file tracks notable user-facing changes from the `0.8.0` milestone
forward.

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
