# Steam Deck masking experiment

Test plan, updated 2026-09-08. The first Desktop Mode takeover, A-to-B
replacement, and timed restoration experiments have passed using temporary
lab tools. See [the measured results](STEAM_DECK_MASKING_RESULTS.md) and
[the proposed design](CONTROLLER_ISOLATION_PROPOSAL.md). The reusable test
utility and product integration described below remain to be built.

The Steam Deck is the primary hardware test platform. The first objective
is to take the built-in controller away from an already running Steam client,
receive physical input in Keymasq, and return control without restarting
Steam or physically reconnecting anything. Controller feature parity and
the settings UI are outside this experiment.

## Prepare an isolated experiment

Build a small repository-owned test utility with discovery, snapshot,
temporary restriction, takeover, verification, and restoration operations.
Keep experimental files in a dedicated temporary directory on the Deck.
Use temporary udev policy and transient services; do not disable SteamOS
filesystem protection or overwrite installed Keymasq configuration.

Control the test over SSH. Record SteamOS, kernel, systemd, Steam, and
Keymasq versions and the active desktop mode at the start of each run.
Rediscover the controller through its physical USB/HID ancestry and serial
or port identity. Never hardcode the earlier `hidraw3` or `event18` names.

Save the existing Keymasq service/session state and temporarily stop its
remapping runtime for the initial mechanism tests. Its current profiles
grab Steam's virtual `28de:11ff` outputs, which would obscure the result.
Restore the previous service state at the end. Repeat later with production
Keymasq integration after the mechanism works.

## Prove restoration before testing takeover

