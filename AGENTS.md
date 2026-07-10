# Keymasq - Agent Guide

## Architecture

Keymasq is a Linux input remapper for keyboards, mice, gamepads, and other evdev
devices. It supports layered profiles, macros, superkeys, combos, analog controls, and
compositor-aware behavior through three processes:

- `keymasqd` - privileged daemon for device grabbing, remap runtime, macro storage/playback, recording, and combo capture/runtime
- `keymasq-session` - per-user broker for profiles, compositor/window tracking, daemon IPC, and GUI-facing state
- `keymasq` - GTK4 GUI and CLI

## Work Rules

- Anchor commands and file paths to the current `environment_context.cwd`.
- Do not overwrite unrelated local changes.
- Match the surrounding Python and GTK4 patterns.
- Keep behavior docs in `docs/`; update them when user-visible semantics change.
- Keep new code async-friendly. Do not add blocking I/O or long synchronous waits.

## Main Code Areas

- `keymasq/common/`: shared models, IPC, paths, settings, security helpers.
- `keymasq/keymasqd/`: daemon, command handlers, macro storage, capture.
- `keymasq/keymasqd/runtime/`: grabbed devices, actions, outputs, combos,
  analog controls, topology, runtime profile tracking.
- `keymasq/session/`: profile/config managers, session client.
- `keymasq/session/listeners`: compositor integration
- `keymasq/session/manager/`: session broker lifecycle, command/event dispatch,
  profile application, recording, inspectors, diagnostics/output streams.
- `keymasq/gui/`: GTK application, window, widgets, wizards, dialogs.
- `tests/`: pytest suite, with focused subtrees for common, keymasqd, session, GUI.
- `docs/`: Project documentation
- `docs/agents/`: compact config references for profiles, hardware, macros,
  superkeys, and combos.

## Terms and Gotchas

- Grab: exclusive daemon control of an input device/interface so Keymasq can suppress,
  pass through, or remap events.
- Hardware config: the physical device description and source button/key IDs.
- Profile: a global remap layer. A profile can contain mappings for multiple devices.
- Mapping: one source input in one profile device layer bound to one action.
- Profile layering has priority order. Conditional profiles override permanent profiles.
- Macro: a saved timed sequence of input events.
- Superkey: a reusable multi-role key/button definition for tap, hold, double-tap,
  tap-hold, or grouped press/release actions.
- Combo: a chord or sequence trigger across one or more grabbed input devices.
- Prefix-shadowing between combos is valid runtime behavior.
- Analog control: reusable stick, trigger, wheel, or game controller axis behavior.

## Checks

Before handing off Python code changes, run `./scripts/check.sh`. It includes
`ruff`, `basedpyright`, and the relevant pytest suite in the pinned Nix environment.
`./scripts/check.sh` is the full handoff gate for agent work; nothing beyond it
is expected.

Run Python commands and tooling through `nix develop -c`.
