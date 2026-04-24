# Custom Modifier Combos

Use any spare key or button as a modifier-like combo leader.

Example:

| Trigger | Action |
|---|---|
| Caps Lock | Suppress |
| Caps Lock + H | Left |
| Caps Lock + J | Down |
| Caps Lock + K | Up |
| Caps Lock + L | Right |
| Caps Lock + U | Page Up |
| Caps Lock + O | Page Down |

This is useful when you want a compact navigation layer without changing your
keyboard layout or relying on app-specific shortcuts.

## Suppress The Leader

Pick the key or button that should act like your custom modifier.

In the **Device** tab, map it to **Suppress**:

```text
Caps Lock -> Suppress
```

Suppressing the leader keeps it from reaching applications when pressed by
itself. Keymasq combo capture still sees the original physical key, so the
same key can be used in combo triggers.

## Add The Combos

Go to the **Combos** tab and add one combo per action.

Create these single-step combos:

| Combo trigger | Action |
|---|---|
| Caps Lock + H | Keyboard: `key_left` |
| Caps Lock + J | Keyboard: `key_down` |
| Caps Lock + K | Keyboard: `key_up` |
| Caps Lock + L | Keyboard: `key_right` |
| Caps Lock + U | Keyboard: `key_pageup` |
| Caps Lock + O | Keyboard: `key_pagedown` |

When capturing each combo, hold Caps Lock and press the second key. Save the
combo with the matching keyboard action.

## Optional: Use Superkeys As Combo Actions

Combo actions can point at saved superkeys.

For example, create a superkey named:

```text
nav_word_select
```

Then bind a combo to it:

| Combo trigger | Action |
|---|---|
| Caps Lock + W | Super Key: `nav_word_select` |

This lets a custom modifier combo trigger richer tap, double-tap, hold, or
tap-and-hold behavior.

## Other Leader Inputs

The same pattern works with other keys and buttons:

- keyboard key: `Caps Lock`, `Menu`, `F13`
- mouse button: side button or extra button
- gamepad button: shoulder button or face button

For example:

```text
Mouse Back -> Suppress
Mouse Back + C -> Copy
Mouse Back + V -> Paste
```

Combos can also span devices, so the leader can be on a mouse while the second
input is on a keyboard.

## App-Specific Overrides

Put the default combos in a permanent profile. Then create conditional
profiles for apps that need different actions.

Example:

- Permanent profile: `Caps Lock + H/J/K/L` sends arrow keys.
- Terminal profile: the same combos send terminal-specific shortcuts.
- Editor profile: the same combos move by word, select text, or run editor
  commands.

If two active profiles define the same combo trigger, the later-applied
profile wins. Conditional profiles override permanent profiles, so app-specific
combos can replace the default behavior while that app is focused.

## Recall Trigger Keys

If the held leader interferes with the combo action, enable **Recall Trigger
Keys** in the combo editor.
