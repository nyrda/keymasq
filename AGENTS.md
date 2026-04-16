# Keymasq - Agent Guide

Use this file as a quick project map. Keep deeper behavior details in `docs/`, not here.

## Architecture

Keymasq is a Linux input remapper built around three processes:

- `keymasqd` - privileged daemon for device grabbing, remap runtime, macro storage/playback, recording, and combo capture/runtime
- `keymasq-session` - per-user broker for profiles, compositor/window tracking, daemon IPC, and GUI-facing state
- `keymasq` - GTK4 GUI and CLI for profiles, mappings, macros, superkeys, and combos

## Main Code Areas

- `keymasq/common/` - shared models, IPC types, paths, security helpers
- `keymasq/keymasqd/` - privileged daemon and input runtime
- `keymasq/session/` - profile resolution, listeners, session socket
- `keymasq/gui/` - GTK app and widgets
- `keymasq/cli/` - CLI
- `tests/` - pytest suite, split into `tests/keymasqd/`, `tests/session/`, `tests/gui/`, and `tests/common/`
- `docs/` - behavioral docs

## High-Value Files

- `keymasq/common/models.py` - `ActionType`, `MappingAction`, profile and combo models
- `keymasq/common/ipc.py` - daemon/session protocol and command types
- `keymasq/keymasqd/daemon.py` - daemon bootstrap, socket server, ACL/recording-unlock enforcement, and dispatch into `daemon_*_commands.py`
- `keymasq/keymasqd/device_manager.py` - stateful facade over `keymasq/keymasqd/runtime/*` for grabs, mappings, combos, topology, and macro playback
- `keymasq/session/manager/core.py` - session server lifecycle, keymasqd reconnect loop, and manager wiring
- `keymasq/session/manager/commands.py` - GUI/session request dispatch and policy gating
- `keymasq/session/manager/profiles.py` - runtime profile reevaluation, device grab/apply logic, and combo updates
- `keymasq/session/manager/state.py` - session runtime state containers
- `keymasq/session/profiles.py` - on-disk profile load/save and profile/combo resolution
- `keymasq/gui/window.py` - main window, device/combo tab setup, and shared profile selection sync

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

- `keymasqd` for `keymasq/keymasqd/` changes and keymasqd-only tests
- `session` for `keymasq/session/` changes and session/compositor tests
- `gui` for `keymasq/gui/` changes and GTK tests
- `full` for multi-category edits, shared-code edits (`keymasq/common/`, CLI, packaging-affecting changes), or before handing off a broad refactor

`./scripts/check.sh` without an argument defaults to `full`. Skip checks for doc-only, config-only, or non-code changes.
