# Game Controller Support

Keymasq can remap game controller buttons, sticks, triggers, wheels, and
other analog axes. It can also turn keyboard and mouse input into virtual
gamepad output for games that expect a controller.

## Adding a Controller

When you add a game controller in the hardware setup flow, Keymasq detects
its buttons and analog axes from evdev capabilities and creates the hardware
profile automatically. Standard buttons (face, shoulders, start/select/guide,
stick clicks, digital D-pad) are added when the controller reports them.
Third-party uinput controllers and wheels are shown in the picker when they
report gamepad capabilities; Keymasq's own virtual output devices stay hidden.

When Keymasq grabs a physical gamepad, it creates a passthrough uinput clone
for unmapped events. That clone reuses the source controller name and input
IDs, so Steam and other tools see it as the same controller model.

## Button Mapping

Keymasq uses an Xbox 360 controller as the template for game controllers.
When you add a controller, buttons that the hardware reports are included
automatically. Buttons the controller does not advertise are omitted — you
can add extra buttons later from the device tab using the listen/capture
flow.

Remap any button from the device tab: click it in the grid and pick an
action. Each button supports the same options as keyboard/mouse mappings,
including rapidfire and tap (see [Actions](ACTIONS.md)).

### Axis Output

You can map any key or button to a gamepad axis value — useful for binding
keyboard keys to stick or trigger output. The mapping sends a fixed axis
value while the source is held and returns to neutral on release.

Triggers are analog axes, not buttons. Gamepad button mappings do not
produce trigger output — use axis mappings instead.

## Analog Controls

Analog Controls let you map sticks, triggers, and other analog axes to
mouse movement, keyboard/button actions, or gamepad output. They are
reusable configs — create one and assign it to any analog input across
devices and profiles.

Configs are saved in `~/.config/keymasq/analog_controls/` and managed from
the **Analog Controls** dialog (accessible from the main menu).

### Learning Analog Inputs

The controller template already includes standard left and right stick
inputs, so most controllers are ready for analog remapping out of the box.
Use **Learn Analog** when your controller has additional or non-standard
analog axes that the template does not cover. You can also delete a
template stick and re-add its axes individually as 1D axes — useful when
you want to remap a single stick direction to an analog trigger or other
1D output.

1. Open the **Device** tab for your controller.
2. Click **Learn Analog**.
3. Choose the input type:
   - **Stick** — a 2D input like a thumbstick (captures two axes)
   - **Generic Axis** — a 1D input like a trigger, slider, or pedal
4. Give it an ID and label (e.g. `left_stick` / "Left Stick").
5. Start the capture and move the physical control through its full range.
6. Review the detected axes: evdev code, min/max values, and center or rest
   position. Edit if needed.
7. Save — the input is now available for remapping.

Right-click a learned analog input label on the device tab to rename or
delete it.

### Assigning an Analog Control

Once you have a learned analog input and a saved analog control config:

1. Open the **Device** tab and select the analog input.
2. In the mapping dialog, choose a saved analog control by name.
3. One input can fan out to multiple configs — each receives the same
   normalized input and handles its output independently:

```toml
[devices."045e:028e".mapping.right_stick]
action = "analog_control"
analog_control_names = ["FPS Mouse", "WASD"]
```

### Input Types

| Type | Axes | Normalized Range | Use Cases |
|------|------|-----------------|-----------|
| **Stick** | X + Y | `-1.0` to `1.0` per axis | Thumbsticks, flight sticks, analog D-pads |
| **Axis** | X only | `-1.0` to `1.0` for signed ranges | Triggers, sliders, pedals, throttles |

### Modes

Each analog control operates in one mode:

#### Mouse Movement

Converts analog input into continuous mouse cursor movement. The stick or
axis controls the speed and direction of movement — tilt further to move
faster.

**Stick settings:**
- **Horizontal Speed** / **Vertical Speed** — maximum pixels per second for
  each axis (can be split or linked)
