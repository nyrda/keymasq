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
- **Area Mouse** maps tilt to a bounded area around the pointer's starting position. Returning
  to the neutral pose moves the pointer back toward that position.

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

An accelerometer can measure pitch and roll from gravity, but it cannot determine yaw. Fast
controller movement adds acceleration of its own, so the reported tilt may wobble briefly while
the controller is moving.

For **Area Mouse**, the full-output angle sets the horizontal and vertical range. Enable **Drag
center** if you want the neutral point to follow when you tilt beyond that range.

For **Motion to Analog**, the full-output rate or angle determines how much movement produces
the Analog Control's maximum input. Axis controls use one selected gyro or tilt axis. Stick
controls use two.

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
