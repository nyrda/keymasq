# Hardware masking

Open **Settings > Hardware > Manage** to list physical input devices, including USB and Bluetooth
attachments. This list comes from sysfs, so the Steam Deck controller appears
even when Steam owns its raw interface and the kernel exposes no gamepad
event node. Listing hardware does not open its raw interfaces.

Masking does not require a known vendor, controller model, native decoder, or
evdev node. It reconnects the selected attachment's bound HID or USB input drivers and
reserves its device nodes for Keymasq. If the expected input interfaces cannot
be recreated, the trial restores access with an error.
There is no fixed limit on simultaneous reservations. Each physical attachment has
its own masking switch, confirmation, saved choice, and recovery journal.
Devices with identical vendor and product IDs are distinguished by their physical
attachment identities. Masking uses the existing Keymasq unlock policy, including
the SteamOS policy. Saved remapping configurations alone
never enable it.

Masking is available on the final hardware setup page, below the hardware name
in Hardware Settings, and in the global overview under Settings. Setup applies
pending choices after saving the hardware and keeps the page open for confirmation.
All three locations use the helper's same saved mask state. In setup and Hardware
Settings, hover over the Device masking group for an explanation of masking and
shared receivers.

Turn a device's switch **on** to mask it. On first activation, the independent
recovery service starts a 30-second deadline before changing device access.
Once input is ready, an inline strip asks whether it still works. Choose
**Keep masking** to confirm, or **Undo** to restore access. Missing the deadline
restores access even if the dialog closes. Confirmation saves the choice for
reconnects and restarts. Previously confirmed hardware can be switched back on
without repeating the confirmation; acquisition deadlines still protect it.

Turn the switch **off** to restore that device's access and disable its saved
mask. No separate pause or resume action is needed. A confirmed device keeps
its switch on while disconnected and shows **Waiting for device**. It is masked
automatically when it returns. The saved choice can also be changed while the
device is absent. First-time masking requires the device to be connected.

Failures appear below the device name, with the blocking application named when
available and a **Retry** button. Normal rows show the device name, a chevron,
and its switch on one line. Clicking the name or chevron expands the connection
and masking scope inline. The switch only changes masking. Waiting and reconnecting
states appear beside the name; ordinary on/off states need no extra text.
If an enabled mask stops while its device is connected, the row shows
**Not masked**. The saved switch alone does not indicate that takeover succeeded.
Details use plain text without automatic selection. Errors offer **Copy diagnostics**
for the full technical information. Switches and expanded details stay in place
while status and countdowns update. In Hardware Settings, these details appear
under **Hardware → Device details**, separate from the masking switches.

Keep an unaffected keyboard, mouse, or touch input available to confirm first-time
masking. A USB reservation covers every HID and input interface on that USB device,
which can include several controllers on a shared receiver. A shared hub, Bluetooth
adapter, and the Deck touchscreen remain outside the selected controller's scope.

Masking does not select a controller output or convert Deck controls into Xbox
controls. Before hardware setup, the reserved interfaces use ordinary Keymasq
passthrough with their kernel-reported capabilities. Add the controller through
the normal setup flow and choose its **Default output**, or change that choice
later in Hardware Settings. The saved hardware route works without a profile;
profiles override individual inputs through the existing remapping runtime.

A selected virtual output receives matching event codes, with axis ranges
scaled by the general controller router. Deck trigger and trackpad codes can
differ from an Xbox layout and need explicit mappings. Masking supplies no
Deck-specific conversion, output selection, or extra force-feedback support.
See [controller routing](GAMEPAD.md) for the output behavior and limits.

Removing a profile preserves the hardware's default route. Removing the hardware
configuration returns its reserved interfaces to ordinary setup passthrough.
Changing the default output releases held output state and keeps the physical
interfaces grabbed throughout the change. Unconfigured interfaces remain
reserved alongside configured sources.

Disabling or removing a virtual output keeps the physical mask and input
readers active. Inputs routed to that absent output are dropped; inspectors
and other mappings can still use the controller. Re-enabling the output
resumes its route without reconnecting the physical controller.

