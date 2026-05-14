# Analog Controls

Analog Controls are reusable configs for analog source inputs exposed by a
hardware template.

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

Thresholds use normalized stick values from `-1.0` to `1.0`. A threshold
activates when the current axis value enters the trigger range and releases
when it leaves the release range. The trigger range must be inside the release
range so hysteresis is explicit.

Overlapping thresholds are valid. They are evaluated independently; Keymasq
does not prioritize or merge overlapping ranges.

## Templates

The GUI includes templates for WASD, arrow keys, and mouse wheel. Templates
only generate normal threshold data, so the saved config remains editable and
runtime behavior has no special cases except continuous mouse movement.
