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

Turn a device's switch **on** to mask it. The daemon allows 90 seconds to
quiesce input, complete the bounded hardware job, and acquire replacement readers.
Once input is ready, a separate 30-second confirmation window starts and an
inline strip asks whether it still works. Choose
**Keep masking** to confirm, or **Undo** to restore access. Missing the deadline
restores access even if the dialog closes. Confirmation saves the choice for
reconnects and restarts. Previously confirmed hardware can be switched back on
without repeating the confirmation; acquisition deadlines still protect it.
Refreshing interfaces during a manual trial does not extend its existing
confirmation window.

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
and watchdog recovery still apply. Automatic startup does not require another
confirmation or unlock; the original confirmation authorizes it.

Closing the GUI leaves confirmed masks active. Normal daemon/session shutdown
and suspend restore physical access; startup and wake reapply enabled masks when
the same user session is available.
If the masking monitor has failed, cleanup still releases its readers and
attempts hardware restoration before reporting the monitor error.

Runtime failure restores physical access before retrying remapping automatically.
The daemon releases the affected reservation's readers; unrelated readers stay
running. Once restoration finishes, retry starts after two seconds. Repeated
failures increase the delay up to one minute, and thirty seconds of healthy
masking resets it. The session reapplies its active configuration. A saved
automatic mask is retried when its hardware is present, without a GUI action.

When masking needs recovery, emergency reset first prevents new grabs,
neutralizes ordinary output, and releases input devices. A controller cleanup
failure does not skip other devices, and physical masking recovery runs afterward.
Emergency recovery also restores masks with no runtime readers, including
disconnected devices and masks
still acquiring their replacements. Per-device unmasking leaves unrelated
remapping active. When physical recovery completes, the session reapplies
profiles even if the device list has not changed. This does not clear an
existing global remapping stop. Before system suspend, ordinary output
neutralization is attempted before masking cleanup; both steps run even if
either fails.

With no active masking state or pending recovery records, emergency reset releases
devices and the session reapplies profiles normally. Saved, inactive mask history
alone does not pause remapping or require **Enable remapping**.

**Unmask all devices** turns every mask off and disables those saved choices,
without pausing ordinary remapping. Administrative recovery still provides the
emergency stop for an unresponsive daemon. If that stop is active, the dialog
shows a separate **Enable remapping** action. Recovery never resumes while the
helper is still repairing device access. Saved hardware mapping files and old
display history alone never authorize automatic masking.

## How it works

`keymasqd` owns masking reservations, confirmation deadlines, saved preferences,
and reconnect handling. There is no separate masking daemon. For privileged
operations it starts a short-lived `keymasq-hardware@.service` job through systemd.
The job invokes the existing `keymasq-record hardware-operation` entry point,
resolves the selected attachment again, records permissions and driver bindings,
and installs runtime udev rules. The root process exits when the operation ends.
Listing hardware and monitoring unchanged masks do not start privileged jobs.

The daemon allows 75 seconds for a systemd job request. Session requests allow
160 seconds so an in-flight job can finish before a recovery job runs; the GUI
allows 165 seconds for the session response. Inventory requests use the same
allowance because they can queue behind administrative work.

The rules reserve the selected device's hidraw, usbfs, evdev and legacy joystick
nodes for root and the dedicated `keymasq` account. They remove desktop ACLs and
match the USB port, model and serial when present, or the specific non-USB HID
instance. They exclude virtual outputs.

Within the daemon, `HardwareMasking` owns the polling loop and lifecycle cleanup.
`MaskCoordinator` manages independent `MaskReservation` policies, deadlines, and
transaction locks; `MaskRuntime` owns each reservation's readers and remapping
retries. `MaskRegistry` tracks runtime path ownership as interfaces move between
reservations and hardware configurations.

The coordinator uses a narrow backend interface. `SystemdMaskBackend` submits
privileged jobs, while `LinuxMaskBackend` implements the root transactions.
Both use the same read-only inventory helpers. The daemon and GUI share the
mask phase definitions.

The job sandbox permits writes to its own records, hardware request files,
runtime udev rules, and the existing `hidden` and `hidden-hardware` directories
under `/run/keymasq`. Recovery removes stale evdev hiding markers there before
restoring permissions. The rest of `/run/keymasq` remains read-only in the job.

Before arming rules for a connected device, the helper records its ownership and
static ACLs. Recovery restores and reads back that baseline, then lets current
udev policy grant desktop access. Saved ACLs never overwrite the result of that
policy pass. On nodes tagged `uaccess`, named-user grants are managed by current
desktop policy and are not replayed from the snapshot. Configure permanent user
grants on those nodes through udev rules so they are reapplied during recovery.
Static group ACLs and ACLs on nodes without `uaccess` are preserved.
Removing a desktop grant also preserves any ACL mask restriction on the owning
group's effective permissions.

