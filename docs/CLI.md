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
keymasq macros play <name> [--speed SPEED]
keymasq macros cancel
```

| Subcommand | Description |
|---|---|
| `list` | List available macros |
| `play <name>` | Play a macro by name |
| `cancel` | Cancel all running macro playback |

**Options for `play`:**

| Option | Description |
|---|---|
| `--speed SPEED` | Playback speed multiplier |

### diagnostics

Toggle keymasqd latency diagnostics.

```bash
keymasq diagnostics on [--interval SECONDS]
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
