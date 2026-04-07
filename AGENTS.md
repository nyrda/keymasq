# Keyforge - Agent Guide

Use this file as a quick project map. Keep deeper behavior details in `docs/`, not here.

## Architecture

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
- `keyforge/common/ipc.py` - daemon/session protocol and command types
- `keyforge/keyforged/daemon.py` - privileged command server, ACL checks, and runtime manager wiring
- `keyforge/keyforged/device_manager.py` - daemon-side runtime coordinator for grabs, mappings, combos, and macro playback
- `keyforge/session/manager/core.py` - session socket lifecycle, daemon connection loop, and runtime state
- `keyforge/session/manager/commands.py` - GUI/session command dispatch and policy gating
- `keyforge/session/profiles.py` - profile load/save and active-profile/combo resolution
- `keyforge/gui/window.py` - main window, device/combo tab setup, and shared profile selection sync

## Gotchas

- Profiles are global, not per-device. Per-device mappings live in profile device layers keyed by `hardware_id`.
- Active profile ordering matters. Later profiles win.
- Prefix-shadowing between combos is valid runtime behavior. The GUI only rejects exact duplicates within one profile.
- `COMPOSITOR_DISPATCH` is generic backend behavior. Compositor-specific UI belongs under `gui/widgets/compositor_actions/`.
- `MainWindow` owns profile selection. Device tabs and `ComboTab` must stay in sync with the window-level selection.
- `KeySelectorDialog` is shared by device mappings and combo actions. Do not hardcode compositor-specific UI into it.
- In GTK code, create dependent attributes before connecting signals that may fire immediately.
- Xbox triggers are analog output (`ABS_Z`, `ABS_RZ`), not normal digital buttons.

## Coding Style

- Keep new code async-friendly. Do not add blocking I/O or long synchronous waits.
- Match the surrounding Python and GTK4 patterns.
- When adding behavior, update the relevant `docs/*.md` if the change affects user-visible semantics or security.

## Commands

All tool commands (`ruff`, `basedpyright`, `pytest`) must run inside `nix develop -c <cmd>`. Example: `nix develop -c ruff check .`

Run `./scripts/check.sh <category>` after Python code changes:

- `keyforged` for `keyforge/keyforged/` changes and keyforged-only tests
- `session` for `keyforge/session/` changes and session/compositor tests
- `gui` for `keyforge/gui/` changes and GTK tests
- `full` for multi-category edits, shared-code edits (`keyforge/common/`, CLI, packaging-affecting changes), or before handing off a broad refactor

`./scripts/check.sh` without an argument defaults to `full`. Skip checks for doc-only, config-only, or non-code changes.
