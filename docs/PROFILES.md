# Profile System

The profile system is how Keyforge stores and applies remaps. A profile is a
named set of mappings that can include layers for one device or several
devices.
Keyforge can activate more than one profile at the same time, then merge those
profiles into the final mapping for each device based on profile type,
priority, and active window rules.

## Mental Model

The easiest way to think about profiles is:

- a profile is a named layer of remaps
- more than one profile can be active at once
- for each device, Keyforge merges the matching layers from the active profiles

What "matching layers" means:

- a profile can contain a keyboard layer, a mouse layer, a gamepad layer, or
  any combination of them
- when input comes from your mouse, Keyforge only looks at the mouse parts of
  the active profiles
- when input comes from your keyboard, Keyforge only looks at the keyboard
  parts of the active profiles

Simple example:

- `Base` is your normal everyday profile
- `Gaming` is a profile that should only apply while a game window is focused
- `Base` contains keyboard and mouse mappings
- `Gaming` contains only mouse mappings

When you are on the desktop:

- only `Base` is active
- your keyboard uses the keyboard layer from `Base`
- your mouse uses the mouse layer from `Base`

When you focus the game window:

- `Base` is still active
- `Gaming` becomes active too
- your keyboard still uses only `Base`, because `Gaming` has no keyboard layer
- your mouse uses the merged result of `Base` and `Gaming`

So the system is not "one profile per device". It is "one or more active
profiles, each of which may contribute a layer to each device".

## Where Profiles Live

Profiles are stored in:

```text
~/.config/keyforge/profiles/<profile_name>.toml
```

The visible profile name can contain arbitrary characters. The on-disk filename is derived from that name by replacing unsafe filename characters so the file always stays inside `profiles/`.

Hardware definitions are still separate:

```text
~/.config/keyforge/hardware/<hardware_id>.toml
```

## Profile Types

There are two profile types.

### Permanent profiles

- Always active when enabled
- Applied before all conditional profiles
- Good for your baseline remaps

### Conditional profiles

- Active only when their window rules match the focused window
- Applied after permanent profiles
- Good for app-specific or game-specific overlays

## Activation And Merge Rules

Keyforge can have more than one active profile at once.

For each device, Keyforge resolves the final mapping by layering active profiles in this order:

1. Enabled permanent profiles
2. Enabled conditional profiles whose window rules match

Within those two groups, profiles are applied in ascending:

1. `priority`
2. `created_at`
3. profile name, case-insensitive

The last applied mapping wins. In practice:

- higher `priority` overrides lower `priority`
- if priorities are equal, newer `created_at` overrides older
- if both are equal, name order is the tiebreaker

Conditional profiles always override permanent profiles, even if the permanent profile has a higher numeric priority.

## Unmapped Buttons and Overrides

Buttons not listed in a device layer pass through unchanged.

If a higher-priority profile explicitly sets a button to `passthrough`, that
cancels any remap from a lower-priority profile for that button. This is useful
when a conditional profile needs to restore a button's original behavior that a
permanent profile normally remaps.

## Exclusive Input Capture

`always_grab_all` is a per-device layer setting that makes Keyforge capture all
input from the device, even buttons that are not remapped. This prevents the
original input from reaching your apps.

For a given device, this is enabled if any active layer for that device sets it
to `true`.

## Window Rules

Conditional profiles use window rules to decide when to activate. All rules in
a profile must match for it to become active.

Typical fields are:

- `class` — the application identifier your compositor assigns (e.g.
  `steam_app_730`, `firefox`). The GUI fills this in automatically when you
  pick a window.
- `title` — the window title text.
- `tag` — the workspace or tag name (compositor-dependent).

## TOML Format

Each profile file contains:

- one `[profile]` section for global profile metadata
- one `[devices."<hardware_id>"]` section per device layer (the hardware ID
  identifies your device — shown in the GUI device tab header, e.g. `1234:5678`)
- one `[devices."<hardware_id>".mapping.<button_id>]` section per mapped button

Example:

```toml
[profile]
name = "Gaming"
enabled = true
is_permanent = false
priority = 50
notify_on_activation = true
created_at = "2026-03-09T12:34:56"

[[profile.window_rules]]
field = "class"
pattern = "steam_app_730"

[devices."1234:5678"]
always_grab_all = false

[devices."1234:5678".mapping.btn_back]
action = "keyboard"
target = "key_1"

[devices."1234:5678".mapping.btn_forward]
action = "keyboard"
target = "key_2"

[devices."abcd:ef01"]
always_grab_all = true

[devices."abcd:ef01".mapping.key_f13]
action = "exec"
cmd = "playerctl play-pause"
```

## Common Patterns

### Base profile plus app overlay

- Create a permanent `Base` profile for your normal remaps
- Create a conditional `Gaming` profile with window rules for the game
- Put only the buttons you want to change in `Gaming`

When the game window is focused, `Gaming` overlays `Base`.

### One profile across multiple devices

Use one profile when a workflow spans several devices. Example:

- mouse side buttons for push-to-talk and weapon swap
- keyboard function keys for overlays
- keypad buttons for OBS or Discord

All of those can live in one `Streaming` profile.

### Explicitly removing a base mapping

If `Base` maps `btn_extra` to `key_m`, a higher conditional profile can restore normal behavior:

```toml
[devices."1234:5678".mapping.btn_extra]
action = "passthrough"
```

## GUI Behavior

In the GUI:

- profiles are global
- each device tab edits that device's layer inside the selected profile
- enabling or disabling a profile affects every device layer in that profile
- active-state displays show the active profiles contributing to that device
- button mapping offers both `Explicit Passthrough` and `No Override`
- `Explicit Passthrough` stores `action = "passthrough"` and masks lower-profile remaps
- `No Override` removes the mapping from the current profile so lower profiles can still apply one
- deleting a button from the device tab removes it from the hardware config and clears saved mappings for that button across profiles

Deleting a hardware definition does not delete global profiles. Any layers for that hardware remain in the profile file and stay dormant until that hardware exists again.

## CLI

Profile commands operate on profile names:

```bash
keyforge profiles list
keyforge profiles enable Gaming
keyforge profiles disable Gaming
keyforge profiles toggle Gaming
```

`list` shows:

- global profile metadata
- which devices each profile has layers for
- active profiles per device

## Profile Actions

Profile control actions inside mappings now target only a profile name:

- enable profile
- disable profile
- toggle profile

Toggling a profile affects all device layers contained in that profile.

## Combos

Profiles can also contain combo definitions.

Combos follow the same active-profile ordering rules as mappings, but runtime prefix conflicts are resolved by the combo matcher, not by the GUI.

See `docs/COMBOS.md` for combo behavior, timeouts, storage, and shadowing rules.
