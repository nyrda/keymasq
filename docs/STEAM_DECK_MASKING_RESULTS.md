# Steam Deck masking results

Measured on 2026-09-07 in Desktop Mode. A temporary probe took the built-in
controller from running Steam, read physical input, mapped A to virtual B,
and delivered that output to the same Steam process. Timed restoration
returned the raw device to Steam. This establishes a working takeover path
on this Deck, not a completed Keymasq feature or a general Linux guarantee.

See the [design](CONTROLLER_ISOLATION_PROPOSAL.md) and
[remaining test plan](STEAM_DECK_MASKING_TEST_PLAN.md).

## Routing integration, September 12

The source branch now uses the merged hardware-level default output routing.
The dedicated Deck-to-Xbox adapter used in the September 8 AppImage has been
removed. Unconfigured reserved hardware uses ordinary passthrough; configured
hardware uses its selected default output. Output changes retain the physical
grab, and recovery checks accept shared virtual outputs without a private clone.

Local validation passed `./scripts/check.sh` with 3,614 tests passed and 28
skipped because the test environment has no uinput access. Coverage includes
adoption, shared-output readiness, source retention during output changes,
configuration removal, missing outputs and saved-route reapplication without
profiles. These tests use simulated input and output devices.

The September 7 and 8 measurements below describe those earlier builds. They
do not validate the revised output path. This revision has not been deployed
to the Deck; the next hardware checks are listed in the test plan.

## Development app integration, September 8

The development AppImage now implements the takeover with an independent
`keymasq-maskd` service and the normal Keymasq remapping runtime. The following
Desktop Mode checks used the same Steam process, PID `1100966`:

- A 30-second unconfirmed trial restored physical access and paused remapping.
  A kept trial retained the virtual Xbox controller beyond that deadline.
- Fresh opens as `deck` failed on the controller's three hidraw nodes, usbfs
  node, and four physical event interfaces. The replacement event node opened
  normally. The touchscreen's sysfs ancestry was outside the reservation.
  Its fresh-open permission was already restricted before testing, so these
  checks do not establish touch interaction through the GUI.
- In the confirmed physical-input window around 01:27–01:28 CEST, the daemon
  and an independent virtual-output reader both recorded 131 A press/release
  pairs. Virtual left-stick X and Y reached both limits, -32768 and 32767.
  A trace restricted to Steam's virtual-controller reads recorded 131 A
  presses and 130 releases within its bounded window. Earlier idle captures
  are excluded from this input proof.
- Freezing `keymasqd` at 01:21:31 led the helper to kill it at 01:21:42.
  By 01:21:49 the original driver mode and Steam raw connection had returned;
  remapping remained paused after the daemon restarted.
- Freezing `keymasq-maskd` at 01:52:44 exercised its separate systemd watchdog.
  The watchdog fired at 01:53:04. Journal recovery restored the original mode,
  removed the masking rules, and Steam reopened hidraw3. The helper restarted
  at 01:53:08 and remapping remained paused.
- A running Wine game's `winedevice.exe` retained the controller's usbfs node.
  Activation refused before changing access. After the user closed the game,
  takeover succeeded. Starting that game after masking still needs testing.

Setup initially treated the reservation as another configured device. In
addition, ordinary evdev enumeration omitted the hidden physical gamepad.
Discovery now includes reserved paths, labels unconfigured sources as
available to add, and keeps configured-device duplicate protection. Button
and analog capture borrow the existing reader instead of attempting another
exclusive grab.

Automatic button discovery exposed another inventory mismatch. The masked
gamepad advertised 24 buttons as strings such as `EV_KEY_304`, while setup's
fallback accepted symbolic names such as `btn_south`. With GUI access denied,
setup generated zero buttons. Axis metadata and live button learning used
separate paths and continued to work. Setup now accepts the numeric inventory
format, and the Deck's extra joystick-range buttons no longer cause a flight
stick layout. On the installed AppImage, discovery returned all 24 buttons.
An append-only repair added 14 missing definitions to the test Deck's saved
configuration; its 10 existing button definitions, axes and calibrated motion
sensor were verified unchanged against a backup. This was a one-off repair,
not an automatic migration of saved hardware configurations.

