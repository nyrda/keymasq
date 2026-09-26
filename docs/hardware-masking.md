# Hardware masking

Masking stops other applications from opening a physical input device while
Keymasq keeps access for remapping. It is system-wide device access control. The
hardware stays visible in sysfs and may linger in cached application lists, but
ordinary applications can no longer read it. Root and other privileged input
services remain trusted.

Masking works for USB and Bluetooth devices without needing a known vendor,
controller model, native decoder, or evdev node. Each physical attachment has
its own switch, confirmation, and saved choice, so devices with identical vendor
and product IDs are handled separately. There is no fixed limit on how many
devices can be masked at once. Masking uses the normal Keymasq unlock policy,
including on SteamOS. Saved remapping configurations alone never enable it.

## Where to find it

Open **Settings > Hardware > Manage** for the overview of all physical devices.
The list comes from sysfs, so the Steam Deck controller appears even when Steam
owns its raw interface. Listing hardware does not open any device.

The same switch is also on the final hardware setup page and below the hardware
name in Hardware Settings. All three places show the same saved state. Setup
applies pending choices after saving the hardware and keeps the page open for
confirmation.

## Turning masking on

Keep an unaffected keyboard, mouse, or touch input available before masking a
device for the first time. The device must be connected.

Turn the device's switch **on**. Keymasq briefly interrupts the device while it
takes over its drivers, and allows 90 seconds for the replacement input to become
ready. Once input works, a 30-second confirmation window starts and the row asks
whether the device still works. Choose **Keep masking** to confirm, or **Undo**
to restore access. Missing the deadline restores access, even if the window was
closed.

Confirmation saves the choice. A confirmed device can be switched on again later
without repeating the confirmation, including while it is unplugged.

If another application holds the device open, the row names it when possible and
offers **Retry**. Close that application and retry. For USB devices, Keymasq can
also reconnect the device's individual port to force the takeover. This briefly
interrupts every function of that device, including audio or storage. If port
control is unavailable, reconnect the device by hand. **Copy diagnostics**
collects the technical details for a bug report.

## Turning masking off

Turn the switch **off** to restore the device's access and disable its saved
choice. There is no separate pause. **Unmask all devices** does this for every
device at once, without pausing ordinary remapping.

## What a mask covers

A USB reservation covers every HID and input interface of that USB device. On a
shared wireless receiver that includes every controller paired to it. A shared
hub, a Bluetooth adapter, and the Deck touchscreen are outside the selected
controller's scope. USB hubs cannot be masked.

Blocking Bluetooth hidraw access does not restrict direct Bluetooth socket
access or a privileged system broker.

Masking does not choose a controller output or convert Deck controls into an
Xbox layout. A masked device passes through with its kernel-reported
capabilities until you add it through the normal hardware setup and choose a
**Default output**. Unconfigured masked interfaces appear as **Masked ·
Available to add**. See [controller routing](gamepad.md) for output behavior.

## Saved masks, restarts, and reconnects

The background user session reapplies confirmed masks after a reboot or service
restart, including in Steam Deck Gaming Mode, without opening the GUI or
confirming again. Closing the GUI leaves masks active. A normal shutdown or
suspend restores physical access, and startup or wake reapplies enabled masks.

A confirmed device keeps its switch on while unplugged and shows
**Waiting for device**. Keymasq masks it again when it returns. For USB devices
the access restriction is already in place when the device reconnects, before
the desktop is granted access. Without a serial number the saved choice
identifies the port and model, so another unit of the same model on that port
inherits it. Bluetooth devices are identified by their remote address, so
pairing survives reconnects, but their restriction is reapplied a few seconds
after they reconnect rather than ahead of it.

Each saved mask reports one state: **off**, **starting**, **waiting for device**,
**saved incomplete**, **attention**, **activating**, **trial**, **masked**, or
**recovering**. Being disconnected is a normal waiting state, not a failure. If
an automatic start fails, the row shows the reason and Keymasq retries after two
seconds, doubling up to one minute. Connecting or disconnecting the device,
turning its switch on, or pressing **Retry** resets that delay. Absent devices
list the time they were last connected under their details.

A mask confirmed by an earlier build may show
**Waiting for device · confirm again when connected**. Connect the device and
confirm masking once more. It then behaves like any other saved mask.

Keymasq listens for kernel hotplug events, so a returning device is picked up
immediately. Plugging a wireless controller into a masked receiver, or removing
it, does not drop the mask. Keymasq reacquires the changed interfaces.

## When masking stops

If the masked input stops working at runtime, Keymasq restores physical access
first, then retries automatically after two seconds, doubling up to one minute.
Thirty seconds of healthy operation resets the delay. Other masked devices keep
running throughout. If an enabled mask stops while its device is connected, the
row shows **Not masked** until the retry succeeds.

Emergency reset stops remapping, releases input devices, and restores every
mask, including disconnected devices and masks still starting. Afterwards the
dialog offers **Enable remapping**, which stays unavailable while Keymasq is
still repairing device access. Saved but inactive masks never pause remapping on
their own.

## Administrative recovery

From SSH or a TTY, stop the daemon to release all grabs and restore access:

```sh
sudo systemctl stop keymasqd
```

If recovery reports an error, run the recovery helper while the daemon is
stopped:

```sh
sudo keymasq-helper recover-hardware
```

For the installed AppImage, use `/opt/keymasq/bin/keymasq-helper`.

Recovery only changes devices it can positively identify. If an undo journal is
damaged, recovery still removes that device's rules and restores what it can
prove, then reports an error and keeps daemon startup blocked. The same applies
when the journal is missing and the independent recovery records are malformed
or disagree about the device. Recovery continues for other masks and retains the
damaged records for repair. Do not delete recovery records to bypass the error,
because they may hold information needed to repair an interrupted hardware
operation. Restart `keymasqd` when recovery succeeds.
Recovery preserves user profiles, hardware configurations, and confirmed masking
preferences.

## Uninstalling

Removing Keymasq stops `keymasqd`, restores every masked device, and removes
Keymasq's own ACL entries from device nodes. Other ACL entries, such as those
from the desktop session or Steam, stay in place. If recovery cannot finish,
removal is refused and Keymasq stays installed, so the recovery helper remains
available. Fix the reported error or reboot, then remove Keymasq again.

## Further reading

- [Hardware masking design](hardware-masking-design.md) describes the privileged
  jobs, udev rules, permission baselines, USB takeover, and recovery journals.
- [Security model](security.md) covers the trust boundary of the root job.
- [Troubleshooting](troubleshooting.md) covers daemon capability and permission
  problems.