An unconfigured reserved interface appears as **Masked · Available to add**.
Adding gamepad or motion sources attaches their configuration to Keymasq's
existing readers. Button and analog learning use that same input stream, with
output suppressed during capture; they do not release the mask or recreate the
selected output. Ending capture returns to normal profile processing.
Interfaces already present in a hardware configuration retain the usual
duplicate-device protection.

The background user session reapplies enabled masks after reboot or service
restart, including in Steam Deck Gaming Mode without opening the GUI. It waits
for a working replacement before completing takeover. The acquisition deadline
and heartbeat recovery still apply. Automatic startup does not require another
confirmation or unlock; the original confirmation authorizes it.

Closing the GUI leaves confirmed masks active. Normal daemon/session shutdown
and suspend restore physical access; startup and wake reapply enabled masks when
the same user session is available.

Runtime failure restores physical access before retrying remapping automatically.
The daemon releases the affected reservation's readers; unrelated readers stay
running. Once restoration finishes, retry starts after two seconds. Repeated
failures increase the delay up to one minute, and thirty seconds of healthy
masking resets it. The session reapplies its active configuration. A saved
automatic mask is retried when its hardware is present, without a GUI action.

**Unmask all devices** turns every mask off and disables those saved choices,
without pausing ordinary remapping. Administrative recovery still provides the
emergency stop for an unresponsive daemon. If that stop is active, the dialog
shows a separate **Enable remapping** action. Recovery never resumes while the
helper is still repairing device access. Saved hardware mapping files and old
display history alone never authorize automatic masking.

## How it works

`keymasq-maskd` is a small root service separate from the input event loop. It
resolves the selected attachment again, records its current device permissions
and driver bindings, and installs runtime udev rules for the selected device.
The rules reserve its hidraw, usbfs, evdev and legacy joystick nodes for root
and the dedicated `keymasq` account. They remove desktop ACLs and match the
USB port, model and serial when present, or the specific non-USB HID instance.
They exclude virtual outputs.

Confirmed USB masks keep their rules armed while the device is unplugged.
The next connection receives the restrictions during initial udev processing,
before the desktop is granted access. Temporary USB connection numbers remain
part of transaction validation, but are not permission-rule matches. The saved
selector also allows enabling an already confirmed mask while unplugged.
Normal disconnection does not suspend the saved USB mask or remove its rules.
The rules also stay armed when a wireless controller connects to or disconnects
from a confirmed USB receiver while Keymasq reacquires its changed input interfaces.
For interface changes on the same USB connection, Keymasq verifies the existing
mask and reacquires readers without rebinding drivers or cycling the receiver.
This preserves gamepad nodes that the kernel creates when a wireless controller returns.
They apply to driver bind and unbind events as well as add and change events,
so later driver events cannot reapply the ordinary desktop access policy.
The helper removes armed rules on unmasking, owner failure, or service shutdown,
including when all masked devices are offline. While Keymasq is stopped, the
original devices remain available. Startup arms saved rules for the authenticated
session and performs takeover if applications already opened a connected device.

Permissions alone cannot revoke existing raw handles. The helper first waits
for the daemon to release this attachment's physical and native readers.
It then applies access restrictions and unbinds and binds the actual HID
drivers discovered in sysfs. USB input interfaces without a HID layer, such as
those managed by `xpad`, use the same handshake, journal, and driver rebind on
the USB bus. Discovery follows input ancestry rather than a model or driver
allowlist. Sibling audio and storage interfaces are not rebound. This briefly
interrupts the selected input hardware.
The removed endpoints invalidate existing application handles; applications
cannot reopen the replacements. Unrelated readers keep running during takeover.
The helper records every binding for recovery and verifies that the expected
interfaces return. USB rules cover hidraw, usbfs, evdev, and legacy joystick
nodes. Bluetooth and other HID reservations cover hidraw and input nodes.

