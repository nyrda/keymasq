# CLI Reference

The `keymasq` command-line interface provides quick access to status, macros,
profiles, and diagnostics without opening the GUI.

## Global Options

```
keymasq [--json] [--version] <command>
```

| Option | Description |
|---|---|
| `--json` | Print raw session response as JSON |
| `--version` | Show version number |
| `-h, --help` | Show help |

## Commands

### status

Show Keymasq runtime status.

```bash
keymasq status
keymasq --json status
```

### type

Compile text into a temporary keyboard macro and play it immediately.

```bash
keymasq type "hello"
echo "hello" | keymasq type
keymasq type --speed 1.5 "hello"
keymasq type "café"
keymasq type "user<tab>password<enter>"
keymasq type "user<tab><wait:100:250>password<enter>"
keymasq type --no-unicode "café"
keymasq type --print-json "hello"
```

| Option | Description |
|---|---|
| `--down-ms MS` | Key down duration for each typed key. Use `0` for no hold delay. Default: `10` |
| `--pause-ms MS` | Pause between typed characters. Use `0` for no inter-key delay. Default: `20` |
| `--speed SPEED` | Playback speed multiplier for event timestamps. Explicit wait controls keep their wall-clock duration |
| `--no-unicode` | Fail on unsupported characters instead of using Linux Ctrl+Shift+U input |
| `--print-json` | Print the compiled macro JSON instead of playing it |

When no text argument is given, `type` reads the full text from stdin. By
default, unsupported characters fall back to Linux Unicode input
(`Ctrl+Shift+U`). Use `--no-unicode` when you want direct key events only.

The type compiler supports a small set of inline controls:

| Control | Description |
|---|---|
| `<tab>` | Press Tab |
| `<enter>` | Press Enter |
| `<wait:MS>` | Wait a fixed number of milliseconds |
| `<wait:MIN:MAX>` | Wait a random number of milliseconds in the inclusive range |

Use `\<` to type a literal `<`. Backslashes are otherwise treated as normal
text, so `\\<tab>` types `\<tab>`.

### play

Compile compact event tokens into a temporary macro and play it immediately.

```bash
keymasq play key_a
keymasq play key_leftctrl:1 wait:20 key_c wait:20 key_leftctrl:0
keymasq play move_abs:100:200 wait:10 btn_left
keymasq play key_a wait:10:20 key_b
keymasq play --speed 0.5 key_a wait:20 key_b
keymasq play --print-json key_a wait:20 key_b
```

The compact grammar uses `:` for all parameters:

| Token | Description |
|---|---|
| `key_a` | Tap a key: down then up |
| `key_a:1` / `key_a:down` | Press and hold a key |
| `key_a:0` / `key_a:up` | Release a held key |
| `btn_left` | Click a button: down then up |
| `btn_left:1` / `btn_left:down` | Press and hold a button |
| `btn_left:0` / `btn_left:up` | Release a held button |
| `move_abs:X:Y` | Move the pointer to absolute coordinates |
| `move_rel:DX:DY` | Move the pointer relative to the current position |
| `wait:MS` | Wait a fixed number of milliseconds |
| `wait:MIN:MAX` | Wait a random number of milliseconds in the inclusive range |

The compact compiler automatically releases any held keys/buttons at the end of
the macro, in reverse press order. It rejects duplicate explicit presses and
releases without matching presses.

For full macro support, pass canonical macro JSON:

```bash
keymasq play --json '[{"device_type":"keyboard","type":1,"code":30,"value":1,"t_us":0}]'
cat macro.json | keymasq play --json
keymasq --json play --json < macro.json
```

`play --json` accepts either an event list or a macro object with an `events`
field. JSON playback uses the existing macro runtime and supports the full macro
event schema. Pointer moves compiled from `move_abs` and `move_rel` are semantic
macro actions, for example `{"macro_action":"mouse_move_abs","x":100,"y":200}`.
The compact token grammar intentionally does not support `exec`.

| Option | Description |
|---|---|
| `--json` | Read macro JSON instead of compact event tokens |
| `--speed SPEED` | Playback speed multiplier for event timestamps. Explicit wait controls keep their wall-clock duration |
| `--print-json` | Print the compiled macro JSON instead of playing it |

When no compact tokens or JSON payload are given, `play` reads from stdin.

### profiles

Manage profile state.

```bash
keymasq profiles list
keymasq profiles enable <profile_name>
keymasq profiles disable <profile_name>
keymasq profiles toggle <profile_name>
```

| Subcommand | Description |
|---|---|
| `list` | Show devices and profiles overview |
| `enable <name>` | Enable a profile |
| `disable <name>` | Disable a profile |
| `toggle <name>` | Toggle profile enabled state |

### macros

Control macro playback.

```bash
keymasq macros list
keymasq macros create <name> [--force] [JSON]
keymasq macros play <name> [--speed SPEED]
keymasq macros delete <name>
keymasq macros cancel
```

| Subcommand | Description |
|---|---|
| `list` | List available macros |
| `create <name>` | Create a stored macro from JSON |
| `play <name>` | Play a macro by name |
| `delete <name>` | Delete a stored macro |
| `cancel` | Cancel all running macro playback |

**Options for `play`:**

| Option | Description |
|---|---|
| `--speed SPEED` | Playback speed multiplier for event timestamps; explicit wait controls are not scaled |

**Options for `create`:**

| Option | Description |
|---|---|
| `-f, --force` | Overwrite an existing macro by updating it |

`macros create` reads macro JSON from stdin when no JSON argument is provided.
It accepts either a macro object with an `events` field or a raw event list. The
CLI-provided name is always used for the stored macro. This JSON is the CLI
interchange format; stored macros are written by keymasqd as compressed
`.kmacro.xz` files.

```bash
keymasq type "test123üäß<tab><wait:20>12345<tab><wait:20>" --print-json \
  | keymasq macros create type_stuff

keymasq play key_a wait:20 key_b --print-json \
  | keymasq macros create demo_sequence --force
```

### diagnostics

Toggle keymasqd latency diagnostics.

```bash
keymasq diagnostics on [--interval SECONDS] [--include CATEGORY] [--exclude CATEGORY]
keymasq diagnostics off
```

When enabled, keymasqd logs periodic latency percentiles (p50, p95, p99, max).
View with:

```bash
journalctl -u keymasqd -f
```

| Option | Description |
|---|---|
| `--interval SECONDS` | Logging interval in seconds |
| `--include CATEGORY` | Add a diagnostics category: `mainline`, `combo`, `internal`, or `all` |
| `--exclude CATEGORY` | Hide a diagnostics category after includes are applied: `mainline`, `combo`, or `internal` |

The default category is `mainline`, which shows the normal passthrough and
remap-action paths. Use `--include combo` for combo-specific timing and
`--include internal` for low-level daemon details.