The installed app was checked with a temporary hardware configuration and
permanent A-to-B profile. Configuration adopted the existing gamepad with
`newly_grabbed=0`. Analog learning returned physical axis samples, then resumed
the profile. The virtual event node stayed unchanged throughout setup, capture,
and profile resumption. Temporary test configurations were removed afterwards.
The final 90-second mapped-output capture received no button events, so it
does not establish physical A-to-B behavior for the integrated app. That
button-level confirmation still needs a user input trial.

That development artifact passed the AppImage smoke verifier and the
repository check gate (3,291 passed, 28 skipped). It was repacked from the
installed AppImage's bundled dependencies with this branch's Python code;
it is not a clean distribution build. Gaming Mode, suspend/resume, broad
repeatability runs, external controllers and SDL-specific backend tests remain
outstanding. The sections below retain the initial standalone experiment.

## Persistent startup masking, September 8

The startup option is enabled by default on confirmation. The installed
development build stored the Deck's confirmed identity for UID 1000 and
reapplied it through `keymasq-session`, with the GUI closed:

- At 02:49:46 CEST, restarting `keymasqd` returned to `masked` with
  `automatic=true`, without another Keep request.
- At 02:50:08, restarting the user session did the same.
- At 02:53:45, restarting `keymasq-maskd` restored the controller, restarted
  the input daemon and reapplied the mask. This caught and fixed a shutdown
  race where the helper classified its own termination of the daemon as a
  failure. The regression test covers that ordering.
- Administrative recovery stayed paused through a daemon restart at 02:50:25,
  despite the enabled startup preference. Explicit Resume reapplied the mask.
- Disabling the option left the live mask active. After restarting the daemon
  at 02:54:25, the controller remained unmasked, `lizard_mode=Y`, with no
  recovery pause. Re-enabling the option restored automatic takeover.
- A real reboot returned to Gaming Mode, with `gamescope-wl` and Steam's
  `-gamepadui` session. The boot ID changed from
  `cde3a841-fe74-4ffd-a835-e007b1a37891` to
  `9d62dc73-7730-4394-ba94-b611a2a6e21b`. The saved hardware identity stayed
  `21805c584b69c2d9ed7af282`, while its connection generation changed from
  `7:130692` to `2:27248`. At 02:55:35 the daemon acquired the new physical
  event nodes; at 02:55:42 the supervisor reported `masked`, `automatic=true`
  and no recovery pause. No GUI or Keep request was involved.
- Steam PID 2236 held `/dev/input/event23`, the `045e:028e` replacement named
  `keymasq-Steam Deck`. It held no physical controller hidraw or USB handles.
  Fresh opens as `deck` failed on physical event4, event9, event10, event11,
  usbfs `003/002`, and hidraw0, hidraw2 and hidraw3. The replacement opened
  normally. The saved hardware configuration's SHA-256 was identical before
  and after reboot. No new physical button-input trial was collected.

The first daemon startup during that boot failed while creating its global
mouse output because `/dev/uinput` was no longer writable. systemd retried
five seconds later and startup succeeded before automatic masking began.
This boot-time permission race remains to be investigated. Gaming Mode startup
and ownership are now measured; suspend/resume and sustained gameplay remain
outside these checks.

This build passed `./scripts/check.sh` with 3,339 tests and passed the AppImage
smoke verifier. Its SHA-256 is
`c8c8e83dafaa9d96b8a24fc848d5235fab882af17fbba81eeff4f740529b8c7e`.
It uses the same development repacking process described above.

## Environment and scope

- SteamOS 3.8.14, kernel
  `6.16.12-valve24.4-1-neptune-616-gfe145653a794`, systemd 257.
