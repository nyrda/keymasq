# Security Model

## How Keymasq Works Around Wayland Restrictions

Wayland intentionally prevents applications from reading or injecting input
across windows. Keymasq bypasses this by operating at the kernel level using
evdev and uinput — the same layer where compositors themselves read input.

A privileged system daemon (`keymasqd`) reads from `/dev/input/*` devices and
writes to a virtual device via `/dev/uinput`. This happens below Wayland, so
the compositor sees Keymasq's output as normal hardware input.

**Why this is still safe:**

- The daemon runs as a dedicated `keymasq` system user, not root
- GUI and CLI never touch input devices directly — they talk to a per-user
  session broker, which talks to the daemon
- Macro recording is disabled until the user opts in through the Polkit-backed
  helper; capture features require an explicit Polkit unlock
- The daemon accepts only one session connection at a time, preventing rogue
  processes from issuing commands

The rest of this document covers the security model in detail.

---

## Architecture

Keymasq uses a two-broker design:

- `keymasqd`: privileged daemon for evdev/uinput, macro storage, recording, and capture
- `keymasq-session`: per-user broker for GUI/CLI requests, profile logic, and compositor integration

GUI and CLI do not access kernel input devices directly.

## Target Environment

Keymasq is designed for single-user Linux desktops. The default security policy reflects this: open access with optional UID allowlists for multi-user systems.

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

The GUI capture unlock flow uses `pkexec` to run the `keymasq-record` helper.

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

- The first daemon client connection that passes peer validation becomes the
  active owner, identified by (`uid`, `pid`, `connection_id`).
- Additional daemon client connections are denied while that owner is alive.
  They are closed immediately at accept time, before any command is processed.
- Ownership is released only when the owning connection disconnects. There is
  no takeover, transfer, or preemption path.

This prevents a second local process from concurrently issuing privileged daemon commands while a legitimate session broker is connected.

### First-valid-session ownership is intentional

Keymasq targets single-seat desktops, and first-valid-session ownership is the
deliberate design for that target — not a placeholder for something smarter:

- Ownership is **not** bound to an "installing user". Package installation
  cannot identify a reliable desktop owner: installs commonly run as root,
  through configuration-management automation, inside an image build, or as
  declarative NixOS configuration. None of those contexts name the human who
  will sit at the machine, and several produce systems with no such user at
  install time at all.
- Ownership does **not** infer an "active seat". Seat/session inference
  (logind seats, active-VT tracking, greeter sessions) is brittle across
  display managers, fast user switching, and headless or nested sessions, and
  would turn ownership into a moving target.

On a single-seat desktop, the first allowed `keymasq-session` to connect is the
logged-in user's broker, which is exactly the process that should own the
daemon. For unusual shared-system installations, `daemon_allowed_uids` in
`/etc/keymasq/security.toml` is the explicit control: it restricts which UIDs
may connect at all, so ownership can only ever be claimed by a listed user.
This remains the supported mechanism; do not rely on install-time or
seat-inference behavior that Keymasq intentionally does not have.

### Ownership lifecycle and cleanup

When the owning connection disconnects — clean shutdown, crash, or daemon
restart of `keymasq-session` — `keymasqd` runs disconnect cleanup before the
next client can claim ownership:

1. The runtime capture unlock held by that owner is cleared.
2. All pending (unsaved) recordings are discarded.
3. All grabbed input devices are released, so hardware returns to passthrough.

Ownership release is then logged and the owner slot becomes free. The
per-user `keymasq-session` broker reconnects automatically with exponential
backoff (1 s doubling up to 30 s), reclaims ownership, and reapplies active
profiles. A brief passthrough window between disconnect and reconnection is
expected behavior, not a fault.

### Ownership diagnostics

`keymasqd` logs every ownership transition with the full owner identity
(`uid`, `pid`, `connection`):

```text
Daemon owner claimed uid=1000 pid=1234 connection=1
Denied client uid=1001 pid=5678 connection=2: owner already held by uid=1000 pid=1234 connection=1
Daemon owner released uid=1000 pid=1234 connection=1
```

View them with `journalctl -u keymasqd`. The denial line identifies both the
rejected client and the current owner, which is usually enough to find a stale
or competing `keymasq-session` process. See the ownership section in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) for conflict scenarios such as fast
user switching and stale session processes.

## Daemon Capability and Service Hardening

`keymasqd` runs as the dedicated `keymasq` system user inside a hardened
systemd service, with exactly one Linux capability:

```ini
NoNewPrivileges=true
AmbientCapabilities=CAP_DAC_OVERRIDE
CapabilityBoundingSet=CAP_DAC_OVERRIDE
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/run/keymasq /var/lib/keymasq
```

`CAP_DAC_OVERRIDE` is an accepted, deliberate part of the trusted runtime —
not leftover privilege. It exists for the physical-source management and
force-feedback passthrough design, and these are the exact operations that
require it:

