# Daemon Session Integration Test

Keymasq includes a NixOS VM integration test for the `keymasqd` daemon plus
`keymasq-session` broker. It runs without the GTK GUI. A Python client script
acts like the GUI by writing profile, hardware, superkey, and macro state,
then driving virtual input devices and asserting the daemon's virtual outputs.

## What It Covers

The test is intentionally a smoke/integration suite, not exhaustive unit
coverage. It verifies that the core runtime classes still work together:

- simple keyboard remap
- extended function, media, microphone mute, and brightness keyboard outputs
- suppress
- tap-enabled remap
- repeat last action across direct mappings, passthrough input, combos, and superkeys
- rapidfire
- macro playback and cancellation
- macro create/update/rename/delete plus loop count
- macro pause/resume with exact cleanup and continuation events
- child pause expiry while a parent configured with Never retains its progress
- parallel-child failure aborts a parent paused with Never and releases a sibling's
  held key without another trigger press; the next press starts a fresh invocation
- superkey tap
- overloaded superkey with multiple press and release actions
- chord, multi-step, prefix-shadowing, overlapping, negative, and multi-source combos
- combo bound to a superkey
- combo trigger-key recall and restore timing with a combo-bound superkey
- profile toggle, priority override, passthrough override, and held-output profile change
- temporary profile activations across direct mappings, temporary toggles, superkeys, overload
  superkeys, combos, and combo-bound overload superkeys
- standard and `BTN_TASK` mouse buttons, relative movement, wheel, and mouse combo output
- gamepad button and analog axis output
- controller touchpad mouse strokes, sparse axis reports, profile changes, and held-touch restart
- emergency reset
- capture, combo capture, recording save, and playback
- session restart, daemon restart, and secondary device hotplug/replug

## Layout

The NixOS VM check lives in:

```text
nix/daemon-session-integration-test.nix
```

The runner source lives in:

```text
nix/daemon-session-integration-test/
```

Important subdirectories:

- `fixtures/` - TOML templates rendered into the VM user's `~/.config/keymasq`.
- `scenarios/` - one scenario file per integration case.
- `support.py` - VM test harness helpers for sockets, virtual devices, fixture rendering, and output assertions.
- `runner.py` - loads and runs the scenario list.

Fixtures are rendered through `support.py` by loading template files from
`fixtures/` and substituting runtime values such as the virtual evdev paths.

## Running It

Run the VM check from the repository root:

```bash
./scripts/integration.sh daemon-session
```

Run one or more scenarios by kebab-case scenario key, and repeat selected
scenarios when chasing flakes:

```bash
./scripts/integration.sh daemon-session --scenario hotplug-replug
./scripts/integration.sh daemon-session --scenario profile-lifetime-direct-actions,hotplug-replug --repeat 10
./scripts/integration.sh daemon-session --scenario macro-pause-resume,macro-child-pause-expiry --repeat 3
./scripts/integration.sh daemon-session --scenario macro-paused-parent-child-failure --repeat 3
```

This is equivalent to running the Nix check directly:

```bash
nix build 'path:.#checks.x86_64-linux.daemon-session-integration-test'
```

Use the `path:` flake reference while the VM files are uncommitted. A plain
`.#...` build evaluates the Git snapshot and can miss newly added fixture or
scenario files.

The test is VM-heavy. A Linux host with KVM acceleration is strongly
recommended.

## Area mouse scenarios

Run both input styles together:

```bash
./scripts/integration.sh daemon-session --scenario analog-stick-to-mouse-area,analog-touchpad-mouse
```

The stick scenario uses the original Area Mouse fixture without an explicit
style setting, checking that it defaults to Stick. It verifies unequal first
and last samples, fast full-range flicks, holding still, diagonal movement, and
returning to the pointer origin. A second profile checks center jitter, the
single deadzone setting, sensitivity, and a nonlinear response curve. Returning
inside the deadzone must return the pointer to its origin.

The virtual source exposes `ABS_HAT1X/Y` and `ABS_HAT2X/Y` with the Steam
Controller's signed 16-bit axis range. TOML fixtures define paired analog inputs,
Area Mouse controls with Touchpad style, and two layered profiles. The real session broker loads
the configuration and applies profile changes through daemon IPC. Assertions read
the daemon's mouse output through evdev; runtime state and output methods are not
mocked. The kernel filters unchanged ABS values, exercising sparse reports.

| Case | Required output |
| --- | --- |
| Touch either pad away from center | No pointer movement |
| Increase each normalized coordinate by 0.25 | Exactly 100 horizontal and 50 vertical units |
| Hold still | No additional movement |
| One axis becomes zero | Continue the stroke |
| X reaches zero before Y changes in the same report | Use the complete pair without a false release |
| Both axes return to zero | No return movement |
| Retouch elsewhere with Y remaining zero | Anchor silently, then move normally on an X-only report |
| Higher-priority profile changes one pad's scale and inversion | Reanchor that pad using both current coordinates; apply the new scale and inversion |
| The other pad's mapping remains unchanged | Preserve its existing stroke across the same profile update |
| Remove the override | Reanchor and restore the original scale without losing the unchanged coordinate |
| Restart the daemon with a finger held down | Recover the unreported axis from the real device state and resume without a jump |

Every output assertion rejects unexpected movement as well as incorrect deltas,
and checks for trailing events. Both pads return to zero and the scenario disables
its profiles during cleanup. Use `--repeat 3` to check repeated execution.

This scenario does not emulate the Steam Controller firmware or force an evdev
queue overflow. `SYN_DROPPED` recovery and failed device-state reads remain covered
by the focused daemon tests. Physical touch feel still needs hardware testing.

## Debugging Failures

On failure, Nix prints the failed derivation path and suggests a `nix log`
command. Run that command to see:

- `keymasqd` status and journal
- `keymasq-session` status and user journal
- `/dev/uinput` permissions
- `/proc/bus/input/devices`
- generated Keymasq config from inside the VM
- scenario runner output

The runner prints each scenario name as it starts. The last printed
`integration: ...` line identifies the scenario that failed.

## Adding Scenarios

Add one file under:

```text
nix/daemon-session-integration-test/scenarios/
```

Then import it and append it to `SCENARIOS` in:

```text
nix/daemon-session-integration-test/scenarios/__init__.py
```

If the scenario needs new persistent profile, hardware, or superkey state, add
or update a fixture under `fixtures/` rather than embedding TOML in Python.
