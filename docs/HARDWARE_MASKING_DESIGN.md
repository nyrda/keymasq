# Hardware masking design

This is the engineering reference for [hardware masking](HARDWARE_MASKING.md).
It records how the privileged transaction, the daemon-side coordination, and
recovery behave. The trust boundary of the root job is described in
[SECURITY.md](SECURITY.md).

## Components

`keymasqd` owns masking reservations, confirmation deadlines, saved preferences,
and reconnect handling. There is no separate masking daemon. For privileged
operations it starts a short-lived `keymasq-hardware@.service` job through
systemd. The job invokes the existing `keymasq-record hardware-operation` entry
point, resolves the selected attachment again, records permissions and driver
bindings, and installs runtime udev rules. The root process exits when the
operation ends. Listing hardware and monitoring unchanged masks do not start
privileged jobs.

Within the daemon, `HardwareMasking` owns the monitor loop and lifecycle cleanup.
`MaskCoordinator` manages independent `MaskReservation` policies, deadlines, and
transaction locks; `MaskRuntime` owns each reservation's readers and remapping
retries. `MaskRegistry` tracks runtime path ownership as interfaces move between
reservations and hardware configurations. The coordinator uses a narrow backend
interface: `SystemdMaskBackend` submits privileged jobs, while `LinuxMaskBackend`
implements the root transactions. Both use the same read-only inventory helpers.
The daemon and GUI share the mask phase and lifecycle definitions.

## Request budgets

Activation allows 90 seconds to quiesce input, complete the bounded hardware
job, and acquire replacement readers. A manual trial then has a separate
30-second confirmation window; refreshing interfaces during a manual trial does
not extend it. The daemon allows 75 seconds for a systemd job request. Session
requests allow 160 seconds so an in-flight job can finish before a recovery job
runs; the GUI allows 165 seconds for the session response. Inventory requests
use the same allowance because they can queue behind administrative work.

## Access rules

The rules reserve the selected device's hidraw, usbfs, evdev, and legacy
joystick nodes for root and the dedicated `keymasq` account. They remove desktop
ACLs and match the USB port, model, and serial when present, or the specific
non-USB HID instance. They exclude virtual outputs. Bluetooth and other HID
reservations cover hidraw and input nodes. Rule filenames include the attachment
ID, so restoring one mask cannot remove another mask's rules.

The rules apply to driver bind and unbind events as well as add and change
events, so later driver events cannot reapply the ordinary desktop access
policy. Temporary USB connection numbers remain part of transaction validation,
but are not permission-rule matches.

The job sandbox permits writes to its own records, hardware request files,
runtime udev rules, and the existing `hidden` and `hidden-hardware` directories
under `/run/keymasq`. Recovery removes stale evdev hiding markers there before
restoring permissions. The rest of `/run/keymasq` remains read-only in the job.

## Permission baselines

Before arming rules for a connected device, the helper records its ownership and
static ACLs. Recovery restores and reads back that baseline, then lets current
udev policy grant desktop access. Saved ACLs never overwrite the result of that
policy pass. On nodes tagged `uaccess`, named-user grants are managed by current
desktop policy and are not replayed from the snapshot. Configure permanent user
grants on those nodes through udev rules so they are reapplied during recovery.
Static group ACLs and ACLs on nodes without `uaccess` are preserved. Removing a
desktop grant also preserves any ACL mask restriction on the owning group's
effective permissions.

Nodes first created while a saved mask is already armed have no earlier
permission record. Recovery resets them to `root:root`, mode `0600`, without
extended ACLs, then applies current udev policy. Permission restoration failures
retain the recovery journal and report an error for retry.

## Takeover

Permissions alone cannot revoke existing raw handles. The helper first waits for
the daemon to release this attachment's physical and native readers. It then
applies access restrictions and unbinds and binds the actual HID drivers
discovered in sysfs. USB input interfaces without a HID layer, such as those
managed by `xpad`, use the same handshake, journal, and driver rebind on the USB
bus. Discovery follows input ancestry rather than a model or driver allowlist.
Sibling audio and storage interfaces are not rebound. USB hubs, including
composite devices with a hub interface, cannot be masked; discovery does not
include input interfaces belonging to downstream USB devices.

