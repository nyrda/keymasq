# Gamepad Output Support

Keyforge can remap mouse buttons, keyboard keys, or any input to virtual gamepad buttons. This is useful for games that expect controller input or for using mouse buttons as gamepad controls.

## How It Works

When you map a button to a gamepad action, Keyforge creates a virtual gamepad device using Linux uinput. Games see this as a standard gamepad and can receive input from it.

## Available Gamepad Buttons

The virtual gamepad appears as an Xbox 360 controller. Button codes follow the Linux evdev naming convention.

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
| `btn_tl2` | LT (Left Trigger, digital) | 312 |
| `btn_tr2` | RT (Right Trigger, digital) | 313 |

### Shoulder Buttons (Bumpers & Triggers)
| Code | Xbox | PlayStation | Description |
|------|------|-------------|-------------|
| `btn_tl` | LB | L1 | Left bumper |
| `btn_tr` | RB | R1 | Right bumper |
| `btn_tl2` | LT | L2 | Left trigger (digital) |
| `btn_tr2` | RT | R2 | Right trigger (digital) |

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
```

## Rapidfire and Tap

Gamepad buttons support the same rapidfire and tap options as keyboard/mouse mappings:

- **Rapidfire**: Rapidly press the button while held
  - `rapidfire_hold_ms`: How long each press lasts
  - `rapidfire_wait_ms`: Delay between presses

- **Tap**: Press and release automatically
  - `tap_hold_ms`: How long the button is held

## GUI Configuration

1. Select a device and profile
2. Click on a button in the grid
3. Select "Gamepad Button" from the function dropdown
4. Enter the target button code (e.g., `btn_south`, `btn_tl`)
5. Configure rapidfire or tap if needed
6. Click Apply

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

Keyforge creates a uinput device that appears as an Xbox 360 controller:

- **Name**: `Microsoft X-Box 360 pad`
- **Vendor ID**: `0x045e` (Microsoft)
- **Product ID**: `0x028e` (Xbox 360 controller)
- **Capabilities**:
  - 17 digital buttons (all standard gamepad buttons)
  - 8 analog axes (sticks, triggers, D-pad)

### Linux Gamepad Specification

Keyforge follows the [Linux Gamepad Specification](https://kernel.org/doc/html/latest/input/gamepad.html):

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
btn_tl, btn_tr, btn_tl2, btn_tr2,
btn_select, btn_start, btn_mode,
btn_thumbl, btn_thumbr,
btn_dpad_up, btn_dpad_down, btn_dpad_left, btn_dpad_right
```

For Xbox-style naming, use: `btn_a`, `btn_b`, `btn_x`, `btn_y`

## Limitations

- **Analog axes**: Currently, only digital button output is supported
- **Triggers**: Digital only (on/off, not pressure-sensitive)
- **Sticks**: No analog stick emulation (use keyboard/mouse for movement)

## Future Enhancements

Planned features for gamepad support:

- [ ] Analog trigger support (pressure-sensitive)
- [ ] Analog stick emulation from mouse movement
- [ ] Multi-button combos
- [ ] Gamepad-to-gamepad remapping
