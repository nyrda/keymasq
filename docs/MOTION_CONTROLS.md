# Motion controls

Motion Controls turn a controller's gyroscope or tilt into mouse movement, stick output, or
an Analog Control input. They are saved separately from profiles, so the same control can be
used in several profiles. A Motion Control is active whenever a profile maps it to a motion
sensor.

## Set up a motion sensor

Keymasq supports motion sensors exposed by Linux, including those in PlayStation, Nintendo,
and Steam controllers. Other controllers may also work when their Linux driver provides a
compatible motion event device.

The hardware setup wizard normally finds and attaches the motion sensor. If it does not appear
on the controller tab, open **Hardware settings**, choose **Add Event Device**, and select the
controller's motion interface. Removing that event device also removes its saved calibration.

## Calibrate the gyroscope

Calibration removes the slow cursor or stick movement caused by gyro drift.

1. Open the controller tab and choose **Hardware settings**.
2. Under Motion Normalization, choose **Calibrate gyro…**.
3. Put the controller on a stable surface and leave it untouched. Keymasq lets the sensor
   settle, then measures it for three seconds.

Keymasq rejects a run if the controller moves, the measurement is too short, or sensor data is
lost. A few isolated noisy readings are fine.

After measurement, Keymasq restores normal profile handling before saving the result. The
Close button is disabled during this brief finishing step. If cleanup fails, use **Retry
cleanup**. You may close the dialog at that point; Keymasq will keep trying in the background
and save a completed calibration once cleanup succeeds. Closing while measurement is still in
progress cancels that run.

Guided calibration corrects gyro drift and measures sensor noise. It does not change axis
direction or profile sensitivity. Use advanced manual normalization only if automatic
calibration does not work with your controller driver or you need accelerometer correction.
Keymasq cannot calibrate accelerometer bias from a stationary pose because the sensor also
measures gravity.

## Choose an output

Open **Motion Controls** from the application menu, or click a Motion Sensor card in a
controller profile. The menu entry appears once a configured controller has an attached motion
sensor.

The presets cover the common uses:

- **Gyro Mouse** moves the pointer while you rotate the controller. Movement stops when the
  controller stops rotating.
- **Gyro Stick** turns controller rotation into right-stick output. It can target a virtual
  gamepad or another configured controller.
- **Tilt Mouse** moves the pointer continuously while the controller remains tilted.
- **Tilt Stick** holds a stick away from center while the controller remains tilted.

The Right Stick presets output to the same controller. Keymasq grabs its gamepad interface
to provide that output, even when the motion mapping is the profile's only mapping. Its
buttons and sticks continue through the passthrough gamepad. Mouse-only motion mappings do
not require this additional grab.

Same-device output is a software copy of the controller, retaining its original name and
controller identity. It does not modify the physical controller's HID reports. Steam or a
game using a direct HID driver can bypass this output by reading the physical controller
through `hidraw`; Keymasq's source hiding covers evdev and joystick nodes, not `hidraw`.
In that case, select a virtual gamepad such as `keymasq-gamepad` and route both the Analog
Stick Control and Gyro Stick to it. The game must use that virtual controller. Matching names
can be distinguished by the software device's `/devices/virtual/input/` sysfs path.

Gyro Stick adds a rotation-based adjustment to the latest passthrough stick or Analog Stick
Control output from the same source controller, when both target the same output stick. Each
axis is clamped to the destination's range. Each gyro axis returns to neutral as soon as its
unsmoothed rate enters the configured gyro deadzone. Smoothing does not prolong minimum-output
compensation after input stops. Disabling the motion mapping restores the stored stick position. A held
stick does not need to move again. At full stick deflection, gyro cannot increase the output
further in that direction.

New Gyro Stick controls default to a **90°/s full stick rate** and a **0°/s deadzone**.
The full stick rate measures rotation speed, not the angle at which you hold the controller.
It is the rotation speed needed to reach full stick output. Lower values make aiming faster;
higher values make it slower. With a linear curve and zero deadzone, rotating at 45°/s gives
50% output at a 90°/s full stick rate, or 25% at 180°/s, before minimum-output compensation.
Existing saved values are kept; edit your current control to try these defaults.