- Steam main PID `1100966` remained running throughout these trials.
- Built-in USB controller `28de:1205`, attachment `3-3`, main HID device
  `0003:28DE:1205.0012`, bound to `hid-steam`.
- Main controller raw node `/dev/hidraw3`; related keyboard and mouse raw
  nodes `/dev/hidraw1` and `/dev/hidraw2`; usbfs `/dev/bus/usb/003/007`.
  These names describe this connection only. The helper checked USB identity
  and resolved raw interface ancestry before acting.
- Existing `keymasqd` and `keymasq-session` services were temporarily stopped
  to prevent their profiles from grabbing Steam-created outputs. Their
  configuration was preserved and both services were restored.
- Temporary rules restricted the controller's three raw nodes, its USB node,
  and event/joystick nodes under its main gamepad interface. The test did
  not restrict the separate keyboard/mouse evdev interfaces or CDC serial
  interfaces. It did not reset the USB parent, dock, touchscreen, or network.

The root helper installed two task-owned rules under `/run/udev/rules.d`,
removed `uaccess` before seat ACL processing, and enforced `root:keymasq`
mode `0660` without desktop ACLs. Rules covered recreated nodes as well as
existing ones. The probe ran as the `keymasq` service account; fresh access
tests ran as the ordinary `deck` user.

A separate transient system service owned recovery. A verified timer was
armed before each mutation. Recovery stopped the probe, restored the saved
driver mode when applicable, removed the test rules, reapplied device
policy, restored saved raw/USB ACLs by interface identity, and restarted
the previously active Keymasq services. Initial no-op and permission-only
deadline restoration passed before driver reattachment was attempted.

## Mechanism comparison

| Trial | Measured result | Conclusion |
| --- | --- | --- |
| Restrict new opens | Fresh read and read/write opens failed on the three raw nodes and USB node. Steam kept both existing raw handles and completed 747 reads and 2 ioctls in three seconds without errors. | Permissions alone cannot take over an existing connection. |
| Synthetic main hidraw removal | With restrictions active, Steam retained the same two handles and completed 1,249 reads and 4 ioctls in five seconds without errors. Physical gamepad evdev remained absent. | A fake unplug notification did not make this Steam backend release ownership. |
| Main HID driver unbind/bind | Steam's raw handles disappeared. The physical `Steam Deck` and `Steam Deck Motion Sensors` evdev nodes returned under the real USB/HID ancestry. Fresh desktop opens remained denied. | Targeted `hid-steam` reattachment can take this raw connection from running Steam. |
| Reattachment plus gamepad mode | The service-account probe read physical buttons and axes and produced virtual output that Steam opened and read. | The replacement path works once the input adapter enables reporting. |

The reattachment wrote the real main HID device ID to the `hid-steam`
driver's `unbind` and `bind` attributes. It destroyed and recreated that
driver's children, including its layered raw proxy. Restrictions were
already active when those nodes appeared. The physical USB device stayed
attached. Three reattachment trials returned physical input nodes; only
the final trial completed the end-to-end input proof.

There was no sustained usbfs holder in the inspected baseline. Denying a
fresh USB open does not prove revocation of an already-open usbfs handle.
That case needs a separate adversarial test before claiming complete USB
takeover support. Continuous reopen races were not tested either.

## Why the first input trial was silent

The first virtual-output probe opened and grabbed the correct physical
evdev node. Steam opened its virtual output. Nevertheless, the probe saw
zero button or stick events while the user continuously rotated the left
stick and pressed A.

