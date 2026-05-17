# Analog Controls

Analog Controls are reusable configs for analog source inputs exposed by a
hardware template. Stick controls can drive mouse movement, digital actions,
or another gamepad stick output. 1D axes and triggers can drive one-direction
mouse movement, digital action ranges, or another analog axis output.

Configs live in:

```text
~/.config/keymasq/analog_controls/
```

Profiles map a hardware analog source to a saved config:

```toml
[devices."045e:028e".mapping.right_stick]
action = "analog_control"
analog_control_name = "FPS Mouse"
```

One source can fan out to multiple saved configs. Each config receives the same
normalized input and handles its own mouse movement, gamepad output, and digital
thresholds independently:

```toml
[devices."045e:028e".mapping.right_stick]
action = "analog_control"
analog_control_names = ["FPS Mouse", "WASD"]
```

## Config Shape

```toml
name = "FPS Mouse"
description = "Right stick mouse"
input_type = "stick"

[mouse_motion]
enabled = true
mode = "velocity" # velocity or area; area is stick-only
speed = 900.0
speed_x = 900.0 # stick mouse movement only; defaults to speed
speed_y = 900.0 # stick mouse movement only; defaults to speed
area_radius_x = 400.0 # stick mouse area only
area_radius_y = 400.0 # stick mouse area only
area_start_enabled = false
area_start_x = 0
area_start_y = 0
deadzone = 0.15
sensitivity = 1.0
response_curve = 1.0
direction = "right" # 1D axis only: left, right, horizontal, up, down, vertical
invert_x = false
invert_y = false
tick_ms = 8

[gamepad_output]
enabled = true
output_id = "same-device" # same-device, explicit hardware id, or virtual-gamepad-N
deadzone = 0.0
target = "same" # same, left, right
sensitivity = 1.0 # analog output only; 0.1..2.0
response_curve = 1.0 # analog output only; 0.25..4.0

[[thresholds]]
axis = "x"
trigger_min = 0.65
trigger_max = 1.0
release_min = 0.55
release_max = 1.0
actions = [
  { action = "keyboard", target = "key_e" },
]
```

Stick thresholds use normalized values from `-1.0` to `1.0`. Generic axis
thresholds use normalized values from `0.0` to `1.0` and always use axis `x`. A threshold
activates when the current axis value enters the trigger range and releases when
it leaves the release range. The trigger range must be inside the release range
so hysteresis is explicit.

`gamepad_output` routes the analog source to a gamepad axis on the selected
output. `output_id = "same-device"` routes to the same physical hardware that
provided the source input. Omitting `output_id` keeps the default virtual
gamepad output. `target = "same"` preserves the source side, so `left_stick`
writes `ABS_X`/`ABS_Y`, `right_stick` writes `ABS_RX`/`ABS_RY`,
`left_trigger` writes `ABS_Z`, and `right_trigger` writes `ABS_RZ`.
`target = "left"` or
`target = "right"` forces the output side, allowing right trigger to become
left trigger, left stick to become right stick, and so on. The deadzone is
applied before output, so values below it are sent as centered sticks or
released triggers.

For learned physical output hardware, `target = "analog"` plus
`target_analog_id` routes to a learned analog output on the selected hardware.
The output must have the same shape as the input control: generic axes route to
learned generic axes, and sticks route to learned sticks. Runtime normalizes the
source value first, applies output deadzone/sensitivity/curve, then converts the
result into the learned output min/max/rest or center range before writing the
target `EV_ABS` code. Virtual Xbox gamepads keep the fixed semantic left/right
stick and trigger targets.

For 1D axis output, the analog control owns the output start point and
direction. `output_rest` is the raw value written when the output is released,
and `output_direction` is `min`, `max`, or `both`. `min` and `max` map a
one-sided input from rest to that endpoint; `both` treats the input as signed and
maps it across the output minimum/rest/maximum range. These fields belong to
output behavior, not to learned input hardware calibration.

Mouse movement and analog output apply the same sensitivity and response curve
to normalized analog input. Stick output applies it radially:

```text
distance = sqrt(x*x + y*y)
normalized = (distance - deadzone) / (1 - deadzone)
output_distance = clamp((normalized ** response_curve) * sensitivity, 0, 1)
```

`sensitivity = 1.0` and `response_curve = 1.0` are linear. Higher sensitivity
reaches full output sooner. A response curve below `1.0` is faster near center;
above `1.0` is slower near center. The same curve is mirrored for negative stick
directions and for 1D axis analog output when `output_direction = "both"`. Mouse
movement then multiplies the shaped value by speed. Stick mouse movement can use
separate `speed_x` and `speed_y`; if omitted, both default to `speed`. 1D axis
mouse movement uses `direction = "horizontal"` or `direction = "vertical"` to
map negative and positive source values to both mouse directions. Use `left`,
`right`, `up`, or `down` when the 1D axis should move the mouse in only that
direction.

`mode = "area"` changes stick mouse movement from velocity control to position
control inside a configured 2D area. Runtime shapes each stick axis independently
and maps the result to:

```text
target_x = stick_x * area_radius_x
target_y = stick_y * area_radius_y
```

Each new stick event emits only the relative delta from the previous target
position. Returning the stick to rest therefore emits the opposite movement and
brings the pointer back to the area's origin, subject to the compositor and
screen edges accepting the relative movement. If `area_start_enabled = true`,
the daemon moves the cursor to `area_start_x`/`area_start_y` when the stick first
leaves rest, then applies area-relative movement from there.

```toml
name = "Left Trigger Action"
input_type = "axis"

[[thresholds]]
axis = "x"
trigger_min = 0.50
trigger_max = 1.0
release_min = 0.45
release_max = 1.0
actions = [
  { action = "keyboard", target = "key_e" },
]
```

Overlapping thresholds are valid. They are evaluated independently; Keymasq
does not prioritize or merge overlapping ranges.

Hardware analog inputs are editable. Use **Learn Analog** from the device tab to
record a generic axis or stick from raw `EV_ABS` events, review the detected
evdev code and min/max/rest or center values, then save those values into the
hardware file.

## Templates

The GUI includes templates for WASD, arrow keys, and mouse wheel. Templates
only generate normal threshold data, so the saved config remains editable and
runtime behavior has no special cases except continuous mouse movement.
