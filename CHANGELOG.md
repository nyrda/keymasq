# Changelog

## 0.21.0 - 2026-10-14

### Added

- Added rollover groups: keys or buttons, on one or more devices, where only
  the winning held member drives its mapping. Covers SOCD (Snap Tap) modes and
  two keys on one stick axis, with last pressed, first pressed, cancel out, and
  fixed priority winners.
- Added per-profile pointer movement for mice: scale, invert, or swap mouse
  movement, or drive controller axes from it in velocity or position mode.
- Added Steam Deck stick and trackpad touch inputs through a new HID-BPF native
  input backend. They work as buttons in mappings, combos, and superkeys.
- Added `keymasq masking` to manage hardware masks from the CLI, with
  `--json` output.
- Added a **Block physical keyboard** macro option that holds back keyboard
  keys from physical input while the macro plays.
- Added the `<macro:NAME>` inline control to type macros, which runs a saved
  macro and waits for it before typing on.
- Added Sway compositor actions and window tracking over Sway IPC.
- Added layer-shell focus tracking on Hyprland and a `layer` window rule
  field, so profiles can react to launchers such as fuzzel and walker.
- Added an admin policy key, `[recording_guard] macro_recording_allowed`, to
  turn off the macro recorder.
- Added `input_latency` and a `stalls` diagnostics category
  (`keymasq diagnostics on --include stalls`) that attributes input delays to
  garbage collection, event-loop blocks, daemon commands, and worker jobs.

### Improved

- `keymasqd` now handles grabbed input with about half the per-event work, and
  profile changes, combo updates, worker threads, garbage collection, recording
  playback, and diagnostics no longer hold up input for several milliseconds.
- Config and macro edits only reapply mappings to devices whose mapping
  changed, so held keys, analog state, and motion state on other devices are
  kept. The GUI no longer stalls after remaps and saves.
- Natural cursor moves run in the background, including inside Super Keys,
  analog thresholds, and combos, so other keys and the emergency reset keep
  working during the move. Actions after the move in the same list still wait
  for it.
- Keymasq regrabs a device after a read error or a fast replug at the same
  node instead of leaving it released.
- Hardware masking no longer requires the capture unlock.
- The key selector labels its keyboard with the configured layout and uses
  the layout's ANSI or ISO geometry.
- Profile loading now rejects malformed pointer movement and rollover groups
  instead of silently replacing them with defaults.

### Fixed

- Fixed an AppImage update leaving an incomplete runtime when the disk ran
  full. Systems where a 0.20.0 update already failed this way need one
  `--install` from the new AppImage.
- Fixed the Brotway GUI crashing on start in AppImages built against newer
  GTK and libadwaita.
- Fixed a profile reload hang when a profile had a missing or malformed
  `created_at`.
- Fixed stuck keys when rollover members hand over a key while a
  keyboard-blocking macro plays, and losing taps cutting off the winner's key.
- Fixed Keymasq treating other programs' uinput devices as its own outputs.
- Fixed `keymasq masking resume` re-evaluating profiles when remapping was not
  paused.
- Fixed layer focus tracking on Hyprland builds that renamed the layer
  `interactivity` field.

### Changed

- Renamed the root helper `keymasq-record` to `keymasq-helper`. AppImage
  upgrades from earlier releases keep working.
- Removed the capture unlock and the macro recording opt-in. Old
  `unlock_required` and `macro_edit_requires_unlock` policy keys are ignored
  with a warning.
- Macro loop, pause, and keyboard and mouse blocking settings now belong to
  the saved macro only. Copies in profile, superkey, and analog control files
  are ignored and dropped on the next save.

## 0.20.0 - 2026-09-24

### Added

- Added hardware masking, which reserves a controller's HID, USB, and input
  nodes so Steam and SDL cannot read it directly while Keymasq remaps it.
  Masks are reapplied after reconnect, restart, suspend, and reboot.
- Added gyro and tilt motion controls for mouse aiming, stick output, and
  Analog Control mappings, with sensor calibration and a live motion preview
  in the device inspector.
- Added motion support for the 8BitDo Ultimate 2 in DInput mode.
- Added configurable virtual controller templates, including a flight stick
  layout, extra TriggerHappy buttons, and custom axes.
- Added a default output per controller, so unmapped buttons and axes go to a
  chosen virtual controller in every profile.
- Controller setup now detects every button and axis a device reports, and
  analog bounds and centers can be edited after setup.
- 1D analog controls can now target any axis on the destination controller.
- Added nested macros. Saved macros can now be added to the macro timeline.
- Added Pause on release for macros. Holding the trigger again resumes
  playback, and an optional timeout discards paused macros.
- Added rapidfire blocks to the macro editor.
- Added a keyboard layout setting for type macros, in Settings and as
  `keymasq type --layout`. Layouts come from the system XKB data.
- Added `--wait` and `--ordered` to `keymasq type` and `keymasq macros play`.
  Ctrl+C cancels only that request.
- Added selection, cut, copy, and paste, bulk retiming, gap editing, and a
  Timing Tools dialog to the macro timeline.
- Added independent macro editor windows. Shift-click a macro's edit button
  to open one.
- Added type-to-search in manager and selector dialogs.

### Improved

- `keymasqd` now runs without Linux capabilities. Source hiding runs through
  short-lived `keymasq-hardware@` root jobs authorized by a Polkit rule.
- Keymasq now releases held keys, macros, and outputs before suspend, and
  checks devices again on wake.
- Area Mouse now has Stick and Touchpad input styles. Touchpad style ignores
  pointer jumps when a finger lands or lifts.
- Virtual keyboards and mice now advertise all evdev key codes.
- Repeated short macros now start faster.
- The GUI now remembers the selected profile across restarts.
- Macro recording now ignores motion sensors.
- Added support for GNOME Shell 51.

### Fixed

- Fixed keys staying pressed when a mapping changed while the key was held.
- Fixed superkeys losing release actions when the profile changed while held,
  and enforced the superkey tap timeout.
- Fixed combo actions running out of order across devices.
- Fixed LED and force-feedback forwarding to physical devices, and daemon
  freezes during force-feedback uploads.
- Fixed Unicode type macros typing wrong characters. Type macros with Unicode
  characters saved in earlier versions must be recreated or edited by hand.
- Fixed `keymasqd` running after the Debian package was removed, which kept
  devices grabbed until reboot.
- Fixed `keymasqd` failing to start after an OS image update gave the
  `keymasq` user a new ID.

### Removed

- Removed `keymasq play` and the compact macro syntax. Use `keymasq type`,
  or `keymasq macros create` and `keymasq macros play`.
- Removed move-to-start macro playback. Saved `move_to_start` fields are
  ignored, and the first natural mouse move sets the start position.
- Removed the in-app Feedback dialog.

## 0.19.0 - 2026-07-12

### Added

- Added a Type tab to the key selector and expanded type macro editing with
  named keys, keyboard shortcuts, mouse actions, and natural mouse movement.
- Added a hardware settings dialog for managing attached event devices,
  including stable-path and product-ID detection.
- Added a SteamOS AppImage with installation and update support.

### Improved

- Improved macro recording and editing with recorded start positions,
  timeline edge scrolling while dragging, and safer save and discard flows.
- Added confirmation before discarding unsaved Super Key or Analog Control
  changes.
- Improved permission error messages for input and uinput access.

### Fixed

- Fixed applying mapping changes after a profile reload, and matching when
  several identical devices use numbered IDs.
- Fixed compositor actions on Hyprland.
- Prevented deletion of the last profile.
- Restored the placeholder tab when no devices are configured.

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
