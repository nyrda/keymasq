# Masking VM integration tests

These NixOS VM suites run the installed daemon and session broker with synthetic
UHID controllers. The behavior suite also boots an exporter VM with USB HID
gadgets, which the Keymasq VM imports through USB/IP. A test client drives session
IPC without launching the GTK GUI. No physical USB device is required.

## Running

Use a Linux host with Nix and KVM acceleration. From the repository root:

```bash
./scripts/integration.sh masking-behavior
./scripts/integration.sh masking-recovery
```

Run both together, or repeat them when investigating a failure:

```bash
./scripts/integration.sh masking-behavior masking-recovery
./scripts/integration.sh masking-behavior masking-recovery --repeat 3
```

The runner includes uncommitted test files and forces a fresh test execution.

## Coverage

- `masking-behavior` checks trial confirmation, expiry and undo, client
  disconnects, independent masks, unmask-all, reconnects, profile changes,
  rejected requests and confirmation tokens, and saved choices across reboot.
- `masking-recovery` checks SIGKILL, watchdog expiry, interrupted activation,
  service stop, clean reboot, abrupt power loss, and reapplication of a saved
  mask after restart. It also runs `keymasq-record prepare-removal` while
  another process holds the hardware operations lock. The command must fail and
  leave the mask in place, then succeed once the lock is released. Automatic daemon restart is disabled so startup recovery
  cannot conceal failed cleanup after a stop or crash.

Assertions check actual device permissions and input as an ordinary user,
revocation of existing handles, forwarding through virtual devices, empty daemon
capability sets, and removal of masking rules and recovery records. An unrelated
device must remain accessible throughout.

A composite USB fixture exposes a gamepad and keyboard under one mask. A profile
adopts only its gamepad interface. Removing and reconnecting it must preserve the
same shared keyboard, mouse and gamepad outputs, and another controller must keep
sending mapped button presses through its already-open gamepad output.

The USB cases check raw USB handle revocation on disconnect, repeated reconnect,
saved mask reapplication, replacement devices on the same port, moving a device
to another port, and disabling a saved mask while disconnected. The single-input
fixtures also verify physical-node access and forwarding through one virtual
device.

The USB/IP controller used here does not re-enumerate after a software port power
cycle, so takeover with an existing raw USB handle needs a separate hardware test.
These suites also exclude electrical hub power switching, device firmware quirks,
suspend, GTK interaction, and package installation and removal hooks. Native
package lifecycle tests have a separate runner,
[`packaging/masking/run-guest.sh`](../packaging/masking/run-guest.sh),
for disposable or snapshotted distribution VMs.

The test definitions are [`nix/masking-behavior-test.nix`](../nix/masking-behavior-test.nix)
and [`nix/masking-recovery-test.nix`](../nix/masking-recovery-test.nix).
