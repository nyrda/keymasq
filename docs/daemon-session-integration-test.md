# Daemon session integration test

Keymasq includes a NixOS VM integration test for the `keymasqd` daemon plus
`keymasq-session` broker. It runs without the GTK GUI. A Python client script
acts like the GUI by writing profile, hardware, superkey, and macro state,
then driving virtual input devices and asserting the daemon's virtual outputs.

## What it covers

The test is a smoke/integration suite, not exhaustive unit coverage. It verifies
that the core runtime classes still work together:

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
- parallel-child failure aborts a parent paused with Never and releases a
  sibling's held key without another trigger press, and the next press starts a
  fresh invocation
- macro keyboard blocking: passthrough and remapped presses are held back while
  another macro still types, a key held before playback can be released, and a
  key pressed during playback needs a fresh press afterwards
- macro keyboard blocking with rollover groups and emergency reset: a member
  pressed during the block stays silent, then is restored normally after
  playback ends, and an emergency reset during the block stops the macro and
  lifts the block before the macro would have ended
- `<macro:NAME>` calls in type macros, from a profile mapping and from
  `keymasq type`: the called macro finishes before the following text, the
  prefix is case-insensitive and spaces around the name are ignored, `\<macro:`
  types the literal text (checked by decoding the US-layout keys), a missing or recursive call stops typing at the call and
  later macros still play, and `--print-json` shows the `macro_sync` event
- superkey tap
- overloaded superkey with multiple press and release actions
- chord, multi-step, prefix-shadowing, overlapping, negative, and multi-source combos
- combo bound to a superkey
- combo trigger-key recall and restore timing with a combo-bound superkey
- profile toggle, priority override, passthrough override, and held-output profile change
- superkey press/release Exec commands across mapping replacement and deferred ungrab,
  plus pattern hold completion after profile deactivation
- temporary profile activations across direct mappings, temporary toggles, superkeys, overload
  superkeys, combos, and combo-bound overload superkeys
- standard and `BTN_TASK` mouse buttons, relative movement, wheel, and mouse combo output
- pointer movement factors, swap, and inversion from a source mouse with fractional
  carry-over, plus velocity and position (drag, keep, recenter, minimum output) translation
  of mouse reports into virtual gamepad axes
- gamepad button and analog axis output
- rollover groups: axis members that follow the newest held key without
  centering, keyboard handover and restore, neutral groups, unmapped members
  passed through, an overload superkey member, members on two devices, a
  device unplugged after or during its member's turn, a higher-priority
  profile replacing a group, held keys kept while their own or another group
  changes, a member recalled by a combo and released before, or still held
  while, another member takes over, and members held while their profile is
  disabled, including autorepeat of a held-back member
- controller touchpad strokes, sparse reports, profile changes, held-touch restart,
  near-center release with kernel fuzz, fuzz restoration, landing/lift filtering,
  and drag button ordering during rest and continuous movement
- emergency reset
- capture, combo capture, recording save, and playback
- macro recording refused when `/etc/keymasq/security.toml` disables it
- session restart, daemon restart, secondary device hotplug/replug, and a fast
  replug that reuses the same `/dev/input/eventN` node with no reload
- empty effective, permitted, inheritable, bounding, and ambient capability sets
  before and after every scenario
- source hiding, forwarding, and force-feedback upload/play/stop/erase through
  the installed daemon, including after a restart while the source stays attached
- existing macro files and the mutation lock reopened after a daemon restart
- native motion capture through real UHID/hidraw reports, after initial connection,
  with the controller attached across a restart, and after reconnection
- Steam Deck LS/RS/LP/RP Touch through the bundled HID-BPF program on an emulated
  Deck bound by the kernel's `hid-steam` driver: attachment by the root job,
  descriptor handoff, unchanged `hid-steam` gamepad output, detach on removal,
  re-attach on reconnection and daemon restart, and refusal of a non-root handoff
  client and of non-matching attach requests
- a private user FUSE mount that denies root's descriptor inspection, and refusal
  to skip that descriptor after the mount disappears from the holder's namespace

## Layout

The NixOS VM check lives in:

```text
nix/daemon-session-integration-test.nix
```

The runner source lives in:

```text
nix/daemon-session-integration-test/
```

Subdirectories and files:

- `fixtures/` holds TOML templates rendered into the VM user's
  `~/.config/keymasq`.
- `scenarios/` holds one scenario file per integration case.
- `support.py` provides VM test harness helpers for sockets, virtual devices,
  fixture rendering, and output assertions.
- `runner.py` loads and runs the scenario list.

`support.py` renders fixtures by loading template files from `fixtures/` and
substituting runtime values such as the virtual evdev paths.

