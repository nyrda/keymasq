# Analog Controls

Analog Controls are reusable configs for analog source inputs exposed by a
hardware template. Stick controls can drive mouse movement, digital actions,
or another gamepad stick output. Trigger controls can drive digital action
ranges or another gamepad trigger output.

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

## Config Shape

```toml
name = "FPS Mouse"
description = "Right stick mouse"
input_type = "stick"

[mouse_motion]
enabled = true
speed = 900.0
deadzone = 0.15
curve = "soft" # soft, linear, fast
invert_x = false
invert_y = false
tick_ms = 8

[gamepad_output]
enabled = true
output_id = "virtual-gamepad-2" # optional; omitted means default gamepad output
deadzone = 0.15

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

Stick thresholds use normalized values from `-1.0` to `1.0`. Trigger thresholds
use normalized values from `0.0` to `1.0` and always use axis `x`. A threshold
activates when the current axis value enters the trigger range and releases when
it leaves the release range. The trigger range must be inside the release range
so hysteresis is explicit.

`gamepad_output` routes the analog source to the matching gamepad axis on the
selected output. `left_stick` writes `ABS_X`/`ABS_Y`, `right_stick` writes
`ABS_RX`/`ABS_RY`, `left_trigger` writes `ABS_Z`, and `right_trigger` writes
`ABS_RZ`. The deadzone is applied before output, so values below it are sent as
centered sticks or released triggers.

```toml
name = "Left Trigger Action"
input_type = "trigger"

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

## Templates

The GUI includes templates for WASD, arrow keys, and mouse wheel. Templates
only generate normal threshold data, so the saved config remains editable and
runtime behavior has no special cases except continuous mouse movement.
