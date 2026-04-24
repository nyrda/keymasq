# Universal Copy And Paste

Use one key or combo for copy and paste everywhere, while still handling apps
that use different shortcuts.

Example behavior:

| App type | Copy | Paste |
|---|---|---|
| Normal desktop apps | `Ctrl+C` | `Ctrl+V` |
| Terminals | `Ctrl+Shift+C` | `Ctrl+Shift+V` |
| Insert-style apps | `Ctrl+Insert` | `Shift+Insert` |

This uses one permanent profile for the normal behavior, then conditional
profiles that override the same key or combo for specific windows.

## Create The Default Superkey

Open **Super Keys** and create a **Pattern** superkey:

```text
copy_paste_default
```

Set the slots:

| Slot | Action |
|---|---|
| Tap | Keyboard: `key_leftctrl` + `key_c` |
| Double Tap | Keyboard: `key_leftctrl` + `key_x` |
| Hold | Keyboard: `key_leftctrl` + `key_v` |
| Tap + Hold | Keyboard: `key_leftctrl` + `key_leftshift` + `key_v` |

## Add It To A Permanent Profile

Create or select a permanent profile such as:

```text
Base Editing
```

Bind your preferred key, mouse button, or combo to **Super Key**:

```text
copy_paste_default
```

This is the fallback behavior used everywhere unless a conditional profile
overrides it.

## Create A Terminal Override

Open **Super Keys** and create another **Pattern** superkey:

```text
copy_paste_terminal
```

Set the slots:

| Slot | Action |
|---|---|
| Tap | Keyboard: `key_leftctrl` + `key_leftshift` + `key_c` |
| Double Tap | No action |
| Hold | Keyboard: `key_leftctrl` + `key_leftshift` + `key_v` |
| Tap + Hold | No action |

Create a conditional profile:

```text
Terminal Editing
```

Add a window rule for **class**. A single regex can match several terminals:

```regex
(?i)^(alacritty|kitty|foot|org.wezfurlong.wezterm|gnome-terminal-server|konsole)$
```

Bind the same key, mouse button, or combo you used in `Base Editing`, but set
it to **Super Key**:

```text
copy_paste_terminal
```

When a matching terminal is focused, this conditional profile layers over the
permanent profile and replaces the normal copy/paste binding.

If you use a combo trigger, create the same combo in the conditional profile.
When the trigger is identical, the active conditional profile replaces the
permanent profile's combo action.

## Optional: Insert-Style Override

Some applications handle `Ctrl+Insert` and `Shift+Insert` more reliably than
`Ctrl+C` and `Ctrl+V`.

Create another **Pattern** superkey:

```text
copy_paste_insert
```

Set the slots:

| Slot | Action |
|---|---|
| Tap | Keyboard: `key_leftctrl` + `key_insert` |
| Double Tap | No action |
| Hold | Keyboard: `key_leftshift` + `key_insert` |
| Tap + Hold | No action |

Then create a conditional profile for those apps and bind the same trigger to
`copy_paste_insert`.

## Regex Notes

Window rules use regular expressions. To match several apps with one rule,
use `|` as "or":

```regex
app_one|app_two|app_three
```

Use anchors when you want exact matches:

```regex
^(app_one|app_two|app_three)$
```

Use `(?i)` for case-insensitive matching:

```regex
(?i)^(alacritty|kitty|foot)$
```

All window rules in one profile must match. If you add both a **class** rule
and a **title** rule, the profile activates only when both match the focused
window.

If you capture the active window from the GUI, Keymasq may add both class and
title rules. Delete the title rule when you want one class regex to match all
windows from several apps.

## Why This Shape

Keep the normal binding in a permanent profile. Put only the app-specific
replacement in conditional profiles.

That keeps the setup small:

- one key or combo to remember
- one default behavior everywhere
- focused overrides only where apps need different shortcuts
