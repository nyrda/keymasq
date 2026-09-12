# Hardware masking and ownership proposal

Research and proposed design from 2026-09-08. Default output routing merged
on the base branch before the September 12 integration. The current implementation
is documented in [Hardware masking](HARDWARE_MASKING.md); this document also
records broader proposals that are not implemented. A bounded Desktop Mode experiment took physical input
from running Steam, mapped A to a virtual Xbox controller's B button, and
observed Steam reading that output. See the
[experiment results](STEAM_DECK_MASKING_RESULTS.md) for evidence and limits.

The recommended design is an explicit hardware masking service, available
before hardware is added for remapping. Hardware discovery, access policy,
takeover, and mapping configuration have separate lifetimes. Steam Deck
takeover while Steam remains running is the primary acceptance target.
Controller feature parity and output normalization belong to the controller
runtime. The output discussion below is historical proposal material, not the
implemented routing contract. Hardware masking now uses the merged hardware
`default_output` setting and ordinary passthrough before configuration.

The practical promise is that ordinary applications cannot open the physical
controller for input or control after isolation completes. Device names,
descriptors, sysfs entries, and cached application listings may remain
visible. This is not concealment from the operating system or root. Existing
open handles require a separate transition policy.

## Hardware discovery before configuration

Add a daemon-owned inventory rooted in sysfs and udev rather than evdev
enumeration. It should list physical USB and HID devices, Bluetooth HID
devices, and other supported input devices even when their event nodes are
absent or inaccessible. Read metadata without opening raw controller nodes.
On the Deck, opening the layered hidraw client can itself remove evdev input.

Represent a physical attachment independently of `HardwareConfig`. Keep its
stable identity, connection generation, transport, interfaces, node ancestry,
and mask support in the daemon. VID:PID is a model identity; use serial or
port identity to distinguish attachments. Identify raw proxy nodes and
composite siblings as related parts of the attachment. Treat virtual output
devices separately so Steam's `28de:11ff` outputs cannot substitute for the
physical Deck's `28de:1205` controller in this inventory.

Keep three distinct records:

| Record | Responsibility |
| --- | --- |
| Attached hardware | What is physically present, including hardware with no evdev source. |
| Mask policy and observed state | Which attachment the user wants reserved, its covered interfaces, active restrictions, and any unfinished takeover. |
| Remapping configuration | Buttons, axes, profiles, and output routing for an attachment already added to Keymasq. |

The existing `list_devices_for_recording` flow remains useful for input
capture. Add an inventory operation for hardware administration instead of
stretching recording discovery into that role. The GUI receives opaque
attachment IDs and a daemon-generated preview of affected interfaces. The
daemon validates identity and permissions again when applying a request.

## User-demand masking lifecycle

Provide Settings > Hardware masking, listing attached supported input
hardware and remembered disconnected selections. Each row offers Mask,
Unmask, status, and Add to Keymasq when an input backend becomes available.
The add-device flow can link to the same view when an expected device is
missing. A masked attachment remains listed even with zero event devices
and no remapping configuration. Its row survives reconnects and node changes.

For a Deck already owned by Steam, the row can initially say "Steam Deck
Controller, in use by Steam, input unavailable" when the holder is known.
Mask first blocks new access and then runs a supported takeover method.
Only after the raw owner releases, kernel input returns, and the adapter
enables actual gamepad reporting does Add to Keymasq become available for
evdev configuration. The Deck can expose an event node while suppressing
every gamepad event in its default lizard mode. If takeover fails, show
"New access blocked; existing application still connected" rather than
claiming that masking completed. Identifying holders is diagnostic and must
not be required to list or select the attachment.

Internally track desired policy and actual coverage separately: unmasked,
applying, waiting for release, masked, restoring, and recovery suspended.
Remapping status is independent. A user may intentionally leave a device
masked without adding it. On the Deck, the default must still leave a
usable controller: hand physical input to `hid-steam`, enable gamepad
reporting, and provide default Keymasq passthrough through the virtual
controller. This must work before the user creates hardware mappings or a
profile. The output implementation belongs to the controller work on the
other branches; it is a prerequisite for this default masking behavior.

