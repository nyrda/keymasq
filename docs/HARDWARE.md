# Hardware configuration

A hardware definition describes the physical device Keymasq can grab and the
source IDs that profiles map. The two stay separate on purpose. Hardware says
*where* a source ID comes from, and a profile says *what to do* with it.

Hardware files live in:

```text
~/.config/keymasq/hardware/<hardware_id>.toml
```

Use the GUI for normal setup. The TOML files are plain text, so you can
inspect them, back them up, and hand-edit them for an advanced fix.

## Mental model

A hardware definition has three parts:

- a hardware ID, used as the profile/config key
- one or more attached evdev event devices
- source controls, which are buttons and keys plus analog inputs like sticks
  and axes

The hardware ID is usually the USB vendor/product pair, such as `046d:c08b`.
When you have two of the same model, the second gets a numbered ID like
`046d:c08b@2`. These IDs are profile keys, so changing one means the matching
profile device layer has to use the new ID too.

Each attached event device has its own `id`, such as `mouse`, `kbd`, or
`if02_kbd`. Buttons and analog inputs point back at that `id` through their
`source` field, which is how a single hardware ID can gather controls from
several event devices without mixing them up.

## Event device detection

Every attached evdev device has a `path`, and the GUI offers two ways to find
it again after a reboot or reconnect.

### Stable Path

Stable Path detection stores a kernel-provided link such as:

```text
/dev/input/by-id/usb-Example_Device-event-mouse
```

This is the preferred method when Linux exposes a useful `/dev/input/by-id`
link, because it points at one specific interface and is easy to read.

Keymasq checks the opened input device's vendor and product IDs against the
hardware configuration before using it. Another device or controller mode can
reuse a stable path, so the path alone does not establish a match. When the
IDs differ, that configuration waits for its matching device without grabbing
the replacement. This also applies to readers already acquired by masking.

Two hardware configurations with different vendor/product IDs can use the same
stable path. Each connects only when the device at that path reports its IDs.
Add each controller mode separately through hardware setup. The check uses the
input device's IDs, which can differ from those of its USB receiver.

### Product ID

Product ID detection stores a logical Keymasq path instead:

```text
keymasq:046d:c08b
```

That isn't a real file. At runtime, `keymasqd` matches live devices by
vendor/product ID plus interface metadata (type, topology hint, capabilities).
Use it when a device doesn't expose a stable `/dev/input/by-id` link.

If you own two of the same model, set the second one up through the normal Add
Device flow so it gets its own hardware ID. The hardware settings dialog won't
switch an event device to Product ID detection while another definition already
uses that vendor/product pair, since that would also need a fresh hardware ID.

## Hardware Settings in the GUI

Open hardware settings from the gear button in a device tab.

![Hardware settings dialog showing attached event devices](assets/screenshots/keymasq_hardware_settings.png)

The dialog lists the hardware name and ID, every attached event device with its
detection control, and the rename, delete, and add/remove actions.

**Device masking** appears below the hardware name and on the final hardware
setup page. In setup, switches take effect after **Save**. When masking is
requested, the page stays open for confirmation or retry, and **Done** closes
it. Cancelling setup before saving does not change device access. Hardware
Settings applies masking changes immediately, using the same saved choice as
the global Device masking overview. Shared receiver interfaces use one switch.
Connection and masking scope appear under **Hardware → Device details**. Errors
offer **Copy diagnostics** there. The masking switches do not open another dialog.

The optional `[hardware].masking_devices` list stores physical attachment IDs so
Hardware Settings can show previously associated masks while disconnected. It
does not enable masking or grant access. The helper still owns mask policy.
Exact device paths and physical connection identifiers take precedence over model
IDs. Ambiguous model-only configurations do not select an arbitrary controller.
The live input's vendor and product IDs must also match the configuration, so a
reused path in another controller mode does not add that mode's masking switch.
See [Device masking](HARDWARE_MASKING.md) for scope and recovery behavior.

Clicking the identity row (or `Rename`) opens the same rename dialog as the
device tab. Renaming only changes the display name. Hardware IDs, mappings, and
device identity stay the same. `Delete Hardware` uses the normal delete flow and
leaves your global profiles alone unless you also choose to remove the matching
profile layers.

## Adding and removing event devices

`Add Event Device` attaches another raw evdev device to the same hardware ID
through the Add Device dialog in raw evdev mode. Use this for hardware that
exposes more than one interface, like a mouse with an extra keyboard endpoint.
Selecting a controller motion interface also adds its gyroscope and
accelerometer axes to the hardware layout, so it appears under Motion
Normalization as soon as the hardware is saved.

