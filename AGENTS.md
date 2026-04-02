# Keyforge - Agent Guide

Use this file as a quick project map. Keep deeper behavior details in `docs/`, not here.

## What Keyforge Is

Keyforge is a Linux input remapper built around three processes:

- `keyforged` - privileged daemon for device grabbing, remap runtime, macro storage/playback, recording, and combo capture/runtime
- `keyforge-session` - per-user broker for profiles, compositor/window tracking, daemon IPC, and GUI-facing state
- `keyforge` - GTK4 GUI and CLI for profiles, mappings, macros, superkeys, and combos

## Main Code Areas

- `keyforge/common/` - shared models, IPC types, paths, security helpers
- `keyforge/keyforged/` - privileged daemon and input runtime
- `keyforge/session/` - profile resolution, listeners, session socket
- `keyforge/gui/` - GTK app and widgets
- `keyforge/cli/` - CLI
- `tests/` - pytest suite
- `docs/` - behavioral docs

## High-Value Files

- `keyforge/common/models.py` - `ActionType`, `MappingAction`, profile and combo models
- `keyforge/common/ipc.py` - daemon/session protocol
- `keyforge/common/recording_guard.py` - recording/combo-capture unlock helpers
- `keyforge/keyforged/device_manager.py` - remap runtime, combo runtime, uinput output
- `keyforge/keyforged/combo_engine.py` - combo matcher and timeout handling
- `keyforge/keyforged/daemon.py` - privileged command dispatch
- `keyforge/session/manager.py` - session socket, daemon sync, recording/capture/compositor dispatch
- `keyforge/session/profiles.py` - profile load/save and active-profile/combo resolution
- `keyforge/gui/widgets/device_tab.py` - per-device mapping UI
- `keyforge/gui/widgets/key_selector_dialog.py` - mapping action chooser

## Project Rules That Are Easy To Break

- Profiles are global, not per-device. Per-device mappings live in profile device layers keyed by `hardware_id`.
- Active profile ordering matters. Later profiles win.
- Combos are stored on `ProfileConfig.combos` and resolved alongside mappings.
- Combo matching is exact on stored `hardware_id` + `source` + `evdev`.
- Combo capture is profile-scoped. The session layer expands a profile to the `hardware_id`s to capture from.
- Combo capture uses the same original-input security model as macro recording. Do not bypass unlock or ownership checks.
- Combos may require `force_grab_unmapped` for otherwise passthrough inputs.
- Prefix-shadowing between combos is valid runtime behavior. The GUI only rejects exact duplicates within one profile.
- `COMPOSITOR_DISPATCH` is generic backend behavior. Compositor-specific UI belongs under `gui/widgets/compositor_actions/`.
- Left and right mouse buttons are protected in the GUI.
- Xbox triggers are analog output (`ABS_Z`, `ABS_RZ`), not normal digital buttons.

## GUI Notes

- `MainWindow` hosts both device tabs and a separate `ComboTab`.
- Profile selection is window-level and syncs between device tabs and combo tab.
- `KeySelectorDialog` is shared by device mappings and combo actions.
- Compositor-specific action pages should stay modular; do not hardcode compositor UI into `key_selector_dialog.py`.
- In GTK code, create dependent attributes before connecting signals that may fire immediately.

## Working Style

- Prefer small, local changes over cross-cutting rewrites unless the task requires structure changes.
- Keep compositor-specific code modular so more compositors can be added later.
- When adding behavior, update the relevant `docs/*.md` if the change affects user-visible semantics or security.

## Coding Style

- Use Python type hints consistently.
- Keep new code async-friendly. Do not add blocking I/O or long synchronous waits.
- Follow existing GTK4 patterns in the surrounding file instead of introducing a new style.
- Keep comments sparse and only add them when they clarify non-obvious logic.
- Prefer small helper functions over deeply nested UI or runtime logic.
- Put compositor-specific behavior in dedicated modules, not in generic dialog/runtime code.

## Commands

You are no done before running  `./scripts/check.sh` passes. It runs `ruff`, `basedpyright` with nix develop, and the full pytest test suite inside a VM.