When an existing direct USB handle prevents a HID rebind from completing takeover,
the helper cycles the selected device's individual USB port. It installs the
access rules first, then records the port path and its filesystem identity before
disabling it. Re-enumeration invalidates old usbfs handles and recreates the HID
and input drivers under the armed rules. The helper updates the reservation to
the new connection generation before handing it back to the daemon. This also
handles applications that detached the kernel HID driver.

A port cycle interrupts all functions on that USB device, including any audio or
storage functions. The helper refuses hubs as targets and hubs with ganged power
switching. It never falls back to cycling a parent hub. If port control is
unavailable, close the blocking application or reconnect the device manually.
Cancellation and crash recovery re-enable the recorded port before restoring
access. Recovery does not operate on a replacement hub at the same path.

Blocking hidraw on Bluetooth does not restrict direct Bluetooth socket access or a
privileged system broker.

The built-in Deck controller retains its tested main `hid-steam` interface
transition. The service disables hid-steam's lizard mode
for the reservation, then restores its previous value during recovery. Because
that setting is module-wide, masking refuses other attached hid-steam
controllers and recovers if one appears. For that Deck transition, existing
auxiliary HID handles require the USB port takeover because only the main
interface is rebound in the ordinary path. Other devices do not read or change lizard mode.

`keymasqd` acquires the resulting input interfaces through its normal runtime.
For evdev sources, Keep becomes available after every reserved interface has a live reader and
each emitting interface has a passthrough output or its configured virtual
output. A deliberately disabled virtual output and motion observation do not
require an output. A missing interface, stopped reader, or failed configured
output restores access. Readiness checks
wait for routing transactions to finish. The existing session reevaluation
applies saved hardware routes after takeover, including during automatic startup. A heartbeat to `keymasq-maskd` renews a 12-second lease even
after Keep. An unresponsive input daemon can be killed by the recovery service
to release its grabs and virtual outputs. Turning off or unplugging a controller,
changes to its interfaces, and ordinary trial expiry do not terminate a responsive
daemon. It releases affected readers and waits for the hardware to return.
A device with only hidraw endpoints
can be reserved without a decoder or virtual output. That reserves access;
interpreting proprietary reports still requires an input driver. The helper
also monitors endpoint disappearance or changes, including hidraw-only masks.
The recovery service has its own systemd watchdog and reconciles every
reservation's root-owned journal when it stops or starts.

The root service stores the confirmed startup preference in
`/var/lib/keymasq-masking/reservations/<attachment-id>/policy.json`, bound to the
authenticated desktop UID.
It is separate from the temporary permissions and undo journal under `/run`.
The journal restores an interrupted operation; it does not authorize startup.
Each reservation has its own `suspended` file. The top-level `suspended` file is
reserved for an explicit emergency stop of all remapping. Undo journals and ACL
restore files live under `/run/keymasq-masking/reservations/<attachment-id>/`.
Udev rule filenames include the attachment ID, so restoring one mask cannot
remove another mask's rules. The daemon lease is shared; losing that owner
restores every active reservation.

This blocks ordinary applications from opening the covered physical nodes.
The hardware remains visible in sysfs and may remain in cached application
lists. Root and other privileged input services remain trusted. This is
system-wide device access control, not a per-application visibility filter.

## Administrative recovery

From SSH or a TTY, run:

```sh
sudo keymasq-maskd --recover
```

For the installed AppImage, the explicit path is:

```sh
sudo /opt/keymasq/bin/keymasq-maskd --recover
```

The command restores access through the supervisor, or recovers its journal
offline if the supervisor is stopped. Automatic remapping remains paused.
User profiles and hardware configurations are preserved.

See [Deck experiment results](STEAM_DECK_MASKING_RESULTS.md) and the
[test plan](STEAM_DECK_MASKING_TEST_PLAN.md) for measured coverage and remaining
hardware validation.

## Generic HID trial results

