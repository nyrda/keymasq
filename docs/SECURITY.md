# Security Model

Keymasq uses a two-broker design:

- `keymasqd`: privileged daemon for evdev/uinput, macro storage, recording, and capture
- `keymasq-session`: per-user broker for GUI/CLI requests, profile logic, and compositor integration

GUI and CLI do not access kernel input devices directly.

## Target Environment

Keymasq is designed for single-user Linux desktops. The default security policy reflects this: open access with optional hardening via ACL and UID allowlists for multi-user systems.

## Connection Chain

The runtime forms a single-connection chain:

    GUI/CLI  -->  keymasq-session  -->  keymasqd

Each link accepts exactly one upstream connection at a time:

- `keymasqd` accepts one `keymasq-session` connection. A second session is rejected while the first is alive.
- `keymasq-session` is the sole bridge between GUI/CLI clients and the daemon.
- GUI and CLI talk only to `keymasq-session`, never directly to `keymasqd`.

This means there is always a single linear path from GUI to hardware. No parallel connections can issue competing privileged commands.

## Trust Boundaries

1. GUI/CLI -> `keymasq-session` over the per-user session socket
2. `keymasq-session` -> `keymasqd` over the daemon socket

Both layers enforce authorization. Session-side checks are not advisory; daemon-side checks remain the final authority.

## Privileged Helper Path Pinning

The GUI recording unlock flow uses `pkexec` to run the `keymasq-record` helper.

- Keymasq does not resolve that helper from `$PATH` during privileged execution
- The helper path is treated as a trusted absolute executable path
- The Polkit rule pins the same executable via `org.freedesktop.policykit.exec.path`
- Package builds may substitute a different absolute path, but the runtime helper path and Polkit path must match exactly

This is intentional. Allowing `$PATH` lookup for the `pkexec` target would weaken the trust boundary by letting environment-dependent command resolution influence which program is executed with elevated privileges.

In practice:

- traditional distro packages use `/usr/bin/keymasq-record`
- Nix/NixOS builds stamp the helper to the package store path
- both remain safe because the elevated path is fixed by the package, not chosen from the caller's environment

## Daemon Single-Owner Model

`keymasqd` allows exactly one active session-side client connection at a time.

- The first accepted daemon client becomes the active owner (`uid`, `pid`, `connection_id`).
- Additional daemon client connections are denied while that owner is alive.
- Ownership is released on disconnect.

This prevents a second local process from concurrently issuing privileged daemon commands while a legitimate session broker is connected.

## Peer Identity and ACL

On each accepted Unix socket connection, Keymasq reads `SO_PEERCRED` (`pid`, `uid`, `gid`).

Authorization is then layered as:

- optional UID allowlists
- session command ACL
- daemon command ACL

This prevents bypass when a local process attempts to talk directly to the daemon socket.

## Recording Guard

Keymasq treats recording and capture features as sensitive because they can observe original input.

- Tier 1: recording and capture commands require an active unlock lease by default
- Unlock is per-user and time-bounded by default
- GUI can keep a runtime unlock lease refreshed while it remains the active owner
- "Don't ask again" GUI flows use a longer-lived lease
- Permanent unlock is an explicit administrative decision outside normal runtime flow

When locked, recording/capture requests fail with `recording_locked`.

If `macro_edit_requires_unlock = true`, macro inspection and edit operations are promoted into the same sensitive class.

## Runtime Unlock Ownership Chain

Runtime unlock refresh is bound to the same GUI process and same socket connection:

1. GUI performs the initial unlock
2. GUI claims a refresh lease from `keymasq-session`
3. GUI periodically refreshes that lease through `keymasq-session`
4. `keymasq-session` forwards refresh to `keymasqd`
5. `keymasqd` extends the runtime lease expiry directly

Ownership is checked at both hops:

- GUI -> session: same GUI process and same session socket connection
- Session -> daemon: same session process and same daemon connection

If the owner process or connection changes, refresh is rejected and the runtime unlock is actively cleared by the next lower layer:

- On normal GUI shutdown, the owner explicitly locks the runtime unlock.
- If the GUI disconnects or crashes, `keymasq-session` clears the runtime unlock for that UID when the last same-UID session client disappears.
- If `keymasq-session` disconnects or crashes, `keymasqd` clears all runtime unlock files before releasing devices.

The runtime TTL remains a bounded fallback, but normal and abnormal disconnect paths now clean up the runtime unlock immediately instead of waiting for expiry.

## Sensitive Command Binding

Sensitive commands are bound to the active recording owner, not just to UID/ACL permission.

Session-side sensitive commands currently include:

- `start_recording`
- `begin_capture`
- `capture_read`
- `end_capture`
- `capture_combo`
- `lock_recording_unlock`

Daemon-side sensitive commands currently include:

- `START_RECORDING`
- `CAPTURE_BEGIN`
- `CAPTURE_READ`
- `CAPTURE_END`
- `CAPTURE_COMBO`
- optionally `MACRO_GET`, `MACRO_CREATE`, `MACRO_UPDATE` when macro editing is guarded

If an owner already exists for that UID, the same process and connection must continue issuing those commands. Otherwise the request is rejected with `sensitive_command_denied`.

## Combo Capture Security Model

Combo recording uses the same guarded original-input path as recording and capture.

- Combo capture is performed by `keymasqd`, not by GUI key events
- It observes raw original-input events from the hardware interfaces associated with the selected profile
- It requires the caller to already hold the active recording owner chain
- `CaptureManager.begin_combo()` additionally requires a one-shot daemon-issued authorization capability
- It is therefore gated by both unlock state and owner identity

