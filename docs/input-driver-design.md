# Native input drivers

Keymasq can supplement evdev with bundled native drivers. A driver uses one of
two backends. `hidraw` drivers read a HID interface's raw reports. `hid-bpf`
drivers attach a bundled program to a HID device inside the kernel and read what
it records. The 8BitDo Ultimate 2 Wireless driver adds motion in DInput dongle
mode through hidraw; its buttons, sticks, and four extra buttons continue through
evdev. The Steam Deck driver adds the capacitive stick touch sensors and the
trackpad touch state through HID-BPF. Steam masking is maintained separately and is outside this driver layer.

## Discovery and association

The daemon discovers HID endpoints through sysfs. hidraw drivers match hidraw
nodes; HID-BPF drivers match HID devices directly, including interfaces without
a hidraw node. A registered driver matches VID/PID, transport, HID group, and
its interface's report descriptor. The Ultimate 2 driver
matches `2dc8:6012` over USB and Bluetooth, a Gamepad collection, report ID 1,
and the vendor-defined report field. Its decoder accepts only the observed
34-byte report format. Older firmware and the receiver's idle `2dc8:6013`
interface do not supply motion through this driver. Matching discovery metadata
identifies a candidate. Opening the source confirms usable samples.

Saved hardware configurations contain native sources separately from evdev:

```toml
[[hardware.input_sources]]
id = "imu"
driver = "8bitdo-ultimate2"
companion_of = "gamepad"
```

`backend` defaults to `hidraw`. HID-BPF sources store `backend = "hid-bpf"`.

`gamepad` names an existing `hardware.evdev.devices` entry. Its existing path,
VID/PID, and capability selectors choose the live controller. The native source
then binds to that instance. A motion-only profile uses the evdev selector as an
anchor without grabbing buttons. When both sources are active, they reuse the
same resolved anchor. Other hardware configurations cannot claim its companion
independently. Selecting the driver requires neither a serial number nor a
permanent USB port assignment. Native ownership and live-device preferences
apply to the evdev companions before selection, even for button-only requests or
when a motion-only profile adds buttons. Explicit event paths and their `by-id`
or `by-path` aliases obey the same ownership checks. An explicitly selected
controller that is already claimed stays unavailable, and selection does not
fall back to another controller. Calibration retains the evdev selector's
physical-device preference.

Drivers declare association rules. Shared helpers support the same HID ancestor
or separate HID interfaces under the same USB device. The Ultimate 2 uses the
same HID ancestor. A shared hub or equal VID/PID alone is insufficient to pair
two endpoints. Multiple matching endpoints or conflicting driver claims remain
unresolved. The core also represents sources without an evdev companion. The
first shipped GUI integration is supplemental controller motion.

Live source addresses under `/dev/keymasq-sources/` identify a driver, HID
connection, and hidraw node, or `hid-bpf` for in-kernel drivers. They are internal addresses, not kernel device
nodes. Hardware files save source IDs and selectors instead. Reconnection builds
a new binding from sysfs. Opening validates the HID connection before and after
opening the raw node, so a recycled hidraw number cannot select another device.

## Driver and consumer boundaries

The implementation lives in `keymasq/keymasqd/input_sources/`:

| Module | Responsibility |
| --- | --- |
| `types.py` | Endpoint metadata, bindings, named channels, complete raw frames |
| `registry.py` | Explicit registrations of bundled drivers |
| `discovery.py` | Sysfs enumeration and common association rules |
| `transports/hidraw.py` | Read-only, nonblocking reads with asyncio readiness |
| `transports/hid_bpf.py` | Attachment requests, ring buffer wakeups, state snapshots, detach detection |
| `bpf.py` | Kernel BTF lookup, instruction assembly, and raw `bpf(2)` loading for the root job |
| `hid_bpf_attach.py` | Root job validation, program attachment, and descriptor handoff |
| `manager.py` | Shared connection ownership, subscribers, buffering, continuity |
| `drivers/eightbitdo_ultimate2.py` | Device matching, report validation, channel decoding and conversion metadata |
| `drivers/steam_deck_touch.py` | Deck interface matching, touch program, state decoding |
| `evdev_adapter.py` | Internal adaptation of motion and button frames to existing runtime and inspector consumers |

