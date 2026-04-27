# Super Keys

## Overview

A super key lets you do more with a single key or button. Instead of one
key doing one thing, a super key can give that key multiple jobs depending
on how you press it — or let it do several things at once.

**Example:** you could turn your Caps Lock key into a smart Copy/Paste
key — tap it quickly to copy, hold it down to paste. One key, two useful
actions, no awkward finger stretching.

Keymasq gives you two ways to set this up:

- **Pattern mode** — the key watches *how* you press it (tap, double tap,
  hold, or tap-then-hold) and each gesture can trigger one action or a
  whole sequence of them.
- **Overload mode** — the key does multiple things every time you press it,
  like pressing Ctrl and C together from a single button.

A super key uses one mode or the other, never both.

Super keys are saved on their own, separate from profiles, so you can
reuse the same super key across different devices and profiles without
setting it up again each time.

## Modes

### Pattern (Gesture Recognition)

Keymasq watches how you press the source key and chooses one of four
slots:

| Interaction | What it means |
|---|---|
| **Tap** | Quick press and release. |
| **Double Tap** | Two quick taps in a row. |
| **Hold** | Press and hold past the threshold. |
| **Tap + Hold** | Tap once, then press and hold a second time. |

Each slot can run an ordered bundle of actions. When a slot fires:

- Actions press in list order.
- Hold-style releases happen in reverse order.
- Tap-style releases also happen in reverse order after the short tap pulse.

That makes bundles like `Ctrl` then `C`, or `Shift` then `Tab`, behave
correctly.

### Overload (Multi-Output)

Overload mode does not do gesture recognition. Instead, the source key
behaves like a one-to-many normal mapping and forwards its down, repeat,
and up cycle to multiple child actions.

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

Pattern-slot actions use the same picker as normal mappings, except nested
superkeys and mapping-only control actions are not available. Pattern slots can be:

| Action type | What it does |
|---|---|
| **Keyboard** | Send a keyboard key. |
| **Mouse** | Send a mouse button. |
| **Mouse Move** | Move the cursor relative or absolute. |
| **Gamepad** | Send a gamepad button or trigger. |
| **Macro** | Play a saved macro. |
| **Command** | Run a shell command. |
| **Compositor Dispatch** | Send a compositor-specific command. |
| **Macro Controls** | Toggle recording, stop recording, or cancel playback. |
| **Profile Controls** | Enable, disable, or toggle a profile. |

### Editing Overload Actions

In overload mode, the editor shows one ordered list: **Overload Actions**.
Those children use the normal mapping action picker, so overload keys can mix
the same kinds of actions that regular mappings can use, except:

- **Passthrough** is not available
- **Suppress** is not available
- **Super Key** is not available

## Rapidfire

Rapidfire (see [Actions](ACTIONS.md)) only applies to hold-style pattern
slots:

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

Pattern mode uses the following decision flow:

```text
Press key
 ├─ Release quickly? -> Tap
 │   └─ Press again quickly?
 │       ├─ Release quickly? -> Double Tap
 │       └─ Keep holding? -> Tap + Hold
 └─ Keep holding past threshold? -> Hold
```

The chosen slot then runs its action bundle.

### Concurrency

The same super key can still be assigned to multiple inputs at once. Keymasq
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

Combos can also trigger saved super keys:

- `Overload` works for both single-step and multi-step combos.
- `Pattern` works fully on single-step combos.
- On multi-step combos, only the `Tap` and `Hold` slots are used.

## Deleting A Super Key

Deleting a super key replaces all profile references to it with **Suppress**.
You will not be left with broken references.

## Storage

Super keys live in:

```text
~/.config/keymasq/superkeys/
```

The file format is strict: each super key must declare `mode`, and every
action slot uses a TOML array even when it contains only one action.

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
- Prefer testing bindings with a text editor or game input viewer after changes.

## See Also

- [Actions](ACTIONS.md)
- [Macros](MACROS.md)
- [Combos](COMBOS.md)