Nodes first created while a saved mask is already armed have no earlier permission
record. Recovery resets them to `root:root`, mode `0600`, without extended ACLs,
then applies current udev policy. Permission restoration failures retain the
recovery journal and report an error for retry. USB hubs, including composite
devices with a hub interface, cannot be masked; discovery does not include input
interfaces belonging to downstream USB devices.

If a USB reconnect returns without its input drivers, recovery still attempts to
remove masking rules, restore permissions, and apply current udev policy. The
journal remains for retrying driver repair; bindings from the old connection are
never applied to a replacement connection.

Confirmed USB masks keep their rules armed while the device is unplugged.
The next connection receives the restrictions during initial udev processing,
before the desktop is granted access. Temporary USB connection numbers remain
part of transaction validation, but are not permission-rule matches. The saved
selector also allows enabling an already confirmed mask while unplugged.
Without a serial number, that saved selection identifies the port and model,
so another unit of the same model connected to that port inherits the choice.
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
The removed endpoints invalidate existing application handles; ordinary applications
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
transition. The helper disables hid-steam's lizard mode
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
applies saved hardware routes after takeover, including during automatic startup.
Turning off or unplugging a controller, changes to its interfaces, and ordinary
trial expiry release only the affected readers. They do not terminate the daemon.

Systemd watches `keymasqd` with a 20-second watchdog. Heartbeats run on the input
event loop and stop if that loop blocks. Waiting for a bounded hardware job or
its coordinator lock does not suppress heartbeats. On watchdog expiry systemd
kills the daemon, releasing every evdev grab and virtual output, then runs
`keymasq-record recover-hardware` to restore physical
access. The same cleanup runs after normal service shutdown. Startup also runs
recovery before opening input devices, and fails if recovery is incomplete.
The watchdog applies to ordinary remapping even when masking is never enabled.
It cannot diagnose every logical error in a still-responsive input loop.
Unexpected masking monitor errors stop remapping and attempt physical recovery;
cleanup errors are logged and the monitor keeps running. Invalid saved masking
state disables automatic masking for that attachment while physical recovery
still runs. Other attachments retain their saved preferences.

A device with only hidraw endpoints
can be reserved without a decoder or virtual output. That reserves access;
interpreting proprietary reports still requires an input driver. The daemon
also monitors endpoint disappearance or changes, including hidraw-only masks.

The daemon stores the confirmed startup preference in
`/var/lib/keymasq/masking/reservations/<attachment-id>/policy.json`, bound to the
authenticated desktop UID.
It is separate from the temporary permissions and undo journal under `/run`.
The journal restores an interrupted operation; it does not authorize startup.
Each reservation has its own `suspended` file. The top-level `suspended` file is
reserved for an explicit emergency stop of all remapping. Undo journals and ACL
restore files live under `/run/keymasq-masking/reservations/<attachment-id>/`.
Udev rule filenames include the attachment ID, so restoring one mask cannot
remove another mask's rules. Stopping the daemon stops its outstanding hardware
jobs before root-owned recovery runs.

This blocks ordinary applications from opening the covered physical nodes.
The hardware remains visible in sysfs and may remain in cached application
lists. Root and other privileged input services remain trusted. This is
system-wide device access control, not a per-application visibility filter.

## Administrative recovery

From SSH or a TTY, stop the daemon to release all grabs and restore access:

```sh
sudo systemctl stop keymasqd
```

If recovery reports an error, retry the short-lived helper while the daemon is
stopped:

```sh
sudo keymasq-record recover-hardware
```

If an undo journal is damaged, recovery still removes that reservation's runtime
rules. Separately saved attachment records must agree and match the current
physical device before recovery removes hidden markers, restores a valid saved
permission baseline, or reapplies current udev policy. A missing or conflicting
identity never authorizes changes to a replacement device.

The damaged journal and supporting records remain in place for diagnosis and
retry. Restoring access permissions does not establish the lost USB-port or
original driver state, so recovery still reports an error and daemon startup
remains blocked. The error identifies the journal and any access-restoration
steps that failed. Do not delete the journal to bypass this check; it may contain
the information needed to repair an interrupted hardware operation.

For the installed AppImage, use `/opt/keymasq/bin/keymasq-record`.
Restart `keymasqd` when ready to resume remapping. User profiles, hardware
configurations, and confirmed masking preferences are preserved.

## Trying a worktree build

Install the branch's hardware job unit and Polkit rule, then run `./scripts/dev.sh`.
The existing development launcher runs the daemon in the foreground as `keymasq`,
using the same sudo commands as before. It installs no service overrides or rules
and has no watchdog. The installed hardware job unit also supports a foreground
daemon; starting a job does not start the installed daemon service. A clean
foreground exit restores hardware access through those jobs.

Open Device masking, turn a device on, and try its controls during the first
confirmation countdown. Without Keep masking, access restores automatically.
Daemon logs appear directly in its terminal. Inspect hardware job failures with
`journalctl -u 'keymasq-hardware@*'`.

After stopping the development workspace, start `keymasqd` to use the installed
code.