`keymasqd/fd_handoff.py` receives descriptors from root jobs for any backend.

Drivers describe named channels and decode reports without importing GUI,
profile, or evdev runtime code. The source manager also accepts axis channel
descriptions; ordinary-axis drivers still need acquisition and event adapter
behavior before profiles can use them. A synthetic button source tests that
shared reader ownership does not depend on motion or on an evdev companion.

Button channels name a private code from `keymasq/common/native_sources.py`,
such as `btn_touch_ls`. The codes sit above `KEY_MAX`, so they never collide
with the companion controller's buttons and never reach a uinput device. The
adapter emits `EV_KEY` transitions only when a value changes, and a button
source reports its pressed buttons as active keys. A source with button channels
grabs like an ordinary interface without taking anything from other programs,
creates no passthrough output, and dispatches mappings, combos, superkeys, and
profile triggers through the normal button pipeline. Hardware setup saves each
button with the native source as its `source`. The key selector does not offer
native buttons as outputs, and Learn Buttons ignores native sources.

The adapter assigns internal ABS channel numbers to motion samples. It creates
no uinput device and emits no controller buttons. The existing runtime applies
stored raw bias, scale, sign, noise threshold, smoothing, and output routing once.
Inspector and calibration receive the same raw channels and conversion metadata.
Gyro units are radians/second and acceleration units are metres/second squared.

The Ultimate 2 follows SDL's raw layout and gyro scale, adapted to Keymasq's
axis conventions. Its acceleration scale agrees with the captured Nintendo-mode
comparison. Live user testing found motion behavior equivalent to Nintendo mode.
Calibration remains specific to the source's raw units and is not copied between
DInput and Nintendo modes.

## Lifetime and failure behavior

One asyncio reader owns each active endpoint. Runtime and calibration subscribe
to it, and inspection uses the runtime's event stream. Acquisition starts when a
profile, inspector, or calibration requires the source. The last subscriber
closes it. The Ultimate 2 driver sends no device commands.

Subscribers have bounded queues. Overflow discards stale backlog and marks the
next complete frame as discontinuous. Disconnects discard queued samples and
wake subscribers with an error. A source also fails if no valid sample arrives
for one second, even if unrelated or malformed reports keep arriving. Valid
samples reset this deadline. Long arrival gaps also mark discontinuities so
the motion runtime resets its filters and outputs. Failed native motion does
not release independent evdev button interfaces. Calibration reports missing
sources, malformed streams, and reader failures through its existing errors.

Native runtime addresses remain distinct from evdev paths, including when udev
provides a `by-id` symlink to the underlying hidraw node. Passthrough virtual
device creation and destruction run in workers so controller acquisition,
rollback, and release do not block mouse event processing. Cancelled creation
retains ownership of the worker result until it has been closed, including
when cancellation repeats during cleanup.

Sample timestamps currently use monotonic report arrival time and are marked as
estimated. No hardware clock or sequence counter has been established for this
report format. Keymasq preserves equal sensor values, because equality does not
identify a duplicate sample. Keymasq cannot detect every kernel hidraw queue
loss. It can detect its own queue overflows and observed timing gaps.

A source has one active binding. A second native definition cannot acquire the
same endpoint independently within the same hardware configuration. This change
does not silently substitute kernel motion for calibrated native motion. Such a
substitution requires verified channel equivalence and compatible calibration.
When adding future kernel support, choose one provider for each physical sensor.

## HID-BPF drivers