- **sysfs uevent writes for source hide/restore.** Hiding or restoring a
  grabbed physical source runs `udevadm trigger`, which writes root-owned
  `/sys/.../uevent` files so `99-keymasq-hide-grabbed.rules` re-evaluates the
  node. The daemon is the unprivileged `keymasq` user, so the `udevadm` child
  relies on the inherited ambient capability. This covers per-node hide and
  restore, hardware-level hotplug hiding, and the startup/shutdown
  reconciliation pass (`keymasq/keymasqd/runtime/source_hiding.py`).
- **Device permission resets on hidden nodes.** The hide rules deliberately
  reset hidden `event*`/`js*` nodes to `root:root` mode `0600`, strip ACLs,
  and then re-grant the `keymasq` ACL. The capability guarantees the daemon
  keeps opening and writing those nodes across that reset, including the
  window before the udev `RUN` ACL re-grant lands on a freshly hidden or
  hotplugged node.
- **Force-feedback passthrough.** Rumble proxying writes `EV_FF` uploads,
  erases, and play events to the grabbed physical gamepad node
  (`keymasq/keymasqd/runtime/force_feedback.py`) — which is exactly such a
  hidden, permission-reset node while a game is running.

What does **not** rely on the capability: normal input device reads and
`/dev/uinput` access are granted through explicit ACLs (`setfacl` in
`ExecStartPre` and `91-keymasq-acl.rules`), and the daemon's writable state
lives in its own `RuntimeDirectory`/`StateDirectory`. Failure messages are
distinct per mechanism, so a missing input ACL, missing uinput access, and a
missing capability are directly distinguishable in the logs (see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md)).

The containment around the capability is retained deliberately: dedicated
service user, `NoNewPrivileges`, bounding set limited to this single
capability, protected system and home paths, and writable directories
restricted to `/run/keymasq` and `/var/lib/keymasq`. All maintained package
formats (Debian, RPM, Arch/AUR, AppImage/SteamOS, NixOS module) grant this
same minimal set; none adds any other ambient or bounding capability. The
service files carry matching comments so the grant and its consumers stay in
sync.

A narrower design — delegating the udev trigger and hidden-node access to a
separate root helper so the daemon itself drops the capability — remains a
future option only. It adds IPC surface and failure modes of its own, so it
is not worth adopting unless a concrete security or operational problem with
the current single-capability model justifies that complexity.

## Peer Identity and ACL

On each accepted Unix socket connection, Keymasq reads `SO_PEERCRED` (`pid`, `uid`, `gid`).

Optional UID allowlists can restrict which local users may connect to the session and daemon sockets.

## Recording Guard

Keymasq treats recording and capture features as sensitive because they can observe original input.

- Macro recording requires a user opt-in recorded by the Polkit-backed
  `keymasq-record` helper; the GUI exposes this under
  **Settings > Macro recording** and allows opting out again
- Recording always writes into one of four explicit temporary slots
- Capture commands require an active unlock lease by default
- Unlock is per-user and time-bounded by default
- GUI can keep a runtime unlock lease refreshed while it remains the active owner
- Permanent unlock is an explicit administrative decision outside normal runtime flow

When macro recording is disabled, recording requests fail with
`macro_recording_disabled`. When capture is locked, capture requests fail with
`recording_locked`.

Temporary macro slots are pending recording handles, not inspectable macro
bodies. They can be replayed only through an explicit slot playback action and
cannot be fetched through the macro body APIs. Slot data is kept in
daemon-private storage so slots survive daemon restarts. Saving a slot copies
it into normal macro storage and leaves the slot in place; deleting or
overwriting a slot removes the pending recording.

Saving a temporary slot into the macro library requires the capture unlock
flow when `unlock_required = true`. This is enforced in the GUI, the
session broker, and the daemon command handler.

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

Sensitive commands are bound to the active recording owner, not just to UID admission.

Session-side sensitive commands currently include:

- `begin_capture`
- `capture_read`
- `end_capture`
- `capture_combo`
- `lock_recording_unlock`

Daemon-side sensitive commands currently include:

- `CAPTURE_BEGIN`
- `CAPTURE_READ`
- `CAPTURE_END`
- `CAPTURE_COMBO`
- optionally `MACRO_GET`, `MACRO_CREATE`, `MACRO_UPDATE` when macro editing is guarded

If an owner already exists for that UID, the same process and connection must continue issuing those commands. Otherwise the request is rejected with `sensitive_command_denied`.

`START_RECORDING` is deliberately not part of the capture unlock owner chain.
It is instead gated by the macro-recording opt-in file. This keeps the macro
recording workflow unified after opt-in while preventing recording from being
an available default attack surface.

## Combo Capture Security Model

Combo recording uses the same guarded original-input path as recording and capture.

- Combo capture is performed by `keymasqd`, not by GUI key events
- It observes raw original-input events from the hardware interfaces associated with the selected profile
- It requires the caller to already hold the active recording owner chain
- `CaptureManager.begin_combo()` additionally requires a one-shot daemon-issued authorization capability
- The daemon clamps each combo capture request to a 15-second maximum duration
- It is therefore gated by both unlock state and owner identity