On 2026-09-14, a live trial with an 8BitDo Ultimate 2 Wireless in DInput dongle
mode, USB `2dc8:6012`, reached the ready state and remained active for eight
seconds. The original open hidraw descriptor returned `ENODEV` after rebinding.
The desktop user could not open the replacement hidraw, evdev, joystick, or
usbfs nodes. Keymasq acquired the replacement evdev interface and its output
readiness checks stayed satisfied throughout the trial.

Explicit restoration made all four node types accessible again. This early
trial checked access control and runtime readiness. Later remapping and USB
takeover checks are recorded below; rumble and Bluetooth hardware remain untested.

A subsequent recovery test killed the development daemon while the mask was
active, then relaunched it. The helper reported `daemon_disconnected` and
restored access. The new daemon automatically cleared the failure pause,
reapplied the saved mask, and acquired both joystick and native input readers.
The mask remained active for eight seconds without a GUI Resume action. The
development launcher required the daemon relaunch; this test did not establish
automatic process restarting by that launcher.

After correcting the helper's disconnect handling, a physical controller
power-cycle also passed. The daemon PID stayed unchanged while the controller
was turned off and back on. Recovery cleared its pause automatically, reapplied
the mask, and reacquired joystick and native input readers without Resume.

## Multiple-device validation

Automated tests exercise 80 simultaneous masks without a fixed reservation limit,
several identical-model attachments, independent trial expiry and disconnect,
per-user saved startup policies, token isolation, and a slow recovery while
another attachment starts a trial. Runtime tests cover two reservations adopted
by one hardware configuration, independent reader readiness and release, and
multiple raw-only reservations. These tests do not establish compatibility with
every physical HID driver; live hardware coverage is recorded above.

On 2026-09-15, a live two-device test passed with the 8BitDo Ultimate 2 Wireless
in DInput dongle mode, `2dc8:6012`, and an Xbox 360 Wireless Receiver,
`045e:0719`, using the non-HID USB `xpad` driver. Both reservations remained
masked together for eight seconds. An Xbox evdev descriptor opened before
takeover returned `ENODEV` after rebinding. Desktop opens were denied for the
Xbox evdev, joystick, and usbfs nodes and the 8BitDo hidraw, evdev, joystick,
and usbfs nodes. Restoring only the Xbox made its physical nodes accessible
again. The 8BitDo remained masked with the same token, and the global remapping
pause stayed false. The Xbox trial used no persistent startup preference; the
8BitDo's existing persistent mask remained enabled. This checked access control
and daemon reader readiness, not game mappings or force feedback.

## USB takeover and mode-switch validation

On 2026-09-15, the user confirmed Steam Controller remapping after takeover with
an existing USB handle open. A later off/on test also returned the gamepad node
and restored remapping. After the receiver refresh fix, this transition retained
the receiver's kernel drivers while reacquiring its input interfaces.

The user also confirmed stable 8BitDo motion inspection and live remapping to
`virtual-gamepad-1`. The 8BitDo's DInput mode, `2dc8:6012`, and XInput mode,
`2dc8:310b`, share a stable gamepad path. They now have separate hardware
configurations, and Keymasq checks the opened input's vendor and product IDs
before assigning it to either configuration.

With both modes configured and masked, the user switched modes and confirmed
that Steam never picked up either physical mode. This covers the tested dongle
and host setup; it does not establish behavior for every USB hub or controller.

## Trying a worktree build

Run `./scripts/dev-maskd.sh` in a terminal, then start `./scripts/dev.sh`.
The first command requests sudo, stages this checkout's helper in a root-owned
runtime directory, and starts it under systemd with watchdog and stop recovery.
It stops the installed daemon and masking helper before replacing them.
Run both from the same checkout so they share the reader-quiescence handshake.

Open Device masking, turn a device on, and try its controls during the
first confirmation countdown. Without Keep masking, access restores automatically. Inspect helper
logs with `journalctl -fu keymasq-maskd-dev`. Verify physical opens fail as the
desktop user while the daemon's evdev and native sources remain usable.

After stopping the dev workspace, restore the installed services:

```sh
sudo systemctl stop keymasq-maskd-dev
sudo systemctl start keymasq-maskd keymasqd
```