The removed endpoints invalidate existing application handles; ordinary
applications cannot reopen the replacements. Unrelated readers keep running
during takeover. The helper records every binding for recovery and verifies that
the expected interfaces return. If they cannot be recreated, the trial restores
access with an error.

When an existing direct USB handle prevents a HID rebind from completing
takeover, the helper cycles the selected device's individual USB port. It
installs the access rules first, then records the port path and its filesystem
identity before disabling it. Re-enumeration invalidates old usbfs handles and
recreates the HID and input drivers under the armed rules. The helper updates
the reservation to the new connection generation before handing it back to the
daemon. This also handles applications that detached the kernel HID driver.

A port cycle interrupts all functions on that USB device. The helper refuses
hubs as targets and hubs with ganged power switching, and never falls back to
cycling a parent hub. Cancellation and crash recovery re-enable the recorded
port before restoring access. Recovery does not operate on a replacement hub at
the same path.

The built-in Deck controller retains its tested main `hid-steam` interface
transition. The helper disables hid-steam's lizard mode for the reservation,
then restores its previous value during recovery. Because that setting is
module-wide, masking refuses other attached hid-steam controllers and recovers
if one appears. For that Deck transition, existing auxiliary HID handles require
the USB port takeover because only the main interface is rebound in the ordinary
path. Other devices do not read or change lizard mode.

## Disconnects and interface changes

Confirmed USB masks keep their rules armed while the device is unplugged. The
next connection receives the restrictions during initial udev processing, before
the desktop is granted access. Normal disconnection does not suspend the saved
USB mask or remove its rules. The saved selector also allows enabling an already
confirmed mask while unplugged. Without a serial number, that saved selection
identifies the port and model.

The rules stay armed when a wireless controller connects to or disconnects from
a confirmed USB receiver while Keymasq reacquires its changed input interfaces.
For interface changes on the same USB connection, Keymasq verifies the existing
mask and reacquires readers without rebinding drivers or cycling the receiver.
This preserves gamepad nodes that the kernel creates when a wireless controller
returns.

The helper removes armed rules on unmasking, owner failure, or service shutdown,
including when all masked devices are offline. While Keymasq is stopped, the
original devices remain available. Startup arms saved rules for the
authenticated session and performs takeover if applications already opened a
connected device. If a USB reconnect returns without its input drivers, recovery
still removes masking rules, restores permissions, and applies current udev
policy. The journal remains for retrying driver repair; bindings from the old
connection are never applied to a replacement connection.

## Reconciliation of saved masks

Automatic startup is a reconciler, not a retry loop. Each heartbeat the
coordinator scans sysfs once for all reservations and compares each saved policy
with observed presence. A presence transition resets that reservation's backoff.
Failures are classified by the helper's error code: `selector_missing` and
`selector_invalid` mean the record cannot be armed offline and are parked until
the device is connected and confirmed; every other failure backs off from two
seconds doubling to one minute. An explicit switch-on or Retry resets the delay.
The daemon checks for the root-recorded selector before submitting an arm job,
so an incomplete record never starts privileged work.

The daemon subscribes to kernel uevents over an unprivileged netlink socket and
rescans immediately when USB, HID, input, hidraw, or Bluetooth devices change.
With that subscription the periodic rescan runs every five seconds; without it
the daemon rescans every heartbeat. Deadlines still tick every second. Policy
files record when the device was last seen.

## Runtime readiness

`keymasqd` acquires the resulting input interfaces through its normal runtime.
For evdev sources, Keep becomes available after every reserved interface has a
live reader and each emitting interface has a passthrough output or its
configured virtual output. A deliberately disabled virtual output and motion
observation do not require an output. A missing interface, stopped reader, or
failed configured output restores access. Readiness checks wait for routing
transactions to finish. The existing session reevaluation applies saved hardware
routes after takeover, including during automatic startup.