The utility must journal the exact live device identities, permissions,
ACLs, relevant udev metadata, and task-owned rules before mutation. A
separate system service runs restoration; it does not depend on the SSH
shell, GUI, or test worker. A systemd timer can launch it after a deadline.
[systemd transient services and timers](https://man7.org/linux/man-pages/man1/systemd-run.1.html).

Use a 60-second automatic rollback for the initial experiments. Arm and
verify the timer before applying restrictions. The restore operation must
stop experiment workers and cancel pending transitions before restoring
access, so a late worker cannot mask the device again. Remove only the
experiment's rules and flags. Reconcile live node policy using recorded
identity and current system rules, accounting for nodes recreated during
takeover. Do not restore a stale ACL to another device with the same name.

First test harmless no-op restoration and manual invocation. Then exercise
a temporary permission change and verify recovery when the worker exits,
is killed, or is stopped indefinitely. Also disconnect SSH deliberately.
No automatic takeover loop starts until these bounded tests return access
and leave no experimental restrictions behind. A transient timer does not
survive reboot; boot behavior and stale policy require a separate test.

## Compare takeover mechanisms one at a time

Start each trial with Steam already holding the physical raw interface and
normal controller operation confirmed. Keep Steam's main process running
through the trial. Restore and re-establish that baseline between trials.

| Trial | Action | Question answered |
| --- | --- | --- |
| Baseline | Observe handles, input ancestry, and events without masking. | Is this the same raw-HID ownership path observed previously? |
| Restriction only | Block new non-Keymasq opens on the selected controller's relevant input, HID, and USB nodes. | Do existing Steam handles continue working while new opens fail? |
| Synthetic removal | Keep restrictions active and emit narrowly targeted remove notifications. | Does Steam voluntarily close every relevant old connection? |
| Targeted driver reattachment | With restrictions active for recreated nodes, detach and reattach the selected controller interface through a tested backend. | Can the kernel invalidate the old HID connection and recreate input without restarting Steam? |

Rebinding the Deck's main `hid-steam` interface passed the first Desktop
Mode experiment. Repeatability and other backends remain untested. Inspect
each driver's bind/remove behavior first. Never substitute a reset of the
Bluetooth adapter, USB host controller, or an entire hub. The Deck's USB
controller device also exposes CDC serial interfaces; widening a transition
to the whole USB device requires accounting for those functions. A USB
reset or interface rebind is not proof that an old usbfs handle is unusable.

Synthetic removal requires checking both system and Keymasq state because
udev processes the event as a removal. Verify restoration of tags, links,
and metadata as well as node permissions. Targeted triggering and event
monitoring are supported by udevadm.
[udevadm documentation](https://man7.org/linux/man-pages/man8/udevadm.8.html).

Stop advancing mechanisms once one satisfies the criteria; test its
repeatability and recovery next. If none works, record the precise surviving
access path before considering a broader disconnect or kernel changes.

## Collect evidence that distinguishes takeover from a UI change

Write a timestamped record per trial containing:

- Physical attachment identity, connection generation, sysfs ancestry,
  node identities, effective permissions, and related kernel drivers.
- Kernel and udev lifecycle events, including which physical input nodes
  disappeared or returned and which virtual nodes were created.
- Steam's relevant file handles and short, narrowly filtered syscall
  observations. Distinguish successful I/O, closed handles, and stale handles
  that remain listed but now return errors.
- Fresh open attempts using the ordinary `deck` account and its actual
  groups. A root probe is not a valid test of desktop access restrictions.
- Physical input observed by the test runtime, correlated with the user's
  deliberate button presses. Resolve ancestry after each transition.

A raw probe open can itself make `hid-steam` withdraw evdev input. Do not
open raw devices during passive inventory or leave a diagnostic handle
holding ownership after a test. Additional clients deliberately holding
old handles belong in separate, labelled robustness tests.

The first ownership pass requires all of the following:

1. Steam's main process survives without a requested restart.
2. Steam no longer receives physical reports through its old raw connection;
   all covered new desktop opens fail. No successful direct USB route remains.
3. Physical controller input becomes available to the test runtime. On the
   evdev approach, it comes from the physical Deck ancestry, not `28de:11ff`
   Steam outputs or a cloned virtual device.
4. Restoration returns normal physical access and usable Steam controller
   operation. Existing process state and device caches must actually recover.

There must be a known input event during the observation window; an idle
controller and an absence of observed reads are not sufficient evidence.
Handle snapshots alone also cannot prove that a brief reopen never occurred.
Exercise continuous reopen attempts in later race tests and report limits.

## Demonstrate the replacement path

After ownership succeeds, create one minimal virtual gamepad and demonstrate
an unmistakable mapping, such as physical A producing virtual B with no
virtual A event. In Steam's controller tester, the user checks the resulting
button while instrumentation confirms its physical source and virtual output.
Remove unrelated test outputs from this trial and record any remaining
Steam-created devices rather than assuming every controller list has one row.

This proves the path physical controller to Keymasq to Steam. A correct
button response by itself could also come from remapping Steam's own output,
so it must accompany the physical-ancestry and access checks above. Full
axis conversion, rumble, and vendor features stay with the other branches.

Check input mode before declaring the source ready. In the September 7
experiment, the Deck's physical evdev node returned but emitted no gamepad
events until `hid_steam.lizard_mode` changed from `Y` to `N`. The lab helper
saved and restored the original value. This parameter affects all devices
handled by that module, so a production adapter needs an explicit scoped
mode strategy. Opening evdev alone does not switch the Deck into gamepad mode.

For product integration, also test the Deck with no hardware mapping or
profile configured. Masking must bring up `hid-steam` input and a usable
default passthrough controller without requiring configuration first.
Verify controller operation in both Desktop Mode and Gaming Mode. The
September 7 A-to-B probe established routing only; it did not establish
this complete default behavior.

Keep the touchscreen outside the controller mask and test Keep and Restore
by touch while controller input is unavailable. In a separate trial, leave
the dialog unconfirmed and verify automatic restoration with the GUI
frozen. After timeout, neither profiles nor service restarts may reactivate
the mask until the user explicitly resumes. Activation uses the existing
unlock policy; restoration by the recorded owner remains available after
that unlock expires.

## Hardware default output integration

Repeat the physical-input checks with the September 12 routing rework. The
September 8 AppImage used an adapter that has since been removed.

- Start without a hardware configuration. Confirm masking exposes ordinary
  passthrough, and setup sees the reserved physical sources.
- Choose a virtual default output in hardware setup. Verify Steam receives
  events from that output, with no leftover controller passthrough clone.
- With no active profiles, restart Keymasq and reboot into Gaming Mode. Verify
  the saved mask and hardware route return together.
- Add and remove a button mapping. The hardware default route must remain
  selected when the profile stops applying.
- Switch virtual targets and then select passthrough while a button or axis
  is held. Verify the old output is neutralized, the physical grab remains,
  and the mask watchdog does not restore access during the change.
- Remove the hardware configuration. Verify ordinary passthrough returns
  while masking stays active, and the sources are available to add again.
- Learn buttons and axes while routed. Capture must suppress output, retain
  the reservation, and resume the selected route afterwards.
- Remove the selected virtual output or stop any reserved interface reader.
  Verify recovery restores access and pauses automatic masking.

Test Deck trigger and trackpad mappings explicitly. Default routing preserves
numeric event codes; masking no longer translates those controls into Xbox
codes. Treat game compatibility and physical control coverage as separate
checks from access denial and event delivery.

## Repeatability and failure cases

Once single transitions work, automate 100 mask/unmask cycles in Desktop
Mode and 100 in Gaming Mode, using state-based waits and bounded deadlines.
Record every failure and transition duration. Scripted cycles verify
ownership and cleanup; sample user input before and after transitions too.
These counts are an initial acceptance target, not proof of universal support.

Then test both startup orders, Steam restarting while masked, suspend/resume
while masked, session switching, Keymasq crash and hang, GUI death, SSH loss,
partial udev failure, and emergency recovery. Include boot with remembered
policy and boot after failed activation. Restoration must not immediately
reapply masking or grabs through profiles, hotplug, or automatic restarts.

For persistent masking, confirm once with the default startup option enabled,
close the GUI, and restart the daemon, user session and recovery service
separately. Each must recreate a usable replacement without Keep or a fresh
unlock. Reboot into Gaming Mode and verify that the saved identity resolves
new event nodes and a new connection generation. Repeat with the option off;
hardware must stay unmasked after startup. Finally use administrative recovery
with persistence enabled and verify that daemon restart and reboot preserve
the pause until explicit Resume. Check the saved hardware configuration and
calibration for unintended changes.

The inventory test must also pass when the Deck has no physical evdev node
and has never been added to Keymasq. It must remain visible through masking,
input creation, registration, recovery, and reconnect.

SteamOS and the built-in Deck controller are the release gate for this work.
Afterwards, add USB and Bluetooth external controllers and keyboards/mice
as compatibility cases. Different HID drivers and transports can have
different takeover behavior even when the policy machinery is shared.
VMs remain useful for automated udev and recovery tests, not as a replacement
for the Deck's physical-driver and Steam tests.

Any Python implementation must pass the repository's `./scripts/check.sh`
handoff gate. The hardware report is additional evidence about the target,
and must distinguish completed tests from proposed ones.