Do not declare the Deck handover complete merely because an evdev node
exists. Verify the input backend and replacement output are ready, then
let the user confirm usability during the trial. If setup fails, restore
access. Intentionally reserving hardware without replacement input is a
separate choice whose effect must be clear.

The Deck controller mask must exclude the touchscreen. Keep and Restore
must be usable by touch, so controller failure still leaves a local way to
operate the trial dialog. The independent timer covers failures that also
prevent the user from operating that dialog.

The mask service can hold evdev grabs as part of ownership, including before
a mapping exists, to suppress existing event streams where that works.
Profiles consume this ownership rather than repeatedly acquiring and
releasing it. Existing raw handles still need a takeover backend. A generic
inventory and policy engine can share evdev, HID, and USB enforcement, while
Deck-specific handoff behavior remains in a device adapter. Listing a device
does not imply that every transport or kernel-mediated access path can be
masked. Report supported coverage and affected composite functions.

Store validated preferences separately from hardware mapping files. The
daemon owns the effective runtime policy, checks the owning connection's
authority, and serializes conflicting changes. Since device permissions are
system-wide, a mask also affects other local users; it is not a per-app rule.
For the first version, require a healthy owning session for active masks and
restore access on owner loss. Closing the GUI alone need not end that session.

Use Keymasq's existing general unlock flow for masking and honor its
configured policy, including the SteamOS policy. This proposal adds no
separate authorization system or per-device privilege grant. The privileged
service still validates attachment identity and affected interfaces. The
recorded owner can restore access after the unlock expires.

Remembering a preference must not create unconditional boot-time lockout.
Publish runtime rules only while ownership is active, and reconcile them on
startup and shutdown. Automatic activation must be ordered before Steam
when possible; if Steam starts first, use the same explicit takeover state.
Do not silently apply broader VID:PID restrictions when stable identity is
missing. Existing installations retain their behavior until explicit opt-in.

## Recovery independent of remapping

Treat the recovery path as part of the first mask implementation:

- Initial activation is a timed trial, such as 30 seconds, with Keep and
  Restore controls. Arm recovery before changing access. A supervisor
  outside the GUI and event-processing loop owns the deadline, so a frozen
  GUI or daemon does not strand input. Only explicit Keep confirms the
  trial; expiry restores access and suspends automatic reactivation until
  the user deliberately tries again. Let the user extend setup time.
- Provide Restore all hardware in the GUI and a documented administrative
  recovery command usable from SSH or a TTY even if the daemon is dead.
  The command removes runtime restrictions, restores device policy, and
  invokes only the recorded, supported reattachment steps that are needed.
- Emergency recovery first persists a suspension state, then cancels pending
  takeovers, releases grabs and outputs, and restores access. Reject queued
  stale mask requests. Profiles, hotplug, session reconnect, and daemon
  restart must not immediately reapply the restrictions. Require explicit
  resume; do not delete the user's saved configurations.
- Keep automatic cleanup on daemon failure and normal stop, plus startup
  reconciliation. A hung process needs supervisor/watchdog handling too.
  Deleting marker files alone cannot restore existing device permissions.
- A reserved keyboard shortcut or supported Deck button gesture is useful
  when the daemon receives that input. It cannot be the only escape hatch
  while Steam owns the raw device or the daemon is hung. The activation UI
  should show the actual recovery options available for that attachment.

The existing emergency reset is insufficient for this purpose:
`handle_runtime_reset_event` in `session/manager/events.py` immediately
reevaluates profiles after releasing grabs. Add a distinct recovery reason
and suspension behavior rather than reusing that path unchanged. Test that
recovery also prevents automatic grabs of restored physical devices.

## What SDL and Steam can access

| Path | How applications use it | Required treatment |
| --- | --- | --- |
| `/dev/input/event*` | SDL's Linux backend reads evdev events and queries capabilities; writable access also supports force feedback. | Grab the relevant input interfaces and deny other users new opens. |
| `/dev/input/js*` | Legacy joystick API; SDL supports selecting this backend. | Restrict corresponding joydev nodes too. |
| `/dev/hidraw*` | HIDAPI reads USB or Bluetooth HID reports and sends output and feature reports through controller-specific drivers. | Restrict every relevant HID interface, including interfaces without a matching gamepad event node. |
| `/dev/bus/usb/BBB/DDD` | libusb uses usbfs directly and can claim interfaces or detach kernel drivers where supported. | Restrict the controller's USB device node when that scope is safe. |
| Other physical input interfaces | A controller can also expose motion sensors, mouse, keyboard, or touch input. | Inventory them and explicitly decide which belong to the managed controller. |