- **Invert Axes** — flip X or Y direction

**Axis settings:**
- **Speed** — maximum pixels per second
- **Direction** — which way the axis moves the cursor: `left`, `right`,
  `up`, `down`, `horizontal` (both left/right), or `vertical` (both
  up/down)

**Shared settings:**
- **Deadzone** — fraction of travel to ignore near center (0.0–0.95)
- **Sensitivity** — output multiplier (0.1–2.0); higher reaches full speed
  sooner
- **Response Curve** — exponent shaping the input-to-output curve
  (0.25–4.0); below 1.0 is faster near center, above 1.0 is slower near
  center

#### Mouse Area (stick only)

Maps the stick position directly to a cursor position within a 2D area.
Instead of controlling speed, the stick controls where the cursor is — push
right and the cursor moves right, release and it returns to the origin.

- **Horizontal Radius** / **Vertical Radius** — size of the area in pixels
  from the center point
- **Anchor to a Start Position** — when enabled, the cursor jumps to a
  fixed screen coordinate when the stick first leaves rest, then moves
  relative to that anchor
- **Start Position** — the anchor coordinate; use **Capture** to click a
  point on screen
- **Deadzone**, **Sensitivity**, **Response Curve** — same as Mouse
  Movement
- **Invert Axes** — flip X or Y

#### Digital Actions

Fires keyboard, mouse, or other actions when the analog input crosses a
threshold range. Useful for turning a stick into WASD or a trigger into a
button press.