The upstream v6.16 driver explains this behavior. `steam_input_open` does
not automatically disable lizard mode on the Deck. Its Deck input and
sensor handlers return without reporting when `gamepad_mode` is false and
the module's `lizard_mode` parameter is true. A returned event node is
therefore insufficient evidence that physical input is ready.
[Linux v6.16 hid-steam source](https://github.com/torvalds/linux/blob/v6.16/drivers/hid/hid-steam.c).

For the successful lab trial, the helper saved `Y` from
`/sys/module/hid_steam/parameters/lizard_mode` and temporarily wrote `N`
after reattachment. Physical input then arrived. Recovery restored `Y`.
The source also provides a per-device toggle by holding the options button,
but that path was not tested. The module parameter affects every device
managed by `hid-steam`; it must not become an unqualified per-device GUI
switch. Production work must choose a scoped input-mode mechanism or
explicitly account for that shared setting and its lifetime.

## Physical A reached Steam as virtual B

The temporary C probe validated the physical `28de:1205` source, grabbed it,
and created a minimal uinput gamepad named `Keymasq masking test`, with
Xbox 360 IDs `045e:028e`. It forwarded only A as B and logged left-stick
input. It did not implement full Xbox emulation, axis forwarding, rumble,
or vendor features.

During the successful trial:

- `/dev/input/event5` belonged to the physical Deck under USB `3-3:1.2`.
  Probe PID `1111006`, user `keymasq`, held that source.
- `/dev/input/event10` belonged to the probe's virtual uinput controller.
  Steam PID `1100966` opened it as descriptor `54`.
- Fresh `deck` read opens failed on raw nodes 1, 2, and 3, physical gamepad
  event5, motion event8, and the physical USB node.
- The probe recorded 28 complete physical A press/release pairs and
  30,840 left-stick axis events before restoration.
- An independent `evtest`, running as `deck`, captured virtual
  `EV_KEY / BTN_EAST / 1` and `EV_KEY / BTN_EAST / 0` at 23:49:14 CEST.
  It observed no virtual A event during its window.
- A syscall trace of Steam captured twelve successful 32-byte reads from
  virtual event10, covering six B press/release pairs from 23:49:14 to
  23:49:22. These contained the 32-bit input records for button code 305
  and synchronization. Empty nonblocking reads returned `EAGAIN` normally.

This correlates physical input, probe output, an independent reader, and
Steam's actual reads. A Steam controller-tester screenshot or visual
confirmation of B was not collected. Steam's UI listing and cache behavior
still need an explicit acceptance check.

## Recovery with a stopped worker

At 23:49:54 CEST, a 20-second recovery timer was armed and the worker received
`SIGSTOP`. Its process state was `Ts`. The SSH command then exited.

At 23:50:14, the independent recovery service stopped the worker. systemd
allowed it to resume and exit cleanly, so this exercises recovery from a
stopped process, not an uninterruptible kernel wait or a worker ignoring
termination. Recovery completed at 23:50:16.

The recorded postconditions were the original `lizard_mode=Y`, removal of
both test rules, restored root ownership and `deck` ACLs on raw and USB
nodes, and both original Keymasq services active. Steam PID `1100966`
reopened physical hidraw3. The virtual test controller was withdrawn when
the worker exited. A final three-second raw trace recorded 748 successful
Steam reads and 2 ioctls with no errors. The user subsequently confirmed
that the Deck controls worked normally again in the desktop session.

## What follows for the implementation

Keep hardware inventory independent of evdev and remapping configuration.
For the supported Deck adapter, activation needs a reservation policy,
targeted takeover, input-mode setup, verified physical input readiness,
and then virtual output. The settings page must distinguish reservation
from successful takeover and usable input.

Preserve recovery state for mode changes as well as ACLs and rules. A
supervisor outside the remap loop must restore access after deadline or
failure, and emergency recovery must suspend automatic reactivation.

Next tests are repeated Desktop Mode transitions, Gaming Mode, Steam
restarts while masked, continuous reopen attempts, retained usbfs handles,
suspend/resume, session loss, forced worker death, and partial transition
failures. The proposed 100-cycle runs have not been performed. Neither
generic hardware support nor SDL's individual backends have been tested
by this experiment.

The lab sources and detailed logs are retained locally under
`/tmp/keymasq-controller-research/live-tests/`. They contain assumptions
specific to this attachment and are not a supported administration tool.
No installed Keymasq implementation or profile was changed.
