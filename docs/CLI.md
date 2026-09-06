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

Submit text as a temporary keyboard macro. Requests can run concurrently. Use
`--ordered` to serialize with other requests that opt in, and `--wait` to wait
for completion and cancel this request on interruption.

```bash
keymasq type "hello"
echo "hello" | keymasq type
keymasq type --speed 1.5 "hello"
keymasq type "café"
keymasq type "user<tab>password<enter>"
keymasq type "user<tab><wait:100:250>password<enter>"
keymasq type "<shortcut:ctrl+l><delete>query<enter>"
keymasq type "<down:5><enter>"
keymasq type "<move:420:180><click>"
keymasq type "<click:420:180><doubleclick>"
keymasq type --no-unicode "café"
keymasq type --print-json "hello"
```

| Option | Description |
|---|---|
| `--down-ms MS` | Key down duration for each typed key. Use `0` for no hold delay. Default: `5` |
| `--pause-ms MS` | Pause between typed characters. Use `0` for no inter-key delay. Default: `10` |
| `--speed SPEED` | Playback speed multiplier for event timestamps. Explicit wait controls keep their wall-clock duration |
| `--no-unicode` | Fail on unsupported characters instead of using Linux Ctrl+Shift+U input |
| `--ordered` | Serialize with other requests that opt into ordering |
| `--wait` | Wait for completion; SIGINT or SIGTERM cancels this request |
| `--print-json` | Print the compiled macro JSON instead of playing it |

When no text argument is given, `type` reads the full text from stdin. By
default, unsupported characters fall back to Linux Unicode input
(`Ctrl+Shift+U`). Use `--no-unicode` when you want direct key events only.

Type text supports inline controls such as `<tab>`, `<shortcut:ctrl+l>`,
`<move:X:Y>`, `<click:X:Y>`, and `<wait:MS>`. See
[Type Macro Inline Controls](MACROS.md#type-macro-inline-controls) for the
full reference.

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

### mpris

Control tracked MPRIS media players with Keymasq's media policy.

```bash
keymasq mpris play-pause
keymasq mpris play
keymasq mpris pause
keymasq mpris next
keymasq mpris previous
keymasq mpris stop
keymasq mpris status
keymasq mpris status --json
```

| Subcommand | Description |
|---|---|
| `play-pause` | Pause all playing players, or play the latest started inactive player when nothing is playing |
| `play` | Play the latest user-started player, falling back to the latest detected player |
| `pause` | Pause all currently playing players |
| `next` | Send Next to the latest detected player that supports it |
| `previous` | Send Previous to the latest detected player that supports it |
| `stop` | Stop all currently playing players |
| `status` | Show tracked players, playback state, current metadata, capabilities, and action targets |

`mpris status --json` prints the same MPRIS tracking snapshot Keymasq uses for
actions. It includes raw player IDs for debugging, plus playback state, current
track metadata when the player exposes it, capabilities, and routing order.

### macros

Control macro playback.

```bash
keymasq macros list
keymasq macros create <name> [--force] [JSON]
keymasq macros play <name> [--speed SPEED] [--wait] [--ordered]
keymasq macros delete <name>
keymasq macros cancel
```

| Subcommand | Description |
|---|---|
| `list` | List available macros |
| `create <name>` | Create a stored timeline macro from JSON |
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

`macros create` reads canonical macro JSON from stdin when no JSON argument is
provided. It accepts either a macro object with an `events` field or a raw
event list. The CLI-provided name is always used as the stored macro name.
These are normal timeline macros: they can contain the complete event schema
and can be opened and edited in the GUI. Stored macros are written by keymasqd
as compressed `.kmacro.xz` files.

```bash
keymasq type "test123üäß<tab><wait:20>12345<tab><wait:20>" --print-json \
  | keymasq macros create type_stuff

cat macro.json | keymasq macros create imported_macro
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
| `--include CATEGORY` | Add a diagnostics category: `mainline`, `combo`, `macro`, `internal`, or `all` |
| `--exclude CATEGORY` | Hide a diagnostics category after includes are applied: `mainline`, `combo`, `macro`, or `internal` |

The default category is `mainline`, which shows the normal passthrough and
remap-action paths. Use `--include combo` for combo-specific timing and
`--include macro` for stored-macro loading and playback timing, or
`--include internal` for low-level daemon details.

See [Playback requests](PLAYBACK_REQUESTS.md) for completion results, exit codes,
and the session protocol for third-party clients.
