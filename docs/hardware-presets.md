# Hardware presets

A hardware preset is a TOML file that describes the controls of one device
model. When you add a device that matches a preset, Keymasq uses it to name the
controls and to group the axes correctly. Without a preset, setup works from
the generic codes the kernel driver reports, and some drivers reuse those codes
for controls they were not meant for.

A hardware preset is used once, while the device is added. The result is an
ordinary [hardware config](hardware.md) that you can edit afterwards, with no
ongoing link to the preset file.

Hardware presets are different from [profile presets](presets.md). A hardware
preset describes what the device *has*. A profile preset fills in what its
controls *do*.

Keymasq ships with a bundled **Steam Deck** preset (`steam-deck.toml`). The
kernel driver reports the Deck's trackpads and analog triggers on hat axes, so
generic setup shows them as "Hat 1", "Hat 2", and "Hat 3". The preset turns
them into:

- `left_trackpad` and `right_trackpad`, with the vertical axis corrected
- `left_trigger` and `right_trigger` as two separate axes
- readable names for the back grips (L4, L5, R4, R5), the trackpad clicks, and
  the Steam, View, Menu, and Quick Access buttons

## Using a hardware preset

1. Open **Add New Device** and select the device.
2. On the next page, a **Hardware preset** dropdown appears when a preset
   matches the device model and the selected **Configure as** type. The
   matching preset is selected by default.
3. Click **Save**.

Choose **None, use detected controls** in the dropdown to set the device up
without a preset.

The dropdown is not shown for devices without a matching preset. They are set
up from the detected controls as before.

To use a preset on a device you have already added, delete the hardware and
add it again. Control IDs can change, for example from `hat_0` to
`left_trackpad`, so profile mappings on the old IDs need to be assigned again.

## What a preset changes

A preset is applied on top of the detected controls:

- A button listed in the preset gets the preset's label. Button IDs do not
  change.
- An analog input listed in the preset is built from the detected axes it
  names. Those axes are taken out of the inputs that setup had put them in.
- An axis left over from a split stick becomes its own single-axis input,
  named after its evdev code.
- Detected controls that the preset does not mention are kept unchanged.
- Axis ranges come from the device unless the preset sets them.

Preset controls that the connected device does not report are skipped. An
analog input is also skipped when its ID is already used by another control.

## Preset file format

```toml
[preset]
name = "Steam Deck"
description = "Names the Steam Deck controls and sets up both trackpads."
hardware = ["28de:1205"]
device_types = ["gamepad"]

[buttons.btn_gripl]
label = "L4"

[[analogs]]
id = "left_trackpad"
label = "Left Trackpad"
type = "stick"
axes = [
    { role = "x", evdev = "abs_hat0x" },
    { role = "y", evdev = "abs_hat0y", invert = true },
]

[[analogs]]
id = "left_trigger"
label = "L2"
type = "axis"
axes = [{ role = "x", evdev = "abs_hat2y" }]
```

A preset must define at least one button or analog input.

### `[preset]` fields

| Field | Required | Description |
| --- | --- | --- |
| `hardware` | Yes | List of `vendor:product` model IDs such as `["28de:1205"]`. Matching is case-insensitive. |
| `name` | No | Display name shown in the dropdown. Defaults to the filename stem. |
| `description` | No | Short summary shown under the dropdown. |
| `device_types` | No | List of device types the preset is for, such as `["gamepad"]`, `["mouse"]`, or `["keyboard"]`. The preset is offered when the selected **Configure as** type includes one of them. When omitted, it is offered for every type. |

### `[buttons.<evdev>]` tables

The table key is the button's evdev name, such as `btn_gripl` or `btn_base`.
This is the `evdev` value of the button in the hardware config.

| Field | Required | Description |
| --- | --- | --- |
| `label` | Yes | Label shown for the button on the device tab. |

### `[[analogs]]` tables

| Field | Required | Description |
| --- | --- | --- |
| `id` | Yes | Source ID used in profile mappings. Lowercase letters, digits, and underscores. |
| `type` | Yes | `stick` for a two-axis input or `axis` for a single axis such as a trigger. |
| `label` | No | Display label. Defaults to a title made from the ID. |
| `axes` | Yes | For a `stick`, one axis with `role = "x"` followed by one with `role = "y"`. For an `axis`, one axis with `role = "x"`. |

Each axis accepts:

| Field | Required | Description |
| --- | --- | --- |
| `role` | Yes | `x` or `y`. |
| `evdev` | Yes | Evdev name of the detected axis, such as `abs_hat0x`. |
| `invert` | No | Reverses the axis. Supported on stick axes only. |
| `minimum`, `maximum` | No | Raw range override. Defaults to the range the device reports. |
| `center` | No | Raw center of a stick axis. Defaults to the middle of the range. |
| `rest` | No | Raw rest position of a single axis. When omitted, Keymasq reads it when the device is grabbed. |

An axis can belong to only one analog input in a preset. All axes of an input
must come from the same event device.

Use the IDs `left_stick`, `right_stick`, `left_trigger`, and `right_trigger`
for the controls they describe. Gamepad output uses these IDs to choose the
matching virtual gamepad axis, and the bundled profile presets map them.

## Search folders and priority

Keymasq loads all `*.toml` files from the following directories, from lowest to
highest priority:

1. Bundled hardware presets shipped inside the Keymasq package
2. `<dir>/keymasq/hardware_presets` for each absolute entry in `XDG_DATA_DIRS`
   (evaluated from last to first so earlier entries take priority, defaulting
   to `/usr/local/share:/usr/share`)
3. `~/.config/keymasq/hardware_presets` (or
   `$XDG_CONFIG_HOME/keymasq/hardware_presets`)

When two folders contain a file with the same filename stem, the higher
priority folder wins. This lets you override a bundled preset by placing a file
with the same name in `~/.config/keymasq/hardware_presets`. Invalid files are
logged and skipped without hiding valid presets.

**Refresh** in the Add New Device dialog reloads the preset files.

## Current limits

- A preset relabels detected buttons. It cannot add buttons the device does not
  report or change button IDs.
- Motion sensors and native input sources are not described by presets.
- A preset cannot be applied to hardware that is already configured.