**Minimum stick output (%)** compensates for deadzones in Steam or a game. It defaults to 25%.
For example, a 25% minimum turns a 1% signal into about 25.75% output. Tune it to the game's
deadzone: lower it if aiming jumps, or increase it if slow rotation still does not register.
Higher values can amplify gyro noise.
The setting applies per axis to the combined stick-plus-gyro position while that axis has a
nonzero gyro contribution. Exact cancellation stays neutral, and when gyro stops or is
disabled the ordinary stick position is restored without compensation. The output is still
limited to the destination axis range. This setting is stored as `gamepad.minimum_output`,
a fraction from 0 to 1.

Ordinary stick inputs still overwrite one another rather than being added. A stick from
another source controller is not paired with the gyro. Competing mappings to one destination
can still overwrite each other. Tilt Stick and Motion to Analog retain their existing output
behavior; this adjustment applies only to Gyro Stick.

For more specialized mappings, **Motion to Analog** sends gyro movement or tilt into a saved
[Analog Control](ANALOG_CONTROLS.md). The Analog Control supplies its own deadzones, response
curve, digital actions, mouse behavior, and gamepad target.

## Tune gyroscope controls

Each gyro axis can drive the horizontal channel, the vertical channel, or neither. The default
routing is:

- yaw to horizontal;
- pitch to vertical;
- roll to horizontal.

Yaw and roll add together by default. This makes horizontal movement respond both when you
rotate a level controller and when you tilt it like a steering wheel.

With the default directions:

- turning left or right moves left or right;
- tilting the top edge toward you moves up;
- tilting the controller like a steering wheel moves left or right.

Use the horizontal and vertical inversion switches if either direction feels wrong. Sensitivity
sets the overall output strength. Deadzone ignores small movement near rest. Smoothing reduces
jitter but adds some response delay. Response curve adjusts the balance between fine and fast
movement.

Gyro controls respond to rotation, not the angle at which you hold the controller. Use a tilt
control when you want the output to continue while the controller stays at an angle.

## Tune tilt controls

Tilt controls use the controller's pose when the profile becomes active as their neutral pose.
Choose **Absolute gravity** if you want a level controller to be neutral instead. Keymasq takes
a new activation pose after a profile change, device reconnect, or device reset.
The pose comes from the device's current axis state, including axes that have not changed.

An accelerometer can measure pitch and roll from gravity, but it cannot determine yaw. Fast
controller movement adds acceleration of its own, so the reported tilt may wobble briefly while
the controller is moving.

For **Motion to Analog**, the full-output rate or angle determines how much movement produces
the Analog Control's maximum input. Axis controls use one selected gyro or tilt axis. Stick
controls use two.

Motion to Analog offers **Fixed** and **Adaptive** smoothing. Fixed is the default and keeps
using the existing Smoothing setting. Adaptive uses a [One Euro filter](https://gery.casiez.net/1euro/)
on each normalized axis before the Analog Control applies its deadzones, curves, and actions.
It works with both gyro and tilt sources.

Start with a **Rest cutoff** of **1 Hz** and **Motion response** of **10**. Lower the rest
cutoff to reduce wobble further, at the cost of more delay during slow movement. Raise motion
response to follow movement faster, at the cost of letting more noise through. Adaptive smoothing
replaces fixed smoothing for this control. It has no movement threshold, so tiny sustained
movements still pass through unless the receiving Analog Control or game applies a deadzone.

Filtering cannot distinguish intentional tilt from the accelerometer response to linear movement.
It reduces jitter but cannot remove that source of tilt error. Filter history resets when the
mapping resets, after dropped input, after a gap of more than half a second, or if input
timestamps move backwards.

## Use motion controls in profiles

A profile can attach one or more Motion Controls to the same sensor. Each control keeps its own
tuning and neutral pose, which makes combinations such as Gyro Mouse plus tilt-triggered digital
actions possible.

When a higher-priority profile maps the same motion sensor, it replaces the entire Motion
Control list from the lower-priority profile. Include every control you want in the
higher-priority mapping.

Games and other apps can still read the controller's motion sensor while a Motion Control is
active. Keymasq adds the configured mouse, gamepad, or digital-action output. It does not hide
sensor input from other apps.