Each threshold defines:
- **Axis** — `x` or `y` (sticks) or `x` (axes)
- **Trigger range** — the value range that activates the action
- **Release range** — a wider range around the trigger for hysteresis (so
  the action doesn't flicker at the boundary)
- **Actions** — one or more actions to fire (keyboard, mouse, gamepad, etc.)

Stick and 1D axis thresholds use values from `-1.0` to `1.0`. Positive ranges
cover one direction; negative ranges cover the opposite direction. Multiple
thresholds can overlap — they are evaluated independently.

**Templates** (stick only):
- **WASD** — maps stick to W/A/S/D keys
- **Arrow Keys** — maps stick to arrow keys
- **Mouse Wheel** — maps stick Y to scroll up/down with rapidfire

Templates append thresholds to the existing list and are fully editable
after applying.

#### Analog Output

Routes the analog source to a gamepad axis on a selected output device.
Use this to remap one stick to another, route a trigger to a different
controller, or pass through with adjusted tuning.

- **Output** — target device: a virtual gamepad, the same physical device
  (`same-device`), or another grabbed controller by hardware ID
- **Output Control** — which axis to write:
  - `Same Axis` — preserves the source side (left stick stays left stick)
  - `Left Trigger` / `Right Trigger` or `Left` / `Right` — forces the
    output side
  - Learned analog outputs on physical hardware are also available
- **Output Deadzone** — values below this are sent as centered/released
- **Output Rest** — raw value written when the axis is at rest (1D axes)
- **Output Direction** — `Min`, `Max`, or `Both` (1D axes):
  - `Min` maps from rest toward the minimum endpoint
  - `Max` maps from rest toward the maximum endpoint
  - `Both` treats the input as signed across the full range
- **Sensitivity** — output multiplier (0.1–2.0)
- **Response Curve** — exponent for output shaping (0.25–4.0)

### Tuning: Sensitivity and Response Curve

All modes share the same input shaping math. The analog input is first
normalized and deadzone-removed, then shaped:

```text
distance = magnitude of normalized input
shaped   = clamp((distance ^ response_curve) * sensitivity, 0, 1)
```

- **Sensitivity = 1.0, Response Curve = 1.0** — linear (default)
- **Higher sensitivity** — reaches full output before full physical travel
- **Response Curve < 1.0** — faster response near center, coarser at edges
- **Response Curve > 1.0** — finer control near center, faster at edges
  (good for aiming)

Stick controls apply this radially. Mouse Movement then multiplies the
shaped value by speed. Analog Output maps it to the target axis range.

### Creating and Editing Analog Controls

Open **Analog Controls** from the GUI main menu. The dialog has two panels:

- **Left panel**: lists all saved analog controls, grouped by type
  (sticks / axes). Use **+** to create or **Delete** to remove.
- **Right panel**: edit the selected config's name, description, input
  type, mode, and mode-specific settings.

Use **Save** to apply changes. If you switch selection or close the dialog
with unsaved edits, Keymasq asks whether to save, discard, or keep editing.

## Example Profile

```toml
[profile]
name = "Controller Remap"
enabled = true
is_permanent = true
priority = 0

# Button remaps
[devices."045e:028e".mapping.btn_south]
action = "gamepad"
target = "btn_a"

[devices."045e:028e".mapping.btn_east]
action = "gamepad"
target = "btn_b"
rapidfire_enabled = true
rapidfire_hold_ms = 30
rapidfire_wait_ms = 20

# Button to trigger axis
[devices."046d:c548".mapping.btn_back]
action = "gamepad_axis"
target = "abs_z"
value = 255

# Analog control (stick to mouse)
[devices."045e:028e".mapping.right_stick]
action = "analog_control"
analog_control_name = "FPS Mouse"

# Multiple analog controls on one input
[devices."045e:028e".mapping.left_stick]
action = "analog_control"
analog_control_names = ["WASD", "Mouse Wheel"]
```

## Game Compatibility

The virtual gamepad appears as a standard Linux gamepad:

- **SDL2/SDL3** — full support
- **Steam Input** — full support
- **Wine/Proton** — works via evdev translation
- **Native Linux games** — works with evdev-compatible games

Use [jstest-gtk](https://github.com/Grumbel/jstest-gtk) to verify that
your virtual gamepad buttons and axes are working as expected.

## Technical Details

### Virtual Gamepads

When you map an input to a gamepad action, Keymasq creates a virtual gamepad
using Linux uinput. Games see it as a standard Xbox 360 controller.

Keymasq creates one virtual gamepad by default. You can configure 0–4 from
the GUI hamburger menu under **Settings**. The setting is stored in
`~/.config/keymasq/settings.toml`:

```toml
[gamepads]
virtual_count = 1
```

Each virtual gamepad uses Xbox 360 hardware IDs:

- **Name**: `keymasq-gamepad` (first), `keymasq-gamepad-2` through `-4`
- **Vendor**: `0x045e` (Microsoft)
- **Product**: `0x028e` (Xbox 360 controller)
- **Capabilities**: 17 digital buttons, 8 analog axes

### Output Routing

Gamepad actions can set `output_id` to route output to a specific device.
Valid values:

| Value | Target |
|-------|--------|
| `virtual-gamepad-1` … `virtual-gamepad-4` | A configured virtual gamepad |
| A hardware ID (e.g. `045e:028e@2`) | A grabbed physical controller |

If `output_id` is omitted, output goes to `virtual-gamepad-1`. If the
target is not configured, connected, or grabbed, `keymasqd` logs a warning
and drops the output with no fallback.

Macro gamepad events use the same `output_id` routing. Events without
`output_id` play through the default gamepad.

### Linux Gamepad Specification

Keymasq follows the
[Linux Gamepad Specification](https://kernel.org/doc/html/latest/input/gamepad.html).
Button positions are based on physical location, not labels.

## Limitations

- **Touchpads**: controller touchpads are not supported for remapping yet.
- **Dedicated drivers**: vendor-specific features may still need their
  native driver or Steam Input.

## See Also

- [Analog Controls Config Format](ANALOG_CONTROLS.md) — TOML reference for
  analog control configs
- [Actions](ACTIONS.md)
- [Super Keys](SUPERKEYS.md)
