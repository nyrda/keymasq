# Changelog

This file tracks notable user-facing changes from the `0.8.0` milestone
forward.

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
