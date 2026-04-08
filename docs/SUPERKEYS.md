# Super Keys

## Overview

A super key turns one physical key or button into either:

- A **pattern** key that reacts differently to Tap, Double Tap, Hold, and
  Tap + Hold.
- An **overload** key that fans one input out to multiple normal actions using
  the source key's usual down, repeat, and up cycle.

These two modes are exclusive. A single super key uses one mode or the other.

Super keys are saved separately from profiles and can be reused across
multiple profiles and devices.

The file format is strict: each superkey must declare `mode`, and every
action slot uses a TOML array even when it contains only one action.

## Modes

### Pattern

Pattern mode is the original super key behavior. Keyforge watches how you
press the source key and chooses one of four slots:

| Interaction | What it means |
|---|---|
| **Tap** | Quick press and release. |
| **Double Tap** | Two quick taps in a row. |
| **Hold** | Press and hold past the threshold. |
| **Tap + Hold** | Tap once, then press and hold a second time. |

Each slot now accepts an **ordered list of actions**, not just one action.
When a slot fires:

- Actions press in list order.
- Hold-style releases happen in reverse order.
- Tap-style releases also happen in reverse order after the short tap pulse.

That makes bundles like `Ctrl` then `C`, or `Shift` then `Tab`, behave
correctly.

### Overload

Overload mode does not do gesture recognition. Instead, it treats the source
key like a normal mapped key and forwards that key's lifecycle to an ordered
list of child actions.

Examples:

- One mouse side button presses both `key_leftctrl` and `key_c`
- One keyboard key presses two gamepad buttons
- One button triggers a key press plus a profile toggle

Overload actions use the same runtime rules as normal mappings:

- Key and button outputs receive down, repeat, and up.
- Exec, macro, profile, and compositor actions fire on press, just like
  ordinary mappings.
- Nested superkeys are not allowed.

## Creating And Editing Super Keys

Open **Super Keys** from the GUI. The dialog has two panels:

- **Left panel**: lists all saved super keys. Use **New** to create one or
  **Delete** to remove one.
- **Right panel**: edit the selected super key's name, description, mode,
  actions, and timing.

### Editing Pattern Slots

In pattern mode, each slot has its own ordered action list:

- **Tap**
- **Double Tap**
- **Hold**
- **Tap + Hold**

Click **Edit** on a slot to manage its list. Inside the slot editor you can:

- Add actions
- Edit an existing action
- Move actions up or down
- Remove actions

Pattern-slot actions can be:

| Action type | What it does |
|---|---|
| **Keyboard** | Send a keyboard key. |
| **Mouse** | Send a mouse button. |
| **Gamepad** | Send a gamepad button or trigger. |
| **Macro** | Play a saved macro. |
| **Command** | Run a shell command. |

### Editing Overload Actions

In overload mode, the editor shows one ordered list: **Overload Actions**.
Those children use the normal mapping action picker, so overload keys can mix
the same kinds of actions that regular mappings can use, except:

- **No Override** is not available
- **Passthrough** is not available
- **Suppress** is not available
- **Super Key** is not available

## Rapidfire

Rapidfire still belongs to hold-style pattern actions:

- **Hold**
- **Tap + Hold**

If a hold-style slot contains multiple actions, rapidfire is configured per
action. Non-rapidfire actions stay held normally while rapidfire actions pulse
for as long as the slot is active.

## Timing

The **Timing** section only matters in pattern mode.

| Setting | What it controls | Default | Range |
|---|---|---|---|
| **Tap Timeout** | Maximum time for a tap. | 200 ms | 50-1000 ms |
| **Double Tap Window** | Time between taps for a double tap. | 300 ms | 50-1000 ms |
| **Hold Threshold** | Time before Hold or Tap + Hold activates. | 300 ms | 50-2000 ms |

Overload mode ignores these timing values because it does not do gesture
recognition.

## Runtime Behavior

### Pattern Flow

Pattern mode still uses the same decision flow:

```text
Press key
 ├─ Release quickly? -> Tap
 │   └─ Press again quickly?
 │       ├─ Release quickly? -> Double Tap
 │       └─ Keep holding? -> Tap + Hold
 └─ Keep holding past threshold? -> Hold
```

The difference is that the chosen slot now runs a bundle instead of a single
action.

### Concurrency

The same super key can still be assigned to multiple inputs at once. Keyforge
keeps the outputs stable when two source keys share the same super key and
both hold the same child output.

That applies to:

- Pattern-mode held outputs
- Overload-mode held key and button outputs

## Using Super Keys

Assign a saved super key from the **Device** tab:

1. Pick a key or button on your device.
2. Set the action to **Super Key**.
3. Choose the saved super key by name.

The same super key can be reused on different devices and in different
profiles.

## Deleting A Super Key

Deleting a super key replaces all profile references to it with **Suppress**.
You will not be left with broken references.

## Storage

Super keys live in:

```text
~/.config/keyforge/superkeys/
```

### Pattern Example

```toml
name = "copy_stack"
mode = "pattern"
description = "Tap for copy, hold for paste"

[timing]
hold_threshold_ms = 250

[actions]
tap = [
    { action = "keyboard", target = "key_leftctrl" },
    { action = "keyboard", target = "key_c" },
]
hold = [
    { action = "keyboard", target = "key_leftctrl" },
    { action = "keyboard", target = "key_v" },
]
```

### Overload Example

```toml
name = "ctrl_click_pair"
mode = "overload"

[actions]
overload = [
    { action = "keyboard", target = "key_leftctrl" },
    { action = "mouse", target = "btn_left" },
]
```

## Security Notes

- **Command actions** run inside your user session, not inside the privileged
  daemon, but they still execute automatically on a key press.
- **Compositor actions** interact directly with your desktop environment.
  Review them before putting them into overload lists.

## Best Practices

- Name super keys by purpose, not by the hardware key they are bound to.
- Use pattern mode when timing matters and overload mode when you want plain
  one-to-many remapping.
- Keep bundled actions in the exact order you want them pressed.
- Prefer testing chords with a text editor or game input viewer after changes.

## See Also

- [Actions](ACTIONS.md)
- [Macros](MACROS.md)
- [Combos](COMBOS.md)
