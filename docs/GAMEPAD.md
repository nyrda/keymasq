# Gamepad Output Support

Keymasq can remap mouse buttons, keyboard keys, or any input to virtual gamepad buttons and analog axes. This is useful for games that expect controller input or for using mouse buttons as gamepad controls.

## How It Works

When you map a button to a gamepad action, Keymasq creates a virtual gamepad device using Linux uinput. Games see this as a standard gamepad and can receive input from it.

Keymasq creates one virtual Xbox 360 gamepad by default. You can configure
0-4 virtual gamepads from the GUI hamburger menu under **Settings**. The
setting is stored in `~/.config/keymasq/settings.toml`:

```toml
[gamepads]
virtual_count = 1
```

Gamepad actions can optionally set `output_id` to route output. Valid values
are `virtual-gamepad-1` through `virtual-gamepad-4`, or a configured hardware
gamepad ID such as `045e:028e@2`. If `output_id` is omitted, output uses the
default `virtual-gamepad-1`. Explicit `output_id` values are strict: if the
target is not configured, connected, or grabbed, `keymasqd` logs a warning and
drops the output with no fallback.

## Available Gamepad Buttons

The virtual gamepad appears as an Xbox 360 controller. Button codes follow the Linux evdev naming convention.

## Analog Axis Output

Use `action = "gamepad_axis"` to set a gamepad axis to a specific raw evdev
value while the source input is held. Releasing the source input returns the
axis to neutral `0`.

| Target | Control | Range |
|--------|---------|-------|
| `abs_x` | Left stick X | `-32768..32767` |
| `abs_y` | Left stick Y | `-32768..32767` |
| `abs_rx` | Right stick X | `-32768..32767` |
| `abs_ry` | Right stick Y | `-32768..32767` |
| `abs_z` | Left trigger | `0..255` |
| `abs_rz` | Right trigger | `0..255` |

Example:

```toml
[devices."046d:c548".mapping.btn_back]
action = "gamepad_axis"
target = "abs_x"
value = -32768
output_id = "virtual-gamepad-2"
```

Triggers are analog axis outputs. Use `abs_z` for LT and `abs_rz` for RT.
`gamepad` actions are button-only and do not translate trigger button aliases
to axes.

### Face Buttons
| Code | Xbox | Position | evdev Code |
|------|------|----------|------------|
| `btn_south` or `btn_a` | A | Bottom | 304 |
| `btn_east` or `btn_b` | B | Right | 305 |
| `btn_north` or `btn_x` | X | **Left** | 307 |
| `btn_west` or `btn_y` | Y | **Top** | 308 |

> **Note on naming**: Despite the names "north" and "west", `BTN_NORTH` (307) actually maps to the X button (left position) and `BTN_WEST` (308) maps to the Y button (top position). For clarity, use `btn_a`, `btn_b`, `btn_x`, `btn_y` which match Xbox button names directly.

### Shoulder Buttons
| Code | Xbox | evdev Code |
|------|------|------------|
| `btn_tl` | LB (Left Bumper) | 310 |
| `btn_tr` | RB (Right Bumper) | 311 |

### Shoulder Buttons
| Code | Xbox | PlayStation | Description |
|------|------|-------------|-------------|
| `btn_tl` | LB | L1 | Left bumper |
| `btn_tr` | RB | R1 | Right bumper |

### Thumb Buttons (Stick Clicks)
| Code | Xbox | PlayStation | Description |
|------|------|-------------|-------------|
| `btn_thumbl` | L3 | L3 | Left stick click |
| `btn_thumbr` | R3 | R3 | Right stick click |

### D-Pad Buttons
| Code | Direction |
|------|-----------|
| `btn_dpad_up` | Up |
| `btn_dpad_down` | Down |
| `btn_dpad_left` | Left |
| `btn_dpad_right` | Right |

### Menu Buttons
| Code | Xbox | PlayStation | Description |
|------|------|-------------|-------------|
| `btn_select` | Back | Select | Left menu button |
| `btn_start` | Start | Start | Right menu button |
| `btn_mode` | Guide | PS | Center/home button |

## Example Profile Configuration

```toml
[profile]
name = "Gamepad Mode"
enabled = true
is_permanent = true
priority = 0
notify_on_activation = true
created_at = "2026-03-09T12:34:56"

[devices."046d:c548"]
always_grab_all = false

[devices."046d:c548".mapping.extra_1]
action = "gamepad"
target = "btn_tl"  # Left Bumper (LB)

[devices."046d:c548".mapping.extra_2]
action = "gamepad"
target = "btn_tr"  # Right Bumper (RB)

[devices."046d:c548".mapping.btn_middle]
action = "gamepad"
target = "btn_a"  # A button (bottom)

[devices."046d:c548".mapping.btn_back]
action = "gamepad"
target = "btn_y"  # Y button (top)
rapidfire_enabled = true
rapidfire_hold_ms = 30
rapidfire_wait_ms = 20

[devices."046d:c548".mapping.btn_forward]
action = "gamepad"
target = "btn_a"
output_id = "virtual-gamepad-2"
```