A device with only hidraw endpoints can be reserved without a decoder or virtual
output. That reserves access; interpreting proprietary reports still requires an
input driver. The daemon monitors endpoint disappearance or changes, including
for hidraw-only masks. Turning off or unplugging a controller, changes to its
interfaces, and ordinary trial expiry release only the affected readers.

Removing a profile preserves the hardware's default route. Removing the hardware
configuration returns its reserved interfaces to ordinary setup passthrough.
Changing the default output releases held output state and keeps the physical
interfaces grabbed throughout the change. Disabling or removing a virtual output
keeps the physical mask and input readers active; inputs routed to the absent
output are dropped. Button and analog learning use the reserved input stream with
output suppressed and do not release the mask.

## Failure recovery

Runtime failure restores physical access before retrying remapping. The daemon
releases the affected reservation's readers; unrelated readers stay running.
Retry starts after two seconds, doubling up to one minute; thirty seconds of
healthy masking resets it. Invalid saved masking state disables automatic
masking for that attachment while physical recovery still runs.

Emergency reset first prevents new grabs, neutralizes ordinary output, and
releases input devices. A controller cleanup failure does not skip other
devices, and physical masking recovery runs afterward, including for masks with
no runtime readers. When physical recovery completes, the session reapplies
profiles even if the device list has not changed. This does not clear an
existing global remapping stop. Before system suspend, ordinary output
neutralization is attempted before masking cleanup; both steps run even if
either fails. With no active masking state or pending recovery records,
emergency reset behaves like an ordinary reset.

Systemd watches `keymasqd` with a 20-second watchdog. Heartbeats run on the
input event loop and stop if that loop blocks; waiting for a bounded hardware
job or its coordinator lock does not suppress them. On watchdog expiry systemd
kills the daemon, releasing every evdev grab and virtual output, then runs
`keymasq-record recover-hardware`. The same cleanup runs after normal service
shutdown, and stopping the daemon stops its outstanding hardware jobs first.
Startup also runs recovery before opening input devices, and fails if recovery
is incomplete. Unexpected masking monitor errors stop remapping and attempt
physical recovery; cleanup errors are logged and the monitor keeps running.

## Saved state

The daemon stores the confirmed startup preference in
`/var/lib/keymasq/masking/reservations/<attachment-id>/policy.json`, bound to
the authenticated desktop UID. The root job stores the selector it discovered
during activation in `/var/lib/keymasq-masking/reservations/<attachment-id>/`.
Undo journals and ACL restore files live under
`/run/keymasq-masking/reservations/<attachment-id>/`. The journal restores an
interrupted operation; it does not authorize startup. Each reservation has its
own `suspended` file; the top-level `suspended` file is reserved for an explicit
emergency stop of all remapping.

If an undo journal is damaged, recovery still removes that reservation's runtime
rules. Separately saved attachment records must agree and match the current
physical device before recovery removes hidden markers, restores a valid saved
permission baseline, or reapplies current udev policy. A missing or conflicting
identity never authorizes changes to a replacement device. The damaged journal
and supporting records remain in place for diagnosis and retry. Restoring access
permissions does not establish the lost USB-port or original driver state, so
recovery still reports an error and daemon startup remains blocked. The error
identifies the journal and any access-restoration steps that failed.

## Trying a worktree build

Install the branch's hardware job unit and Polkit rule, then run
`./scripts/dev.sh`. The development launcher runs the daemon in the foreground
as `keymasq` (see `DEVELOPMENT.md`). It installs no service overrides or rules
and has no watchdog. The installed hardware job unit also supports a foreground
daemon; starting a job does not start the installed daemon service. A clean
foreground exit restores hardware access through those jobs.

Open Device masking, turn a device on, and try its controls during the first
confirmation countdown. Without Keep masking, access restores automatically.
Daemon logs appear directly in its terminal. Inspect hardware job failures with
`journalctl -u 'keymasq-hardware@*'`. After stopping the development workspace,
start `keymasqd` to use the installed code.
