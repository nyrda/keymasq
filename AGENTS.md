Keymasq is a Linux-only input remapping and customization tool for keyboards, mice, gamepads, and other evdev
devices. It supports layered profiles, macros, superkeys, combos, analog and motion controls,
hardware masking, and compositor integration (active window tracking, compositor actions) through
two long-running processes, a GUI/CLI, and a short-lived root helper:
- `keymasqd` - privileged daemon for device grabbing, hardware masking coordination, remap runtime, macro recording/storage/playback,
  combo and superkey runtime and state machines, and live input/combo capture
- `keymasq-session` - per-user broker, only path from GUI/CLI to the daemon; owns profile layering and compositor integration
- `keymasq` - GTK4 GUI and CLI
- `keymasq-helper` - root helper, never resident: runs the capture unlock and performs hardware masking

## Work Rules

- Do not overwrite unrelated local changes.
- Match the surrounding Python and GTK4 patterns.
- Update docs in `docs/` when user-visible semantics change.
- `docs/agents/` contains extra compact config txt references addressed to agents
- Keep new code async-friendly. Do not add blocking I/O or long synchronous waits.
- Default to no comments. Add one line only when the code cannot show why it is
  written this way, such as a compositor quirk, a security constraint, or a
  non-obvious ordering requirement.
- Do not add comments or docstrings to tests.

## Terms and Gotchas

- Grab: exclusive daemon control of an input device/interface so Keymasq can suppress,
  pass through, or remap events.
- Hardware config: the physical device description and source button/key IDs.
- Profile: a global remap layer. A profile can contain mappings for multiple devices.
- Mapping: one source input in one profile device layer bound to one action.
- Active profiles are layered by priority.
  Conditional profiles override permanent ones.
- Macro: a deadline-scheduled sequence of events.
- Superkey: a reusable multi-role key definition that can be assigned to keys or buttons
- Pattern superkey: for tap, hold, double-tap, tap-hold patterns.
- Overload superkey: one-to-many mapping with support for extra actions on press or release
- Motion controls: reusable configuration to map a gamepad's gyro rotation or tilt to mouse, stick, or an analog control.
- Analog controls: reusable stick, trigger, or any game controller axis behavior.
  Axes can have multiple analog controls assigned.
- Combo: a chord or sequence trigger across one or more grabbed input devices.
  Prefix-shadowing between combos is valid runtime behavior.
- Capture unlock: runtime or permanent lease that guards privileged input data
  (live capture, combo capture, saving recordings).
  New features exposing raw input must require it.
- Hardware masking: blocks other applications from opening a physical device while Keymasq keeps access.
- `keymasq/masking/` is shared masking code used by the daemon and the helper.
  The daemon side is `keymasq/keymasqd/hardware_masking.py`.
- Tests live under `tests/<category>/` matching `common`, `keymasqd`, `session`, or `gui`.
  Anything else fails collection.

## Checks

Xvfb: always use display ≥ :90. Never use `-displayfd` or `xvfb-run -a`. Never touch `/tmp/.X11-unix/X0*` or `/tmp/.X0-lock`.
Before handing off Python code changes, run `./scripts/check.sh`. It includes
`ruff`, `basedpyright`, and the relevant pytest suite in the pinned Nix
environment. It is the only gate expected for code changes.
Run Python commands and tooling through `nix develop -c`.