SDL has multiple backends. Its Linux implementation selects evdev or classic
joystick operation and avoids devices already handled by another SDL driver.
It also associates separate sensor nodes with controllers. This explains why
changing only evdev visibility can leave a second route available.
[SDL3 Linux backend](https://github.com/libsdl-org/SDL/blob/main/src/joystick/linux/SDL_sysjoystick.c),
[SDL2 Linux backend](https://github.com/libsdl-org/SDL/blob/SDL2/src/joystick/linux/SDL_sysjoystick.c),
[classic joystick hint](https://wiki.libsdl.org/SDL3/SDL_HINT_JOYSTICK_LINUX_CLASSIC).

Hidraw provides raw HID reports independently of the input event stream.
An evdev grab does not reserve the HID transport. HIDAPI's Linux backend
opens hidraw nodes; SDL also includes a libusb backend. Its current default
prefers libusb for selected devices that need it, including GameCube
adapters. Exact selection depends on the SDL version, build, and hints.
[Kernel hidraw documentation](https://www.kernel.org/doc/html/latest/hid/hidraw.html),
[HIDAPI Linux source](https://github.com/libusb/hidapi/blob/master/linux/hid.c),
[SDL HIDAPI implementation](https://github.com/libsdl-org/SDL/blob/main/src/hidapi/SDL_hidapi.c),
[SDL libusb hint](https://wiki.libsdl.org/SDL3/SDL_HINT_HIDAPI_LIBUSB).

The libusb Linux implementation opens usbfs device nodes. Its interface API
supports detaching and reattaching kernel drivers. Restricting hidraw alone
therefore leaves a separate direct USB route.
[libusb Linux source](https://github.com/libusb/libusb/blob/master/libusb/os/linux_usbfs.c),
[libusb device handling](https://libusb.sourceforge.io/api-1.0/group__libusb__dev.html).

Steam Input is not fully open source. Valve's published udev rules explicitly
grant access to hidraw for Sony, Nintendo, Valve, and other controllers, and
include USB device access. That supports treating both as relevant Steam
access paths. It does not establish the backend used by every Steam build or
controller. Verify those combinations experimentally.
[Valve's Steam Input rules](https://github.com/ValveSoftware/steam-devices/blob/master/60-steam-input.rules).

SDL hints such as `SDL_JOYSTICK_HIDAPI=0` are useful for diagnosis. They are
application settings, can be overridden, and do not enforce ownership across
Steam or independently launched games. Do not change global environment
variables or edit Steam's rules as part of this feature.
[SDL HIDAPI hint](https://wiki.libsdl.org/SDL2/SDL_HINT_JOYSTICK_HIDAPI).

## What Keymasq already does

- [source_hiding.py](../keymasq/keymasqd/runtime/source_hiding.py) tracks an
  event node and its sibling `js*` nodes. It creates runtime markers and
  triggers udev only for the input subsystem. It has no hidraw or USB handling.
- [99-keymasq-hide-grabbed.rules](../udev/99-keymasq-hide-grabbed.rules)
  clears joystick classification, sets libinput ignore, removes `uaccess`,
  resets ownership and permissions, strips ACLs, and grants the daemon access.
  Its hotplug markers match controller models by VID:PID.
- [grabbed_device/device.py](../keymasq/keymasqd/runtime/grabbed_device/device.py)
  creates a passthrough clone with physical controller identity and
  capabilities, grabs evdev, then hides the input nodes. Unmapped events go
  to that clone. Hiding failures are logged rather than making the grab fail.
- [outputs.py](../keymasq/keymasqd/runtime/outputs.py) already creates separate
  Xbox 360 style outputs with `045e:028e`, USB bus identity, and version
  `0110`. These synthetic outputs do not advertise force feedback. The
  physical passthrough path has an existing feedback proxy.
- [virtual_gamepads.py](../keymasq/keymasqd/runtime/virtual_gamepads.py) routes
  omitted output IDs to `virtual-gamepad-1`. Explicit hardware IDs resolve to
  a grabbed passthrough output. Normalizing passthrough must account for both
  routes or a single controller can still feed separate outputs.
- [profile/grab_plan.py](../keymasq/session/manager/profile/grab_plan.py)
  selects interfaces according to active mappings and combos. Persistent
  controller ownership needs a lifetime independent of those selections.

The current gamepad documentation describes source hiding more broadly than
the implementation supports for raw HID and USB. Correct that claim when
shipping this work, and distinguish input-node hiding from exclusive access.

## Proposed isolation mechanism

Keep the physical kernel driver bound so Keymasq can continue reading evdev
and forwarding force feedback. Let udev enforce access for a physical device
group, while the dedicated `keymasq` account retains the required access.

First build a daemon-owned topology record. Resolve sysfs ancestry from the
selected input device to its HID device, USB interfaces, and USB device, or
to its Bluetooth HID device. Enumerate related nodes rather than matching
names or assuming every node has `ID_INPUT_JOYSTICK`. Account for legacy
`hiddev` nodes if the device exposes them. Store subsystem, sysfs path,
major/minor, and connection identity; revalidate before mutations so recycled
`eventN` or `hidrawN` names cannot affect another device.

Use serial or stable connection identity where available. VID:PID identifies
a model, not a particular controller. Existing model-wide flags must not
silently become model-wide raw-access restrictions. If identity is ambiguous,
offer a port-bound choice or an explicit all-matching-controllers choice.
Do not infer this permission from an existing evdev grab.

USB permissions apply to the whole usbfs device node, not one interface.
A shared receiver may carry several controllers or unrelated devices.
Restricting its raw node affects all userspace clients of that receiver.
Report this scope before enabling the setting; leave unsupported shared
topologies out of automatic isolation. Never ascend to and restrict the
system Bluetooth adapter. Its USB node is not the individual controller.

Use installed, narrowly scoped rules plus validated daemon-owned runtime
policy. Deny read and write opens by ordinary users, remove desktop access
tags and pre-existing named-user ACLs, and retain daemon access. Group
permissions must not accidentally allow members of `input` or a vendor
group through. Classification flags improve application discovery but do
not replace permissions.

Rule ordering needs a prototype. systemd's seat rule schedules its access
helper when `uaccess` matches. Remove access tags before that stage for
selected devices, then enforce final permissions and ACL cleanup after
vendor rules. Account for additional access tags on newer systems.
`MODE:=` can prevent later rule assignments, but does not by itself remove
ACLs or override another rule's external command. Verify installed rule
behavior rather than assuming a `99-` filename wins every case.
[systemd seat rule](https://github.com/systemd/systemd/blob/main/rules.d/73-seat-late.rules.in),
[systemd access helper](https://github.com/systemd/systemd/blob/main/src/udev/udev-builtin-uaccess.c),
[udev manual](https://man7.org/linux/man-pages/man7/udev.7.html).

For reconnects, the policy must exist before the add events grant desktop
access. Watching hotplug in the daemon and applying chmod afterwards leaves
a race. Matching must work at the USB and HID stages, before evdev children
necessarily exist. Use a short local matcher if static rules cannot express
the identity. No long-running service work inside a udev rule. Until tested,
do not promise an atomic transition across all interfaces or zero exposure
during arbitrary local udev rules.

Reuse the daemon's current privilege boundary. `CAP_DAC_OVERRIDE` supports
opening restricted nodes and triggering uevents; it does not grant arbitrary
chmod, chown, or ACL ownership changes. Keep those operations in udev's
privileged context. [SECURITY.md](SECURITY.md) explicitly retains the single
capability across all packages. This proposal does not require adding one.

## Activation, existing handles, and recovery

Read-only inspection of the target Steam Deck on 2026-09-05 found SteamOS
3.8.14 with kernel `6.16.12-valve24.4-1-neptune-616-gfe145653a794`.
Steam was not running during the initial inspection. The built-in controller
was USB `28de:1205` at `3-3`, with `usbhid` transport and `hid-steam` bound.
Its keyboard and mouse interfaces exposed `hidraw1` and `hidraw2`. The main
controller's layered raw client exposed `hidraw3`; kernel gamepad and sensor
inputs were present as `event18` and `event19`. These node numbers are only
the observed connection's assignments. The controller also has CDC serial
interfaces, so resetting the entire USB device has wider scope than HID.

No process held those three hidraw nodes or the controller's usbfs node in
the initial lsof snapshot. The desktop account had read/write ACLs on all
four nodes. Keymasq was running but held only its output-related device
handles, with no physical controller handle in that snapshot.

Upstream Linux 6.16's `hid-steam` explains an additional ownership issue.
Opening its layered raw client schedules removal of the kernel gamepad and
sensor devices. While a client is open, reports go to that client instead
of the driver's evdev processing. Closing the last raw client schedules
recreation of the input devices. The launch observation below confirms the
removal side on the target's Valve kernel. An evdev grab alone cannot
protect against a driver unregistering the input device itself. Diagnostic
code must also avoid casually opening the layered hidraw node, since that
open can change input availability even without sending any command.
[Linux 6.16 hid-steam source](https://github.com/torvalds/linux/blob/v6.16/drivers/hid/hid-steam.c).

With user authorization, Steam was then launched in the existing KDE
Wayland desktop session through a transient user service. Steam performed
its automatic client update before starting. The existing Keymasq daemon,
session, and profiles were left running. No access restrictions, synthetic
events, device resets, or driver changes were applied by the investigator.

The live observation confirmed:

- Steam PID `1064730` held two read/write descriptors, `83` and `104`, to
  `/dev/hidraw3`. A three-second strace summary restricted to that node
  recorded 746 reads and two ioctls, with no reported errors. No controller
  report payload was collected by that summary.
- Kernel and udev monitors recorded real removal of physical
  `input183/event18`, `input183/js1`, `input184/event19`, and `input184/js2`.
  The controller's raw HID interface remained present. The keyboard and
  mouse input interfaces also remained present.
- Steam held two `/dev/uinput` descriptors and exposed virtual controllers
  named `Microsoft X-Box 360 pad 1` and `Microsoft X-Box 360 pad 0`, both
  with input IDs `28de:11ff`. They reused `event18` and `event19`, now under
  `/sys/devices/virtual/input/input185` and `input186`. Node names alone
  would falsely suggest that the original physical inputs survived.
- Keymasq's existing profiles automatically grabbed those Steam outputs.
  Its journal reported successful grabs for `28de:11ff` and `28de:11ff@2`.
  Keymasq created passthrough clones at `event21` and `event22`, identifiable
  by `phys=py-evdev-uinput`. The observed ordering was physical controller,
  Steam raw input, Steam virtual controller, Keymasq grab, Keymasq clone.
- Steam also held the pre-existing `keymasq-gamepad` output at `event10`.
  This snapshot does not establish which physical or virtual source fed
  each Steam output, or demonstrate an event feedback loop.
- The handle snapshots showed no process holding the controller's direct
  usbfs node or its keyboard/mouse hidraw nodes. That establishes the
  observed sustained path, not the absence of transient startup opens.

Both observer commands ended after their timeouts, and Steam was left
running. This September 5 baseline confirmed raw HID ownership and kernel
input removal. The September 7 follow-up tested restrictions, synthetic
removal, targeted driver reattachment, remapping, and restoration. Its
[results](STEAM_DECK_MASKING_RESULTS.md) establish the Desktop Mode path;
Gaming Mode and repeated transition testing remain outstanding.

Permission changes do not revoke an already-open device. A process may keep
reading HID reports or issuing USB requests through its existing handle.
Even evdev grabbing is event routing rather than removal of every possible
ioctl on another client's descriptor. A successful permission update is not
proof that Steam has released the device.
[chmod semantics](https://man7.org/linux/man-pages/man2/chmod.2.html),
[evdev implementation](https://github.com/torvalds/linux/blob/master/drivers/input/evdev.c).

Current upstream hidraw includes `HIDIOCREVOKE`; Linux 6.12's implementation
does not. Where supported, it revokes the open file description passed to
the ioctl, not all clients of a device. Opening hidraw again in Keymasq and
revoking that descriptor does not revoke Steam's separately opened handle.
`EVIOCREVOKE` has the same client-specific limitation. These are useful to a
broker that originally handed out descriptors, not a universal takeover API.
[upstream hidraw implementation](https://github.com/torvalds/linux/blob/master/drivers/hid/hidraw.c),
[Linux 6.12 hidraw implementation](https://github.com/torvalds/linux/blob/v6.12/drivers/hid/hidraw.c).

Synthetic unplug notifications deserve a separate prototype. Linux can emit
a synthetic `remove` uevent through a targeted `udevadm trigger`. SDL's
evdev discovery processes remove notifications, but its HIDAPI discovery
uses them to request re-enumeration. If the physical HID device still appears
in that enumeration, SDL can retain the existing device and open handle.
A notification therefore cannot establish exclusive ownership. Steam's
behavior needs measurement for each relevant backend.
[Kernel uevent implementation](https://github.com/torvalds/linux/blob/master/lib/kobject_uevent.c),
[SDL udev handling](https://github.com/libsdl-org/SDL/blob/main/src/core/linux/SDL_udev.c),
[SDL HID discovery](https://github.com/libsdl-org/SDL/blob/main/src/hidapi/SDL_hidapi.c),
[SDL HID device reconciliation](https://github.com/libsdl-org/SDL/blob/main/src/joystick/hidapi/SDL_hidapijoystick.c).

In the September 7 Deck trial, this synthetic removal left Steam's raw
handles working. Rebinding only the main `hid-steam` interface under active
restrictions removed those handles and returned physical evdev input.
The adapter then had to disable lizard mode before events arrived. See the
[measured result and shared-mode limitation](STEAM_DECK_MASKING_RESULTS.md).

These events are system-wide. systemd-udevd processes remove events by
cleaning its device database, tags, and node links. A fake removal needs
tested restoration and must not make Keymasq's own hotplug handling release
its reservation. It is not merely a message to Steam.
[systemd remove handling](https://github.com/systemd/systemd/blob/main/src/udev/udev-event.c).

A real software reconnect is a different possible transition backend.
Install isolation policy first, disconnect the selected device, rediscover
and acquire its new nodes under that policy, then publish virtual output.
Driver unbind/rebind can destroy input and hidraw children without removing
the USB device itself. Do not assume that it, USB reset, or an authorization
toggle invalidates an existing usbfs handle. Verify actual removal and
re-enumeration for any backend advertised as a complete reconnect. Shared
receivers and composite devices require explicit scope handling. Keep this
an opt-in, supported-device operation until hardware testing establishes
which transitions close every relevant old access path.
[USB lifecycle](https://github.com/torvalds/linux/blob/master/drivers/usb/core/usb.c),
[usbfs handle lifecycle](https://github.com/torvalds/linux/blob/master/drivers/usb/core/devio.c).

Use this activation sequence:

1. Resolve identity and affected nodes, check support for the required
   takeover and input backends, and record restoration data for existing
   nodes. An evdev node and remapping configuration need not exist yet.
2. Install the reservation policy, restrict current nodes, await bounded
   udev completion, and verify effective access. Keep the UI in an applying
   state until this finishes. A failed trigger must be a reported failure.
3. Acquire required input interfaces. If Steam has an evdev grab or a USB
   backend has detached the kernel driver, report that ownership is pending.
   Apply isolation before asking the user to reconnect the controller or
   close and reopen the affected application. Do not kill Steam automatically.
4. Check for existing users where permitted. `/proc` inspection can assist
   diagnostics, but the daemon's current privileges and proc restrictions
   may prevent a complete scan. No observed handle is not proof of absence.
   Reconnecting under an already active policy gives a clearer transition.
5. Configure the supported adapter's input mode, including restoration
   data for any firmware or driver setting changed. Verify that input is
   usable; node existence alone does not establish this on the Deck.
6. Expose ready input sources to the add-device flow. If the device already
   has a remapping configuration, resume its runtime and output. On failure,
   roll back the attempted activation, or retain a clearly labelled pending
   reservation only if the user selected that behavior. Never report
   exclusive access while knowingly leaving an accessible physical route.

On release, send neutral output and stop effects, withdraw the virtual
controller, release physical handles, remove runtime policy, and restore
access. Reapply current distro rules and verify mode, ownership, ACLs, and
classification. Rules may not reconstruct manual ACLs or original owner
values, so retain narrowly scoped snapshots where needed. Restore only to
the same live device identity; do not overwrite newer administrator changes
or a newly attached device that reused a node name.

Cleanup must also run after daemon crashes, cancellation, startup failure,
session disconnect, uninstall, and reboot. Add service-managed cleanup,
such as an idempotent `ExecStopPost` recovery command, rather than depending
only on Python `finally`. Missing runtime markers alone do not restore the
permissions of a live node. Test the cleanup command's service sandbox and
capabilities in every maintained package. A recovery failure should remain
visible and retryable. Match Keymasq's existing release-on-owner-loss policy.

## Controller output integration, handled separately

The masking page above works before hardware configuration. Once added,
the existing Hardware Settings dialog can link back to that same policy
and offer output configuration:

| Control | Proposed behavior |
| --- | --- |
| Hardware masking | Open this attachment's policy in Settings > Hardware masking. |
| Controller output | Xbox 360 compatible, original evdev layout, or no controller output for keyboard/mouse-only use. |
| Status | Show the mask service's actual state separately from remapping and output readiness. |
| Release controller | End the reservation and return normal physical access. |

Keep ownership while the Keymasq session manages the controller, including
when no conditional profile matches. For an added controller with output
configured, forward input with an empty mapping layer. A masked controller
that has not been added uses ordinary passthrough during setup. Changing window
focus must not destroy and recreate output devices. Closing the GUI follows existing session behavior; stopping the
owning session releases the reservation. No permanent hiding merely because
a device was once configured.

One managed physical gamepad should normally produce one normalized output.
Both unmapped controls and remapped controller actions should reach it.
Explicit cross-device routing remains available. Keep existing global
`virtual-gamepad-N` outputs for keyboard/mouse synthesis and explicit
multi-controller setups; do not automatically funnel every controller into
slot one. Existing configurations retain their routing. A migration must
make the new default target explicit rather than silently changing omitted
`output_id` semantics.

Reuse output construction and feedback machinery, but add a canonical
gamepad conversion layer. Map buttons by physical position, convert D-pad
buttons or hats, and use the hardware's axis roles, ranges, center, rest,
and inversion to produce consistent sticks and independent triggers. Do not
apply the same deadzone twice. Preserve report boundaries, initial state,
resynchronization after dropped events, and neutral state on disconnect.
[Linux gamepad specification](https://docs.kernel.org/input/gamepad.html).

Do not assume the existing 17-button capability set is an exact xpad layout.
Validate button ordering, hats, trigger axes, input properties, bus/version,
and SDL mappings against a real Xbox 360 evdev device. Normalized outputs
backed by hardware need rumble routing to that hardware. Advertise only
effects the proxy can deliver. Shared outputs need an explicit rumble target.

Uinput creates a Linux input device; it does not emulate an Xbox USB
protocol or create a physical USB/hidraw counterpart. Xbox identification
can help compatibility, but it is not a universal compatibility guarantee.
Exclude Keymasq outputs by daemon-owned identity and virtual ancestry, not
VID:PID, since a physical Xbox controller may have identical IDs.
[uinput documentation](https://docs.kernel.org/input/uinput.html).

The original evdev layout option preserves input capabilities rather than
full vendor protocol behavior. An Xbox-compatible output cannot represent
native gyro, touchpad, adaptive triggers, controller audio controls, or all
extra buttons. Raw-access isolation also blocks vendor configuration tools
until released. Keep wheels and flight sticks on their existing layout by
default. If a device requires a userspace driver and exposes no usable evdev
controller, isolation alone is insufficient. Mark it unsupported until a
daemon-owned HIDAPI/libusb backend exists. Such a backend can be a later
adapter feeding the same normalized state model.

## Lower-level alternatives

| Mechanism | Assessment |
| --- | --- |
| Device cgroup policy | Can deny device opens to a managed process tree. Requires control over how Steam and games launch; does not make sysfs disappear or universally revoke old handles. Useful for an optional launcher, not the default desktop integration. |
| Mount namespace filtering | Can hide selected device paths inside a sandbox. Apps launched outside it remain unaffected, and existing handles survive. |
| HID-BPF | Can alter reports and gate raw requests. It is not a ready-made per-client exclusive grab. Dropping incoming reports indiscriminately can also starve kernel input processing; usbfs is a separate path. |
| Targeted HID driver unbind/bind | The main `hid-steam` interface passed the Deck experiment when restrictions preceded reattachment and input mode was configured afterwards. Keep this a tested adapter within explicit masking activation. |
| USB deauthorization | Removes broader USB functionality and the input source until reauthorization. Composite scope, existing usbfs access, and restoration require separate testing. |
| UHID virtual controller | Useful for a future richer HID output, but requires descriptor/report and output-request handling. It does not isolate the physical device. |
| LSM/BPF or a custom kernel module | A possible separate investigation if revoking arbitrary existing access without reconnect becomes mandatory. Needs kernel support, more privilege, and coverage of read, write, ioctl, and USB paths. It conflicts with the present deployment simplicity. |

These assessments follow the documented scope of
[device cgroups](https://docs.kernel.org/admin-guide/cgroup-v2.html#device-controller),
[HID-BPF](https://docs.kernel.org/hid/hid-bpf.html),
[USB authorization](https://docs.kernel.org/usb/authorization.html), and
[UHID](https://docs.kernel.org/hid/uhid.html). Namespace and kernel-policy
approaches would need their own threat model and proof of behavior.

## Implementation order and acceptance criteria

The [Steam Deck experiment plan](STEAM_DECK_MASKING_TEST_PLAN.md) defines
the first mechanism tests, automatic restoration, and evidence required
before implementing the full UI.

First prototype physical inventory, recovery, and takeover on the Deck
without requiring a settings UI. Once takeover works while Steam remains
running, implement Settings > Hardware masking, including devices with no
evdev node, followed by adding recovered input to Keymasq. Use a VM for
additional failure and recovery tests. Other branches
handle controller feature parity and normalized output; this work needs
only enough output to verify ownership and routing. Expand the supported
mask and takeover backends after the Deck case is established.

The isolation implementation belongs beside `runtime/source_hiding.py` with
a physical inventory, dedicated policy store, topology resolver, and
ownership transaction. The session requests ownership; the daemon resolves
and validates affected paths itself. Extend existing grab planning,
topology diagnostics, and service cleanup. The GUI should never send
arbitrary paths for privileged permission changes.

The handoff gate for Python implementation remains `./scripts/check.sh`.
Use the project's Nix integration environment for meaningful behavior cases:

- List and select the Deck while Steam owns its raw interface and there is
  no physical gamepad event node. Keep the row visible through masking,
  source creation, registration, and reconnect.
- Verify timed rollback with GUI death, a hung daemon, and a lost session.
  Restore all must prevent immediate reapplication by profiles, hotplug,
  pending requests, or automatic restarts until explicit resume.
- As an ordinary desktop user, new read-only and read/write opens fail for
  every covered physical node, even with `input` group membership or prior
  desktop ACLs. The daemon still receives input and can send supported rumble.
- Run SDL2, SDL3, classic joydev, HIDAPI, and libusb probes independently.
  Enumeration is recorded separately from successful event access. Exercise
  both native Steam and sandboxed Steam, plus representative Proton games.
- Start the application before isolation, after isolation, and during
  reconnect. Record existing-handle limitations honestly and verify the
  pending/reconnect UI. Do not interpret a denied new open as revocation.
- Exercise two identical controllers, composite interfaces, shared
  receivers, Bluetooth, USB reconnect, suspend/resume, seat changes, daemon
  crash, failed udev triggers, cancellation, and uninstall recovery.
- Verify one intended controller output per managed gamepad, correct full
  axis ranges and trigger rest, D-pad behavior, remapped and unmapped output
  routing, rumble, and no output churn during profile switches.

The first release should promise supported-device access isolation after a
completed ownership transition. A guarantee that the physical controller
vanishes from all userspace discovery, or that arbitrary existing handles
are revoked immediately, needs a different and more intrusive design.
