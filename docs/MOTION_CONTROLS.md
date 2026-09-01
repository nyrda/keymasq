# Motion controls

Keymasq treats a controller motion sensor as a separate evdev interface attached to the
same hardware configuration as the controller. A profile maps that sensor to a reusable
Motion Control. Enabling, disabling, toggling, and layering the profile therefore activates
motion in exactly the same way as every other Keymasq mapping.

Version 1 supports kernel evdev motion interfaces from `hid-playstation`, `hid-nintendo`,
and `hid-steam`. Detection is capability based: the event device must expose
`INPUT_PROP_ACCELEROMETER`. This keeps motion axes separate from ordinary controller sticks.
Other evdev drivers using the same kernel ABI work through the generic path. Hidraw and SDL
input backends are not used.

## Hardware normalization

The hardware setup wizard attaches a sibling motion interface when it finds one belonging
to the selected controller. It translates the driver-specific axis order and signs into
canonical controller-relative axes. Gyroscopes use `pitch`, `yaw`, and `roll` roles;
accelerometers use `x`, `y`, and `z`. Kernel axis resolution supplies the unit conversion:

- gyroscope values become radians per second;
- accelerometer values become metres per second squared;
- frame time is monotonic nanoseconds in the daemon.

Bias, scale, inversion, and noise-floor values live in the hardware configuration. To remove
gyro drift, open the controller tab, choose **Hardware settings**, then choose
**Calibrate gyro…** under Motion Normalization. Put the controller on a stable surface and
leave it untouched while Keymasq records three seconds of stationary input through the
daemon capture path. A run with too few samples or visible controller movement is rejected.

The guided calibration changes gyro bias and noise floor only. It keeps the scale derived
from the kernel axis resolution. Advanced manual normalization remains available for unusual
drivers and accelerometer correction. A stationary pose cannot automatically determine
accelerometer bias because its readings include gravity.

Profile-specific sensitivity, deadzone, smoothing, response curve, axis routing, neutral
reference, and output target live in the Motion Control instead. Each canonical gyro axis
can be unused or routed to the horizontal or vertical output channel. Several gyro axes may
feed the same channel. Accelerometer controls derive controller-relative pitch and roll from
the normalized `x`, `y`, and `z` gravity vector. An accelerometer cannot determine yaw.

The PlayStation, Nintendo, and Steam templates each define their own raw-axis translation.
This is necessary because RX, RY, and RZ do not represent the same physical controller
rotation in all three kernel drivers. Older hardware normalization versions are translated
when loaded, while preserving their calibration offsets, scales, noise floors, and user
inversion.

Motion is processed once per evdev `SYN_REPORT` frame. `SYN_DROPPED` clears accumulated
sensor and output state. Motion interfaces use the normal controller grab, release, hotplug,
and profile-switch lifecycle.

## Outputs

Open **Motion Controls** from the application menu, or click a Motion Sensor card in a
controller profile. The menu entry appears after Keymasq has a configured controller with
an attached motion sensor. The first-use presets create one of five separate control types:

- **Gyro Mouse**, which integrates angular velocity over elapsed frame time and emits
  fractional-accumulated relative mouse motion;
- **Gyro Stick**, which maps angular rate to the originating controller's right stick and
  uses the normal tracked gamepad output path. Its output selector can route that one stick
  output to a virtual gamepad or another configured physical controller instead;
- **Tilt Mouse**, which maps held controller tilt to continuous cursor velocity;
- **Tilt Stick**, which maps held controller tilt to a persistent stick deflection and uses
  the same single-output routing as Gyro Stick;
- **Area Mouse**, which maps tilt to a bounded cursor offset. Motion back toward the neutral
  pose emits the inverse cursor movement.

Multiple motion sensors and multiple named controls are supported, but each sensor mapping
selects one Motion Control and each Motion Control selects one output behavior. There is no
process-wide or global gyro source.

The default axis routing is:

- yaw to horizontal;
- pitch to vertical;
- roll to horizontal.

Yaw and roll are added together before mouse or stick tuning. This lets horizontal movement
respond both when the controller is turned flat and when it is tilted like a steering wheel.
The default mouse directions are:

- turn the controller left or right to move the cursor left or right;
- tilt the top edge toward yourself to move up, or away from yourself to move down;
- tilt the controller like a steering wheel to move left or right.

The horizontal and vertical inversion switches reverse these normalized directions. A gyro
measures angular velocity, so Gyro Mouse stops when the controller stops rotating. Tilt Mouse
continues moving while the controller remains tilted. Tilt Stick continues holding its stick
output for the same reason.

Tilt controls use the profile activation pose as their neutral pose by default. The daemon
captures it from the first complete accelerometer frame after the profile and device become
active. Choosing **Absolute gravity** instead uses a level controller as the neutral pose.
The captured pose is runtime state. Profile changes, device resets, and `SYN_DROPPED` clear
it. Keymasq never writes it into the hardware configuration.

Area Mouse maps its configured full-output angle to a horizontal and vertical pixel radius.
It emits relative deltas from the previous point in that area, matching the existing analog
Mouse Area behavior. With **Drag center** enabled, moving past the configured angle shifts
the neutral point. Movement back toward the center then registers immediately.

Accelerometer tilt is gravity based and low-pass filtered. Linear acceleration can briefly
disturb it while the controller moves quickly. Motion-to-digital gestures, trained movements,
tilt or shake actions, and full gyroscope and accelerometer sensor fusion remain deferred.
