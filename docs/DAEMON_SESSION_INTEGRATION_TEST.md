# Daemon Session Integration Test

Keymasq includes a NixOS VM integration test for the `keymasqd` daemon plus
`keymasq-session` broker. It runs without the GTK GUI. A Python client script
acts like the GUI by writing profile, hardware, superkey, and macro state,
then driving virtual input devices and asserting the daemon's virtual outputs.

## What It Covers

The test is intentionally a smoke/integration suite, not exhaustive unit
coverage. It verifies that the core runtime classes still work together:

- simple keyboard remap
- suppress
- tap-enabled remap
- rapidfire
- macro playback and cancellation
- macro create/update/rename/delete plus loop count
- superkey tap
- overloaded superkey with multiple press and release actions
- chord, multi-step, prefix-shadowing, overlapping, negative, and multi-source combos
- combo bound to a superkey
- combo trigger-key recall and restore timing with a combo-bound superkey
- profile toggle, priority override, passthrough override, and held-output profile change
- profile action lifetimes across direct mappings, temporary toggles, superkeys, overload
  superkeys, combos, and combo-bound overload superkeys
- mouse button, relative movement, wheel, and mouse combo output
- gamepad button and analog axis output
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
nix build 'path:.#checks.x86_64-linux.daemon-session-integration-test'
```

Use the `path:` flake reference while the VM files are uncommitted. A plain
`.#...` build evaluates the Git snapshot and can miss newly added fixture or
scenario files.

The test is VM-heavy. A Linux host with KVM acceleration is strongly
recommended.

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
