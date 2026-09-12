# Hardware masking

Open **Settings > Hardware > Manage** to list physical USB and Bluetooth HID
attachments. This list comes from sysfs, so the Steam Deck controller appears
even when Steam owns its raw interface and the kernel exposes no gamepad
event node. Listing hardware does not open its raw interfaces.

This first implementation supports the built-in Steam Deck controller,
`28de:1205`. Other attachments appear with masking disabled. It accepts one
reservation at a time. Masking is opt-in and uses the existing Keymasq unlock
policy, including the SteamOS policy. Saved remapping configurations alone
never enable it.

Choose **Try masking**. The independent recovery service starts a 30-second
deadline before changing device access. Once the replacement controller is
ready, try its controls and choose **Keep**. Missing the deadline restores
hardware access, even if the dialog has closed. The touchscreen remains outside the
controller reservation and can operate Keep and Restore.

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

An unconfigured reserved interface appears as **Masked · Available to add**.
Adding gamepad or motion sources attaches their configuration to Keymasq's
existing readers. Button and analog learning use that same input stream, with
output suppressed during capture; they do not release the mask or recreate the
selected output. Ending capture returns to normal profile processing.
Interfaces already present in a hardware configuration retain the usual
duplicate-device protection.

**Mask automatically when Keymasq starts** is enabled by default. **Keep** saves
the confirmed hardware identity and this preference for the current user.
The background user session reapplies the mask after a reboot or service
restart, including in Steam Deck Gaming Mode without opening the GUI. It
resolves the hardware's current connection and waits for a working replacement
controller before completing the automatic takeover. The 30-second acquisition
deadline and heartbeat recovery still apply. Automatic startup does not require
another Keep or unlock. The original confirmation authorizes it.

Turn the startup option off before Keep for a temporary reservation. You can
also change it later; disabling it leaves the current mask active but prevents
it from returning on the next start. Closing only the GUI leaves a confirmed
mask active. Normal daemon/session shutdown and suspend restore physical access;
startup and wake reapply the saved mask when the same user session is available.

**Restore** releases the reservation and pauses both remapping and automatic
masking. Trial expiry, failed takeover, daemon failure and hardware disconnection
also restore access and pause automatic behavior. The pause survives restarts
and reboots, even with the startup option enabled. Choose **Resume** to resume
remapping and the saved automatic mask, or explicitly start another masking
trial. An emergency recovery therefore cannot be undone merely by restarting
Keymasq. Saved hardware mapping files and old display history alone never
authorize automatic masking.

## How it works

`keymasq-maskd` is a small root service separate from the input event loop. It
resolves the selected attachment again, records its current device permissions
and driver mode, and installs temporary udev rules for that USB connection.
The rules reserve its hidraw, usbfs, evdev and legacy joystick nodes for root
and the dedicated `keymasq` account. They remove desktop ACLs and match the
USB port, model and connection number. They exclude virtual outputs.

Permissions alone cannot revoke Steam's existing raw handles. The service
unbinds and binds the controller's main `hid-steam` interface. This produces
real device removal and addition, closes the old raw handles and lets the
kernel expose physical input. The service disables hid-steam's lizard mode
for the reservation, then restores its previous value during recovery. Because
that setting is module-wide, masking refuses other attached hid-steam
controllers and recovers if one appears. Existing direct USB or auxiliary HID
handles also prevent activation because the main-interface rebind cannot
revoke those handles.

`keymasqd` acquires the resulting input interfaces through its normal runtime.
Keep becomes available after every reserved interface has a live reader and
each emitting interface has a passthrough output or its selected virtual
output. Motion observation does not require an output. A missing interface,
stopped reader or unavailable selected output restores access. Readiness checks
wait for routing transactions to finish. The existing session reevaluation
applies saved hardware routes after takeover, including during automatic startup. A heartbeat to `keymasq-maskd` renews a 12-second lease even
after Keep. An unresponsive input daemon can be killed by the recovery service
to release its grabs and virtual outputs. The recovery service has its own
systemd watchdog and stop/start reconciliation using a root-owned journal.

The root service stores the confirmed startup preference in
`/var/lib/keymasq-masking/policy.json`, bound to the authenticated desktop UID.
It is separate from the temporary permissions and undo journal under `/run`.
The journal restores an interrupted operation; it does not authorize startup.
The `suspended` file in the state directory preserves the recovery pause.

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