## Rapidfire and Tap

Gamepad buttons support the same rapidfire and tap options as keyboard/mouse mappings:

- **Rapidfire**: Rapidly press the button while held
  - `rapidfire_hold_ms`: How long each press lasts
  - `rapidfire_wait_ms`: Delay between presses

- **Tap**: Press and release automatically
  - `tap_hold_ms`: How long the button is held

## GUI Configuration

### Mapping to Virtual Gamepad Output

1. Select a device and profile
2. Click on a button in the grid
3. Select "Gamepad Button" from the function dropdown
4. Enter the target button code (e.g., `btn_south`, `btn_tl`)
5. Configure rapidfire or tap if needed
6. Click Apply

### Adding a Physical Gamepad

When you add a gamepad in the hardware setup flow, Keymasq now detects its reported
digital gamepad buttons from evdev capabilities and creates the hardware profile
automatically. Standard buttons such as face buttons, shoulders, start/select/guide,
stick clicks, and digital D-pad buttons are added when the controller reports them.
Third-party uinput controllers and wheels can be added when they report gamepad
capabilities; Keymasq's own virtual output devices stay hidden from the picker.
Devices with controller-style axes but non-standard joystick buttons are treated
as gamepads, and their buttons can be added later with the learn flow.

Analog axes still passthrough normally, but they are not editable remap sources yet.
If your controller has unusual extra digital buttons, you can add them later from the
device tab using the same listen/capture flow used for keyboards and mice.

When Keymasq grabs a physical gamepad, it creates a passthrough uinput clone for
unmapped controller events. That clone reuses the source controller name and input
IDs, so tools such as Steam see it as the same controller model rather than a
generic Keymasq device.

## Game Compatibility

The virtual gamepad appears as a standard Linux gamepad. Most games detect it automatically through:

- **SDL2/SDL3**: Full support
- **Steam Input**: Full support
- **Wine/Proton**: Works via evdev translation
- **Native Linux games**: Works with evdev-compatible games

### Tips for Best Compatibility

1. **Use standard button names**: Games expect buttons in standard positions
2. **Test with your game**: Some games may have specific controller requirements
3. **Steam games**: Enable Steam Input for best controller support

## Technical Details

### Virtual Device Created

Keymasq creates a uinput device with Xbox 360 hardware IDs for maximum compatibility:

- **Name**: `keymasq-gamepad`
- **Vendor ID**: `0x045e` (Microsoft)
- **Product ID**: `0x028e` (Xbox 360 controller)
- **Capabilities**:
  - 17 digital buttons (all standard gamepad buttons)
  - 8 analog axes (sticks, triggers, D-pad)

Additional configured virtual gamepads use the same Xbox 360 template. Their
names are `keymasq-gamepad-2`, `keymasq-gamepad-3`, and
`keymasq-gamepad-4`.

Macro gamepad events may include `output_id` and use the same strict routing
rules as gamepad mappings. Macro events without `output_id` still play through
the default gamepad output.

### Linux Gamepad Specification

Keymasq follows the [Linux Gamepad Specification](https://kernel.org/doc/html/latest/input/gamepad.html):

- Button positions are based on physical location, not labels
- D-pad can be digital buttons or analog hat

> **Note on evdev naming**: The evdev aliases `BTN_NORTH` and `BTN_WEST` are confusingly named. 
> Despite "north" suggesting "top", `BTN_NORTH` (307) actually maps to the X button (left position) 
> on Xbox controllers. Similarly, `BTN_WEST` (308) maps to the Y button (top position). 
> For this reason, using `btn_x`/`btn_y` is recommended for clarity.

### Button Code Reference

The full list of available codes (case-insensitive):

```
btn_south (btn_a), btn_east (btn_b), btn_north (btn_x), btn_west (btn_y),
btn_tl, btn_tr,
btn_select, btn_start, btn_mode,
btn_thumbl, btn_thumbr,
btn_dpad_up, btn_dpad_down, btn_dpad_left, btn_dpad_right
```

For Xbox-style naming, use: `btn_a`, `btn_b`, `btn_x`, `btn_y`

## Limitations

- **Analog axes**: physical gamepad axes pass through, but they are not
  editable remap sources
- **Triggers**: virtual gamepad output is digital only (on/off, not
  pressure-sensitive)
- **Sticks**: no analog stick emulation for virtual output
- **Gamepad-to-gamepad**: remapping one gamepad to another is not supported