## Running it

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
./scripts/integration.sh daemon-session --scenario macro-call-from-mapped-type-macro,macro-call-from-keymasq-type
./scripts/integration.sh daemon-session --scenario macro-keyboard-block-rollover,macro-keyboard-block-emergency-reset
```

This is equivalent to running the Nix check directly:

```bash
nix build 'path:.#checks.x86_64-linux.daemon-session-integration-test'
```

Use the `path:` flake reference while the VM files are uncommitted. A plain
`.#...` build evaluates the Git snapshot and can miss newly added fixture or
scenario files.

The test is VM-heavy. Use a Linux host with KVM acceleration.

To check the daemon's capability-free device and storage access:

```bash
./scripts/integration.sh daemon-session --scenario native-hidraw-access,source-hiding,macro-lifecycle,recording-and-capture,hotplug-replug
```

The full suite ends with a policy phase. It replaces `/etc/keymasq/security.toml`
with the VM policy plus `recording_guard.macro_recording_allowed = false`,
restarts `keymasqd` and `keymasq-session`, and runs only the
`macro-recording-disabled-by-policy` scenario. The main run skips that
scenario, and `--scenario` refuses it because selected runs keep the default
policy. Run `./scripts/integration.sh daemon-session` without `--scenario` to
cover it.

The FUSE check runs during VM setup. It creates an ordinary user mount with
`nodev`, without `allow_other`, and verifies that root cannot stat the held file.
The exception must recognize its mount through the holder's proc metadata. A lazy
unmount in the holder's separate mount namespace leaves the file open but removes
the mount entry, so the same exception must then refuse it.

The daemon retains the installed unit's account, ACL rules, filesystem sandbox,
and device policy. The test client alone receives access to `/dev/uhid` to emulate
the controller. These checks cover the emulated devices and existing daemon-owned
state. They do not replace physical controller tests or an upgrade test of every
distribution package.

## Macro call scenarios

The two `macro-call-*` scenarios save a child macro that presses `1` and, 300 ms
later, `2`, plus type macros that call it, a missing macro, themselves, or each
other in a loop. The mapped scenario triggers the saved type macros from a
profile. The CLI scenario runs the installed `keymasq type --wait --json` as the
VM user, so the request goes through the real session socket. Each case checks
the exact keyboard output: the child's keys come before the typed text, nothing
after a failed call is typed, and a later call still plays. The CLI case also
checks the failure message for the missing name or the recursion.

## Pointer movement scenarios

```bash
./scripts/integration.sh daemon-session --scenario pointer-movement-factors,pointer-movement-stick-velocity,pointer-movement-axis-position
```

Each scenario creates a uinput source mouse with its own hardware file and profiles, and
removes them afterwards. Mouse-mode assertions read the mouse's passthrough device and check
exact relative counts, including fractions carried across reports, and that the wheel still
passes through. Axis assertions read the exact sequence of virtual gamepad values: velocity
output returns to rest without further reports, position output holds while the mouse is
still, drag and keep differ after overshoot, a one-way trigger does not wind up past rest,
recentering and minimum output apply, and a mapping change or profile deactivation releases
held axes. Timing windows are checked for their effect, not their exact duration.

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
Controller's signed 16-bit axis range. TOML fixtures define paired analog
inputs, Area Mouse controls with Touchpad style, and two layered profiles. The
real session broker loads the configuration and applies profile changes through
daemon IPC. Assertions read the daemon's mouse output through evdev, and the
test does not mock runtime state or output methods. The kernel filters unchanged
ABS values, exercising sparse reports.

| Case | Required output |
| --- | --- |
| Touch either pad away from center | No pointer movement |
| Increase each normalized coordinate by 0.25 | Exactly 100 horizontal and 50 vertical units |
| Hold still | No additional movement |
| One axis becomes zero | Continue the stroke |
| X reaches zero before Y changes in the same report | Use the complete pair without a false release |
| Both axes return to zero | No return movement |
| Retouch elsewhere with Y remaining zero | Anchor silently, then move normally on an X-only report |
| Higher-priority profile changes one pad's scale and inversion | Reanchor that pad using both current coordinates and apply the new scale and inversion |
| The other pad's mapping remains unchanged | Preserve its existing stroke across the same profile update |
| Remove the override | Reanchor and restore the original scale without losing the unchanged coordinate |
| Restart the daemon with a finger held down | Recover the unreported axis from the real device state and resume without a jump |
| Small landing drift in consecutive reports | No movement. Use the final landing position as the reference for the next step |
| Move from rest, then send no further reports | The lift-hold timer emits exactly the expected displacement |
| Small movement from rest followed immediately by a zero-pair release | Discard the held movement. The next touch starts cleanly |
| Movement from rest and a drag button transition in one report | Emit the button immediately, then the held movement |
| Two consecutive moving reports, with a drag button transition in the second | Release the accumulated movement before the button as speed removes the lift hold |