This is important because combo capture is effectively a short-lived privileged input observation flow, even though its output is only a compact trigger description.

In practice, this means:

- an unlocked lease alone is not enough if another process owns the sensitive-command chain
- ACL permission alone is not enough if capture is locked
- GUI capture of combos is intentionally tied to the same security model as macro recording

## Compositor Dispatch

Compositor dispatch actions are routed through the active window-listener implementation.

- The action is modeled generically as compositor dispatch
- Dispatch actions can carry an explicit compositor target to avoid cross-compositor overlap
- The active listener must explicitly opt in to dispatch support
- If the listener does not support dispatch, the action is rejected
- No shell fallback is used for compositor dispatch
- KDE Plasma dispatch is restricted to a fixed whitelist of supported KWin actions
- Hyprland dispatch is sent through the Hyprland IPC command socket
- Niri dispatch is sent through the Niri IPC command socket with a fixed allowlist

This keeps compositor-specific control inside the listener boundary instead of treating it as unrestricted command execution.

## Policy File

Security policy path:

`/etc/keymasq/security.toml`

Relevant controls:

- `session_command_acl`
- `daemon_command_acl`
- `session_allowed_uids`
- `daemon_allowed_uids`
- `[macro]`
  - `exec_timeout_max_ms`
- `[gui]`
  - `allow_left_right_click_remap`
- `[recording_guard]`
  - `unlock_required`
  - `macro_edit_requires_unlock`

Empty UID allowlists mean no UID restriction. This is the default and is appropriate for single-user desktops. On multi-user systems, populate `daemon_allowed_uids` and `session_allowed_uids` to restrict access to specific users.

`session_command_acl` applies to the single session-side client class: `client`. The session socket is a same-user endpoint, so Keymasq does not model GUI and CLI as separate enforceable trust classes there.

ACL entries are deny rules only. Supported forms: `!command`, `-command`, or `deny:command`. Commands not explicitly denied are allowed. Positive entries (entries without a deny prefix) are ignored.

`[recording_guard].unlock_required` controls whether sensitive original-input
observation flows require an explicit unlock before they are allowed.

When `unlock_required = true`:

- macro recording requires an unlock
- live key/button capture requires an unlock
- combo capture requires an unlock

This is the recommended packaged default because those features observe raw
input before normal remapping or application delivery.

When `unlock_required = false`, those flows are allowed without the extra unlock
step. This is mainly useful for trusted single-user manual installs and
development setups that do not provide the packaged Polkit-based unlock path.

`[recording_guard].macro_edit_requires_unlock` is separate. It controls whether
macro create/update/get APIs also require an unlock, in addition to live
recording and capture.

`[gui].allow_left_right_click_remap` controls whether the GUI is allowed to edit
left and right click mappings. The default is `false`.

When `allow_left_right_click_remap = false`:

- left click and right click remain blocked in the GUI
- the GUI explains that you must explicitly opt in through `security.toml`

When `allow_left_right_click_remap = true`:

- the GUI allows editing those buttons
- the GUI still shows a warning before opening the remap editor

This setting exists because remapping the primary or secondary click can leave
you without a usable pointer button in the desktop UI.

## Socket Paths

- daemon socket: `/run/keymasq/socket` (mode `0o666`)
- session socket: `/run/user/<uid>/keymasq/session.sock` (mode `0o600`)

The daemon socket is world-accessible because `keymasqd` starts as a system service before any user session exists. Any user's `keymasq-session` must be able to connect and claim ownership. Access control is not enforced at the filesystem level but through the single-owner model: once a session claims the daemon, all other connections are rejected. On multi-user systems, use `daemon_allowed_uids` to restrict which UIDs may connect.

The session socket is restricted to the owning user via `XDG_RUNTIME_DIR` permissions and explicit `0o700` on the socket directory.

Socket permissions only gate connection attempts. Actual command authority still depends on peer identity, ACL, unlock state, and owner binding.

## Security Goal

Default operation should avoid repeated auth prompts while still protecting privileged input-observation flows.

The effective security model is:

- peer credential checks on every socket connection
- layered ACL enforcement at session and daemon boundaries
- owner-bound runtime unlock refresh
- owner-bound sensitive command execution
- recording guard for original-input observation features
- listener-scoped compositor dispatch instead of shell execution

## Build Attestations

GitHub releases include build attestations for published artifacts (`.deb`,
`.rpm`, `SHA256SUMS`). These are signed statements from GitHub Actions proving
the artifact was produced by the Keymasq release workflow.

With the GitHub CLI, verify an artifact:

```bash
gh attestation verify ./keymasq_*_all.deb -R nyrda/keymasq
gh attestation verify ./keymasq-*.fedora.*.rpm -R nyrda/keymasq
gh attestation verify ./SHA256SUMS -R nyrda/keymasq
```

This is useful if you want to verify the build chain, not just the checksum.
Most users installing from the package repository do not need this—repository
packages are already signed.

## Diagnostics Mode

Keymasqd includes an optional diagnostics mode for internal latency measurement.

- Enable: `keymasq diagnostics on --interval 5`
- Disable: `keymasq diagnostics off`

When enabled, keymasqd logs periodic latency stats (`p50`, `p95`, `p99`, `max`) for internal event buckets.

View logs with:

`journalctl -u keymasqd -f`
