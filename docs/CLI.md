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
keymasq type "<shortcut:ctrl+l><delete>query<enter>"
keymasq type "<down:5><enter>"
keymasq type "<move:420:180><click>"
keymasq type "<click:420:180><doubleclick>"
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
| `<space>` | Press Space |
| `<esc>` | Press Escape |
| `<backspace>` | Press Backspace |
| `<delete>` | Press Delete |
| `<up>` / `<down>` / `<left>` / `<right>` | Press an arrow key |
| `<home>` / `<end>` | Press Home or End |
| `<pageup>` / `<pagedown>` | Press Page Up or Page Down |
| `<KEY:COUNT>` | Repeat a named key control; for example, `<tab:3>` or `<down:5>` |
| `<shortcut:MOD+KEY>` | Press a keyboard shortcut; for example, `<shortcut:ctrl+l>` or `<shortcut:ctrl+shift+v>` |
| `<move:X:Y>` | Move the pointer to absolute coordinates using the fast natural-move defaults |
| `<click>` / `<lclick>` / `<leftclick>` | Left click |
| `<rclick>` / `<rightclick>` | Right click |
| `<doubleclick>` | Double left click |
| `<click:X:Y>` / `<rclick:X:Y>` / `<doubleclick:X:Y>` | Move to absolute coordinates, then click |
| `<settle>` | Wait 300 ms |
| `<wait:MS>` | Wait a fixed number of milliseconds |
| `<wait:MIN:MAX>` | Wait a random number of milliseconds in the inclusive range |

Shortcut modifiers are `ctrl`, `shift`, `alt`, and `super`, with `control`,
`meta`, and `win` accepted as modifier aliases.

`<move:X:Y>` uses the same natural cursor movement as the compact `play`
`move:X:Y` token with the fast defaults: `100000` px/s, zero jitter, linear
curve, `2` px tolerance, `3000` ms timeout, and `stop_on_failure=false`. The
type syntax does not expose tuning arguments for this control.

Use `\<` to type a literal `<`. Backslashes are otherwise treated as normal
text, so `\\<tab>` types `\<tab>`.

### play

Compile compact event tokens into a temporary macro and play it immediately.

```bash
keymasq play key_a
keymasq play key_leftctrl:1 wait:20 key_c wait:20 key_leftctrl:0
keymasq play move:100:200 wait:10 btn_left
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
| `move:X:Y` | Move the pointer to absolute coordinates using realtime cursor feedback |
| `move:X:Y:SPEED[:JITTER[:CURVE[:TOLERANCE[:MAX_DURATION_MS[:STOP_ON_FAILURE]]]]]` | Natural move with tuning options; `SPEED` is pixels per second, so `100000` is 100 kpx/s |
| `wait:MS` | Wait a fixed number of milliseconds |
| `wait:MIN:MAX` | Wait a random number of milliseconds in the inclusive range |

`move_nat`, `move_natural`, and `move_natural_abs` are accepted as aliases for
`move`. The default compact natural move is `100000` px/s, zero jitter, linear
curve, `2` px tolerance, `3000` ms timeout, and `stop_on_failure=false`. If you
provide a `SPEED` lower than `100000` and omit `CURVE`, the curve defaults to
`natural`; otherwise the omitted curve defaults to `linear`.

Natural movement uses the same runtime as `mouse_move_natural_abs` profile and
macro actions, and macro playback waits for the move to finish before later
events run. `move_abs` remains available as the compatibility absolute-move
token; it is not silently rewritten because existing scripts may rely on its
immediate behavior. Use `key_move` if you need the Linux `KEY_MOVE` key token.

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
event schema. Pointer moves compiled from `move_abs`, `move_rel`, and
`move` are semantic
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