Each device row has a remove button. Removing a device drops its evdev entry and
the controls whose `source` points at it, including an attached motion sensor.
It can also clear the profile mappings for those controls. Controls from the
other attached devices are untouched.

## Switching detection methods

Each event device row has a compact toggle:

```text
Stable | Product
```

![Product ID detection selected for a hardware event device](assets/screenshots/keymasq_hardware_product_id_detection.png)

`Stable` uses the `/dev/input/by-id` path, and `Product` uses the logical
`keymasq:<vendor_id>:<product_id>` path. If a device is on Product detection and
no stable by-id link is known, `Stable` is greyed out, and the tooltip explains
whether the device is disconnected or simply has no `/dev/input/by-id` link.

Switching saves the hardware file and reloads the session. Existing mappings
keep working because they refer to the hardware ID and source control IDs, never
to the evdev path string.

## Keyboard LEDs and other output feedback

Keymasq creates a dedicated passthrough virtual device for each grabbed evdev
interface. The passthrough device keeps the source interface's LED and sound
capabilities. Keymasq forwards output events written to that virtual interface
only to its corresponding physical interface, so applications can still
control device indicators and bells.

The shared synthetic keyboard does not advertise LED or sound capabilities.
Synthetic mouse and gamepad outputs do not advertise force feedback. Force
feedback is available only through a passthrough clone backed by compatible
physical hardware.

## TOML reference

```toml
[hardware]
name = "Logitech G502 Hero"
vendor_id = "046d"
product_id = "c08b"

[hardware.evdev]
devices = [
  { path = "/dev/input/by-id/usb-Logitech_G502_Hero-event-mouse", id = "mouse", type = "mouse" },
]

[[hardware.layout.buttons]]
id = "btn_back"
label = "Back"
evdev = "btn_side"
source = "mouse"

[[hardware.layout.buttons]]
id = "wheel_up"
label = "Scroll Up"
evdev = "rel_wheel"
evdev_code = 8
evdev_value = 1
type = "wheel"
source = "mouse"

[[hardware.layout.analogs]]
id = "left_stick"
label = "Left Stick"
type = "stick"
source = "joystick"

[[hardware.layout.analogs.axes]]
role = "x"
evdev = "abs_x"
evdev_code = 0
```

`[hardware]`

- `name`: display name
- `vendor_id` / `product_id`: ID strings
- `hardware_id`: optional explicit profile/config key, usually for duplicates
  such as `046d:c08b@2`
- `image`: optional image filename

`[hardware.evdev].devices`

- `path`: `/dev/input/by-id/...`, `/dev/input/by-path/...`,
  `/dev/input/eventN`, or logical `keymasq:<vendor_id>:<product_id>`
- `id`: source interface ID used by controls
- `type`: `keyboard`, `mouse`, `gamepad`, or `other`
- `phys`: optional kernel physical/topology hint
- `capabilities`: optional capability list used for Product ID matching

`[[hardware.layout.buttons]]`

- `id`: source ID used in profile mappings
- `label`: display label
- `evdev`: evdev name such as `btn_side`, `key_f13`, or `rel_wheel`
- `evdev_code`: optional numeric evdev code
- `evdev_value`: optional value, used for wheel direction
- `source`: optional event device `id`
- `zone`, `row`, `col`, `type`: optional UI/layout metadata

`[[hardware.layout.analogs]]`

- `id`: source ID used in profile mappings
- `label`: display label
- `type`: `stick` or `axis`
- `source`: optional event device `id`
- `axes`: axis definitions with `role`, `evdev`, optional `evdev_code`, and
  optional calibration fields

## Profile interaction

Profiles map hardware source IDs:

```toml
[devices."046d:c08b".mapping.btn_back]
action = "keyboard"
target = "key_1"
```

Here `046d:c08b` is the hardware ID and `btn_back` is the button ID. Event
device paths never appear as profile keys. See [Profiles](PROFILES.md) for
layering and merge behavior.

## Controller output

Choose passthrough or a virtual controller during setup or in Hardware Settings.
Hover over the Controller output group for routing guidance.
In `[hardware]`, `default_output` defaults to `"passthrough"` and accepts virtual
output IDs such as `"virtual-gamepad-1"`. This setting applies across profiles,
even with none active. Virtual routing forwards
matching button and axis codes, scales axis bounds, and drops unsupported inputs.
Mappings override individual inputs. See [Controller output](GAMEPAD.md#default-controller-output).