This is important because combo capture is effectively a short-lived privileged input observation flow, even though its output is only a compact trigger description.

In practice, this means:

- an unlocked lease alone is not enough if another process owns the sensitive-command chain
- GUI capture of combos is intentionally tied to the capture unlock owner chain

## Compositor Dispatch

Compositor dispatch actions are routed through the active window-listener implementation.

- The action is modeled generically as compositor dispatch
- Dispatch actions can carry an explicit compositor target to avoid cross-compositor overlap
- The active listener must explicitly opt in to dispatch support
- If the listener does not support dispatch, the action is rejected
- No shell fallback is used for compositor dispatch
- KDE Plasma dispatch is restricted to a fixed whitelist of supported KWin actions
- Hyprland dispatch is sent through the Hyprland IPC command socket as
  Hyprland 0.55 Lua dispatcher expressions
- Niri dispatch is sent through the Niri IPC command socket with a fixed allowlist
- GNOME dispatch and cursor-position requests are restricted to allowlisted RPCs
  handled by the Keymasq GNOME Shell bridge

This keeps compositor-specific control inside the listener boundary instead of treating it as unrestricted command execution.

## Policy File

Security policy path:

`/etc/keymasq/security.toml`

Relevant controls:

- `session_allowed_uids`
- `daemon_allowed_uids`
- `[macro]`
  - `exec_timeout_max_ms`: maximum `exec_sync` wait time; the daemon clamps macro
    payloads to this limit and the session uses the same value as the subprocess
    timeout, killing the command if it is exceeded.
- `[gui]`
  - `emergency_cancel_combo_enabled`
- `[recording_guard]`
  - `unlock_required`
  - `macro_edit_requires_unlock`

Empty UID allowlists mean no UID restriction. This is the default and is appropriate for single-user desktops. On multi-user systems, populate `daemon_allowed_uids` and `session_allowed_uids` to restrict access to specific users.

`[recording_guard].unlock_required` controls whether sensitive original-input
observation flows require an explicit unlock before they are allowed.

When `unlock_required = true`:

- live key/button capture requires an unlock
- combo capture requires an unlock
- starting the Device Inspector and enabling its suppression mode require an
  unlock

Template-based hardware creation in the GUI does not require unlock. The unlock
gate applies to original-input observation flows, not to writing normal
same-user hardware/profile config files.

This is the recommended packaged default because those features observe raw
input before normal remapping or application delivery.

When `unlock_required = false`, capture and inspector flows are allowed without
the extra unlock step. This is mainly useful for trusted single-user manual
installs and development setups that do not provide the packaged Polkit-based
unlock path. Macro recording still requires its separate opt-in.

`[recording_guard].macro_edit_requires_unlock` is separate. It controls whether
macro create/update/get APIs also require an unlock, in addition to live
recording and capture.

The GUI warns before editing left and right click mappings, and before saving a
single-button, single-step combo that uses left or right click as the trigger.
These warnings exist because remapping the primary or secondary click can remove
that click **everywhere**.

`[gui].emergency_cancel_combo_enabled` controls whether `keymasqd` reserves
`Ctrl+Alt+Esc` on grabbed keyboards as an emergency combo. The default is
`true`.

When `emergency_cancel_combo_enabled = true`:

- the daemon injects `Ctrl+Alt+Esc` into active keyboard combo runtime state
- the GUI rejects attempts to save that exact combo trigger
- tapping the combo cancels all running macro playback directly in `keymasqd`
  after a 200 ms double-tap window
- double-tapping the combo runs a daemon runtime reset, releases all grabbed
  devices, broadcasts `runtime_reset`, and lets the session reapply profiles

When `emergency_cancel_combo_enabled = false`, the daemon does not inject the
combo and the GUI allows it to be assigned like any other combo. Disabling it
is not recommended unless you intentionally need that exact trigger.

## Socket Paths

- daemon socket: `/run/keymasq/socket` (mode `0o666`)
- session socket: `/run/user/<uid>/keymasq/session.sock` (mode `0o600`)

The daemon socket is world-accessible because `keymasqd` starts as a system service before any user session exists. Any user's `keymasq-session` must be able to connect and claim ownership. Access control is not enforced at the filesystem level but through the single-owner model: once a session claims the daemon, all other connections are rejected. On multi-user systems, use `daemon_allowed_uids` to restrict which UIDs may connect.

The session socket is restricted to the owning user via `XDG_RUNTIME_DIR` permissions and explicit `0o700` on the socket directory.

Socket permissions only gate connection attempts. Sensitive command authority still depends on peer identity, unlock state, and owner binding.

## Security Goal

Default operation should avoid repeated auth prompts while still protecting privileged input-observation flows.

The effective security model is:

- peer credential checks on every socket connection
- optional UID admission controls at session and daemon boundaries
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
gh attestation verify ./keymasq-*.fc*.rpm -R nyrda/keymasq
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