The usual motion assertions include a 250 ms silence check, allowing landing to
settle and returning the speed filter to rest before the next step. Landing,
lift, and continuous-motion cases queue consecutive reports without an
intervening output wait. The scenario checks both button press and release. The
focused daemon tests still cover exact timing thresholds and timer rearming.

Every output assertion rejects unexpected movement as well as incorrect deltas,
and checks for trailing events. Both pads return to zero and the scenario disables
its profiles during cleanup. Use `--repeat 3` to check repeated execution.

This scenario does not emulate the Steam Controller firmware or force an evdev
queue overflow. The focused daemon tests still cover `SYN_DROPPED` recovery and
failed device-state reads. Physical touch feel still needs hardware testing.

## HID-BPF Steam Deck touch scenario

```bash
./scripts/integration.sh daemon-session --scenario hid-bpf-steam-deck-touch
```

`steam_deck.py` creates a UHID device with the Deck's gamepad interface: USB bus,
`28de:1205`, and the interface's report descriptor (one vendor 64-byte input and
feature report). The kernel's own `hid-steam` driver binds to it. The emulator
answers the feature reports `hid-steam` sends during probe (serial number,
attributes, lizard mode settings), so `hid-steam` creates its `Steam Deck`
gamepad, `Steam Deck Motion Sensors`, and hidraw client devices as on hardware.
It then streams `0x09` Deck state reports every 10 ms, because the daemon treats
a report counter that stops advancing as a disconnect. The VM loads `hid_steam`
with `lizard_mode=0`, so `hid-steam` reports gamepad input without Steam.

The hardware fixture configures the touch source as a companion of the
`keymasq:28de:1205` gamepad interface, and a profile maps each touch input to a
key. Only the touch source is grabbed, so the scenario can read the `hid-steam`
gamepad node directly while the program is attached.

| Step | Required result |
| --- | --- |
| Enable the profile with the Deck connected | Exactly one `hid_bpf_ops` struct_ops map named `steam_deck_touc` (`bpftool struct_ops show`), and `keymasqd` still has empty capability sets |
| Set and clear byte 13 bits 6/7 and byte 10 bits 3/4, one at a time | Exact press and release of the mapped key for LS, RS, LP, and RP Touch |
| Two touches in one report, then release them in two reports | Both presses, then each release on its own |
| A, left stick X, and LP Touch with left pad X in one report | `hid-steam` reports `BTN_SOUTH`, `ABS_X`, and `ABS_HAT0X` with the injected values, and the LP Touch key is pressed. Clearing them returns all to zero |
| Connect to `/run/keymasq/handoff` as the session user | Permission denied |
| Connect as the `keymasq` account | The daemon closes the connection and logs `Rejected fd handoff from uid <keymasq>` |
| Attach request for the `hid-steam` client device (HID group `0x0103`) | Job result `The HID device does not match this driver`, attachments unchanged |
| Attach request naming the hidraw `8bitdo-ultimate2` driver | Job result `Unknown HID-BPF driver`, attachments unchanged |
| Restart `keymasqd` with the Deck connected | A new attachment replaces the old one, and touches work |
| Remove the Deck | The attachment disappears |
| Reconnect the Deck | The session re-grabs it, a new attachment appears, touches and `hid-steam` output work |

The refusal probes run through narrow sudo rules: `bpftool --json struct_ops
show` as root, and `hid_bpf_probe.py` as the `keymasq` account, which may start
`keymasq-hardware@` jobs through the polkit rule. The test user reads the daemon
journal through the `systemd-journal` group. The probes do not cover a handoff
socket owned by another account; the unit tests cover that refusal.

The emulator reproduces the Deck's report format and the probe handshake that
`hid-steam` needs, not the controller firmware. Physical Deck touch behavior,
the real USB topology, and hardware masking of the Deck still need hardware
testing.

## Debugging failures

On failure, Nix prints the failed derivation path and suggests a `nix log`
command. Run that command to see:

- `keymasqd` status and journal
- `keymasq-session` status and user journal
- `/dev/uinput` permissions
- `/proc/bus/input/devices`
- `keymasq-hardware@` job journal, HID kernel messages, and struct_ops maps
- generated Keymasq config from inside the VM
- scenario runner output

The runner prints each scenario name as it starts. The last printed
`integration: ...` line identifies the scenario that failed.

## Adding scenarios

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
