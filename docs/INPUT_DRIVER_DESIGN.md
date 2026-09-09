# Native input drivers

Keymasq can supplement evdev with bundled hidraw drivers. The first driver adds
motion from the 8BitDo Ultimate 2 Wireless in DInput dongle mode. Buttons, sticks,
and the four extra buttons continue through evdev. Steam masking is maintained
separately and is outside this driver layer.

## Discovery and association

The daemon discovers HID endpoints through sysfs. A registered driver matches
VID/PID, transport, and its interface's report descriptor. The Ultimate 2 driver
matches USB `2dc8:6012`, a Gamepad collection, report ID 1, and the vendor-defined
report field. Its decoder accepts only the observed 34-byte report format.
Older firmware and the receiver's idle `2dc8:6013` interface do not supply motion
through this driver. Matching discovery metadata identifies a candidate; usable
samples are confirmed when the source is opened.

Saved hardware configurations contain native sources separately from evdev:

```toml
[[hardware.input_sources]]
id = "imu"
driver = "8bitdo-ultimate2"
companion_of = "gamepad"
```

`gamepad` names an existing `hardware.evdev.devices` entry. Its existing path,
VID/PID, and capability selectors choose the live controller. The native source
then binds to that instance. A motion-only profile uses the evdev selector as an
anchor without grabbing buttons. When both sources are active, they reuse the
same resolved anchor. Other hardware configurations cannot claim its companion
independently. Neither a serial number nor a permanent USB port assignment is
required to select the driver. Native ownership and live-device preferences apply
to the evdev companions before selection, even for button-only requests or when
a motion-only profile adds buttons. Explicit event paths and their `by-id` or
`by-path` aliases obey the same ownership checks. An explicitly selected
controller that is already claimed stays unavailable; selection does not fall
back to another controller. Calibration retains the evdev selector's
physical-device preference.

Association rules are declared by drivers. Shared helpers support the same HID
ancestor or separate HID interfaces under the same USB device. The Ultimate 2
uses the same HID ancestor. A shared hub or equal VID/PID alone is insufficient
to pair two endpoints. Multiple matching endpoints or conflicting driver claims
remain unresolved. The core also represents sources without an evdev companion;
the first shipped GUI integration is supplemental controller motion.

Live source addresses under `/dev/keymasq-sources/` identify a driver, HID
connection, and hidraw node. They are internal addresses, not kernel device
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
| `manager.py` | Shared connection ownership, subscribers, buffering, continuity |
| `drivers/eightbitdo_ultimate2.py` | Device matching, report validation, channel decoding and conversion metadata |
| `evdev_adapter.py` | Internal adaptation of motion frames to existing runtime and inspector consumers |

Drivers describe named channels and decode reports without importing GUI,
profile, or evdev runtime code. The source manager also accepts button and axis
channel descriptions. The shipped consumer adapter handles motion; native button
or ordinary-axis drivers will need the corresponding acquisition and event
adapter behavior before they can be exposed to profiles. A synthetic button
source tests that shared reader ownership does not depend on motion or on an
evdev companion.

The adapter assigns internal ABS channel numbers to motion samples. It creates
no uinput device and emits no controller buttons. The existing runtime applies
stored raw bias, scale, sign, noise threshold, smoothing, and output routing once.
Inspector and calibration receive the same raw channels and conversion metadata.
Gyro units are radians/second and acceleration units are metres/second squared.

The Ultimate 2 follows SDL's raw layout and gyro scale, adapted to Keymasq's axis
conventions. Its acceleration scale agrees with the captured Nintendo-mode
comparison. Live user testing found motion behavior equivalent to Nintendo
mode. Calibration remains specific to the source's raw units; it is not
copied between DInput and Nintendo modes.

## Lifetime and failure behavior

One asyncio reader owns each active endpoint. Runtime and calibration subscribe
to it; inspection uses the runtime's event stream. Acquisition starts when a
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

Sample timestamps currently use monotonic report arrival time and are explicitly
estimated. No hardware clock or sequence counter has been established for this
report format. Equal sensor values are preserved; equality does not identify a
duplicate sample. Kernel hidraw queue losses cannot all be detected. Keymasq's
own queue overflows and observed timing gaps are detectable.

A source has one active binding. A second native definition cannot acquire the
same endpoint independently within the same hardware configuration. This change
does not silently substitute kernel motion for calibrated native motion. Such a
substitution requires verified channel equivalence and compatible calibration.
When adding future kernel support, choose one provider for each physical sensor.

## Adding another driver

Implement the `InputDriver` contract, provide its supported endpoint match,
association rule, named channels, and pure report decoder, then register it in
`registry.py`. All packages grant the daemon generic read access to hidraw, so
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
