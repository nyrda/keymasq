# Changelog

This file tracks notable user-facing changes from the `0.8.0` milestone
forward.

## 0.8.1

### Improved

- Added native cursor positioning for GNOME and X11 so absolute pointer moves
  no longer need to fall back to the virtual mouse path on those desktops.
- Improved GNOME bridge probing and cursor-position handling across more
  `gsettings` layouts, and added broader VM coverage for cursor-setting flows.

### Packaging

- Clarified Fedora Atomic and Bazzite RPM install troubleshooting for the new
  native RPM packaging path.

## 0.8.0

### Improved

- Reworked macro recording and unlock flows with clearer source selection,
  saved recording preferences, and better handling around locked recording
  triggers.
- Let Add Device and Add Input capture continue after unlocking instead of
  forcing the user to restart those flows.

### Packaging

- Split RPM packaging into native Fedora and openSUSE builds and updated the
  related Fedora/Bazzite install path documentation.
