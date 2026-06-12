# Analog Controls — Config Format

This is the TOML reference for analog control configs. For an overview of
the feature, modes, and GUI workflow, see [Game Controller Support](GAMEPAD.md).

Configs live in `~/.config/keymasq/analog_controls/`. Profiles map a
hardware analog source to a saved config:

```toml
[devices."045e:028e".mapping.right_stick]
action = "analog_control"
analog_control_name = "FPS Mouse"
```

One source can target multiple configs. Each config receives the same
normalized input and handles its output independently:

```toml
[devices."045e:028e".mapping.right_stick]
action = "analog_control"
analog_control_names = ["FPS Mouse", "WASD"]
```

## Top-Level Fields

```toml
name = "FPS Mouse"
description = "Right stick mouse"
input_type = "stick"         # "stick" (2D) or "axis" (1D)
```

## Mouse Motion

```toml
[mouse_motion]
enabled = true
mode = "velocity"            # "velocity" or "area" (area is stick-only)
speed = 900.0                # pixels/sec (axis, or stick fallback)
speed_x = 900.0              # stick only; defaults to speed
speed_y = 900.0              # stick only; defaults to speed
area_radius_x = 400.0        # area mode only
area_radius_y = 400.0        # area mode only
area_start_enabled = false   # area mode: jump to start position first
area_start_x = 0
area_start_y = 0
deadzone = 0.15              # 0.0–0.95
sensitivity = 1.0            # 0.1–2.0
response_curve = 1.0         # 0.25–4.0
direction = "right"          # axis only: left|right|up|down|horizontal|vertical
invert_x = false             # stick X, or 1D axis direction
invert_y = false             # stick only
tick_ms = 8                  # update interval in ms
```

### Velocity mode

Analog input controls movement speed. Stick controls use `speed_x` and
`speed_y` (falling back to `speed`). Axis controls use `speed` and
`direction` to choose which mouse axis to drive. `horizontal` and
`vertical` map both positive and negative source values to opposite
mouse directions.

### Area mode (stick only)

Stick position maps directly to a cursor position within a 2D area:

```text
target_x = shaped_x * area_radius_x
target_y = shaped_y * area_radius_y
```

Each event emits only the relative delta from the previous target.
Returning to rest brings the pointer back to the origin. When
`area_start_enabled = true`, the daemon moves the cursor to
`area_start_x`/`area_start_y` when the stick first leaves rest.

## Gamepad Output

```toml
[gamepad_output]
enabled = true
output_id = "same-device"    # same-device, hardware ID, or virtual-gamepad-N
deadzone = 0.0               # 0.0–0.95; values below are sent as centered
target = "same"              # same|left|right|analog
target_analog_id = ""        # required when target = "analog"
output_rest = 0              # raw value at rest (1D axis only)
output_direction = "both"    # min|max|both (1D axis only)
output_invert = false        # 1D axis only; inverts direction when output_direction = "both"
output_invert_x = false      # stick output only
output_invert_y = false      # stick output only
sensitivity = 1.0            # 0.1–2.0
response_curve = 1.0         # 0.25–4.0
```

### Output targets

- `same` — preserves the source side: `left_stick` writes `ABS_X`/`ABS_Y`,
  `right_stick` writes `ABS_RX`/`ABS_RY`, `left_trigger` writes `ABS_Z`,
  `right_trigger` writes `ABS_RZ`.
- `left` / `right` — forces the output side, allowing cross-routing (right
  stick to left stick, left trigger to right trigger, etc.).
- `analog` — routes to a learned analog output on the target hardware,
  identified by `target_analog_id`. The output must match the input shape
  (stick-to-stick or axis-to-axis). Runtime normalizes the source, applies
  output deadzone/sensitivity/curve, then converts to the learned output's
  min/max/rest range.

For physical hardware outputs, `same`, `left`, and `right` stick targets also
use the target stick's hardware min/max/center values when available. Virtual
Xbox gamepad stick targets use the standard `-32768` to `32767` range.

### Output direction (1D axis)

- `min` — maps input from rest toward the axis minimum.
- `max` — maps input from rest toward the axis maximum.
- `both` — treats the input as signed across the full min/rest/max range.

For `output_direction = "both"`, `output_invert = true` flips the signed
output around the rest value.

### Stick output inversion

For stick-to-stick output, `output_invert_x` and `output_invert_y` flip each
output axis independently after deadzone/sensitivity/curve shaping. Learned
hardware target-axis inversion is still honored and combines with these
per-control flags.

Virtual Xbox gamepads use fixed semantic targets (left/right stick and
trigger) and ignore `target_analog_id`.

## Thresholds (Digital Actions)

```toml
[[thresholds]]
axis = "x"                   # "x" or "y" (stick); always "x" (axis)
trigger_min = 0.65
trigger_max = 1.0
release_min = 0.55
release_max = 1.0
actions = [
  { action = "keyboard", target = "key_d" },
]
```

A threshold activates when the axis value enters the trigger range and
releases when it leaves the release range. The trigger range must be inside
the release range for explicit hysteresis.

Stick and 1D axis thresholds use `-1.0` to `1.0`. Positive ranges cover one
direction; negative ranges cover the opposite direction. Overlapping thresholds
are valid and evaluated independently.

## Input Shaping

Mouse motion and analog output share the same curve:

```text
distance   = sqrt(x² + y²)           # stick; or abs(x) for axis
normalized = (distance - deadzone) / (1 - deadzone)
output     = clamp((normalized ^ response_curve) * sensitivity, 0, 1)
```

- `sensitivity = 1.0`, `response_curve = 1.0` — linear
- Higher sensitivity reaches full output sooner
- `response_curve < 1.0` — faster near center
- `response_curve > 1.0` — slower near center (finer aim)

The curve mirrors for negative stick directions and for `output_direction = "both"`.

## Presets

For new users, the mapping dialog's **Presets** tab offers one-click starting
points (Mouse Move, Mouse Area, Scroll Wheel, WASD for sticks; Trigger Left
Click, Trigger Right Click, Trigger Scroll Up, Trigger Scroll Down for
triggers). A preset saves a normal, fully editable config and maps it to the
input — see [Game Controller Support](GAMEPAD.md) for the GUI workflow.

## Templates

The GUI provides templates for stick digital actions. Templates append
thresholds to the existing list; the result is fully editable.

| Template | Thresholds | Actions |
|----------|-----------|---------|
| WASD | 4 (±X, ±Y at 0.65) | `key_w`, `key_s`, `key_a`, `key_d` |
| Arrow Keys | 4 (±X, ±Y at 0.65) | `key_up`, `key_down`, `key_left`, `key_right` |
| Mouse Wheel | 4 (±X, ±Y at 0.55) | Scroll up/down (Y) and side-scroll left/right (X) with rapidfire (hold 20ms, wait 60ms) |

## Full Example

```toml
name = "FPS Mouse"
description = "Right stick mouse look"
input_type = "stick"

[mouse_motion]
enabled = true
mode = "velocity"
speed_x = 2000.0
speed_y = 900.0
deadzone = 0.30
sensitivity = 1.0
response_curve = 2.7
invert_x = false
invert_y = false
tick_ms = 8

[gamepad_output]
enabled = false
output_invert_x = false
output_invert_y = false
```

```toml
name = "Left Trigger Action"
description = "Fire on half-pull"
input_type = "axis"

[mouse_motion]
enabled = false

[gamepad_output]
enabled = false

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