hid-steam reads the Steam Deck state report but does not report its capacitive
stick touch bits or its trackpad touch bits. Its only hidraw node belongs to a virtual client device, and
opening that node removes hid-steam's gamepad and motion evdev devices. A
HID-BPF program sees each report before hid-steam, leaves it unchanged, and
records the touch bits. The Deck report is type `0x09`; byte 13 bit 6 is the
left stick and bit 7 the right stick, and byte 10 bit 3 is the left trackpad and
bit 4 the right trackpad, as documented in hid-steam and SDL. hid-steam uses the
trackpad bits only to zero the pad coordinates it reports.

Opening a HID-BPF source asks the root hardware job to attach it. The job
resolves the named HID device in sysfs, checks that the driver matches it, and
builds the program from the driver. It needs no compiler or libbpf: `bpf.py`
reads type IDs from `/sys/kernel/btf/vmlinux`, assembles the instructions, and
loads them with raw `bpf(2)` calls. The job attaches the program as a
`hid_bpf_ops` struct_ops link and passes the link, a ring buffer, and a state map
to the daemon over `/run/keymasq/handoff`, then exits. Closing the link detaches
the program. After the last user releases the source, the daemon keeps the
link open for 30 seconds, so profile switches do not start a root job each
time, and then closes it. Stopping the daemon closes it at once. See [Security](security.md#native-input-programs) for the privilege
boundary.

The program counts every report it recognizes and keeps the latest state and a
change sequence number in the state map. Each state change also becomes a ring
buffer record carrying its sequence number and the new state. The daemon maps
both without BPF privileges and delivers records in sequence order, so a touch
and release that arrive before the reader runs still produce both transitions.
Sequence numbers drop duplicates. When the state map is ahead of the delivered
records, because a record is not yet submitted or a full ring lost it, the
reader delivers the current state, so the latest state always arrives. The
reader also wakes every 100 ms. When the report count stops advancing for
half a second, the source fails like a disconnected device: held buttons are
released, including profile triggers held by a touch. The Deck reports at 250 Hz,
so its count always advances while the program runs. Time the daemon could not
observe does not count toward that half second or the one-second limit for a
valid sample: a system suspend, which also covers user space frozen around it,
or a reader wake-up that came late because the event loop stalled restarts the
limit instead.

When a reader of a still-configured device fails and its node stays in place,
the daemon announces the device to the session again, which grabs it anew.
A device that keeps failing is announced again after 1 s, then with doubling
delays up to one minute.

A USB reconnect creates a new HID device, and the old program stops. Hotplug
discovery then builds a new binding and the next grab attaches again. A driver
rebind keeps the same HID device and program. Hardware masking stops the
source's reader before its takeover and grabs it again afterwards, so the
program is attached to the reserved device. A running HID-BPF source counts as
ready for masking without an output.

HID-BPF needs Linux 6.11 or later with `CONFIG_HID_BPF`. The HID core can be
built in or a module. SteamOS 3.7 and later meet this. Two common kernels do
not: Debian 13's stable 6.12 kernel is built without `CONFIG_HID_BPF` (the
trixie-backports kernels have it), and Ubuntu 24.04's GA kernel is 6.8 (the HWE
kernel works). On kernels without it the source reports an error and the
controller's evdev inputs keep working.

## Adding another driver

Implement the `InputDriver` contract, provide its supported endpoint match,
association rule, transport, named channels, and pure report decoder, then
register it in `registry.py`. A HID-BPF driver also provides `program()`. Its
program follows the layout in `bpf.py`: it increments the report count, and on
each state change stores the new state, increments the change sequence, and
submits a record with both. Its decoder receives one driver state.
All packages grant the daemon generic read access to hidraw, so
read-only drivers need no packaging changes. Add report fixtures and association
tests. A driver using the existing supplemental motion arrangement needs no
model-specific changes in
profiles, calibration, or GUI setup. New input kinds or protocols requiring
writes need their own consumer or transport support. Arbitrary Python imports
from hardware files and an external plugin ABI are not supported.

Tests cover captured reports, unsupported modes, identical devices and reversed
selection order, missing companions, shared subscribers, overflow, disconnect,
cancelled calibration, persistence, and adding motion to existing hardware.
Python changes must pass `./scripts/check.sh`.
