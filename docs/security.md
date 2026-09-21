# Security model

## How Keymasq works around Wayland restrictions

Wayland intentionally prevents applications from reading or injecting input
across windows. Keymasq bypasses this by operating at the kernel level using
evdev and uinput, the same layer where compositors themselves read input.

A privileged system daemon (`keymasqd`) reads from `/dev/input/*` devices and
writes to a virtual device via `/dev/uinput`. This happens below Wayland, so
the compositor sees Keymasq's output as normal hardware input.

**Why this is still safe:**

- The daemon runs as a dedicated `keymasq` system user, not root
- GUI and CLI never touch input devices directly. They talk to a per-user
  session broker, which talks to the daemon
- Macro recording is disabled until the user opts in through the Polkit-backed
  helper, and capture features require an explicit Polkit unlock
- The daemon accepts only one session connection at a time, preventing rogue
  processes from issuing commands

The rest of this document covers the security model in detail.

---

## Architecture

Keymasq uses a two-broker design:

- `keymasqd`: privileged daemon for evdev/uinput, macro storage, recording, and capture
- `keymasq-session`: per-user broker for GUI/CLI requests, profile logic, and compositor integration

GUI and CLI do not access kernel input devices directly.

## Target environment

Keymasq is designed for single-user Linux desktops. The default security policy reflects this, with open access and optional UID allowlists for multi-user systems.

## Connection chain

The runtime forms a single-connection chain:

    GUI/CLI  -->  keymasq-session  -->  keymasqd

Each link accepts exactly one upstream connection at a time:

- `keymasqd` accepts one `keymasq-session` connection. It rejects a second session while the first is alive.
- `keymasq-session` is the sole bridge between GUI/CLI clients and the daemon.
- GUI and CLI talk only to `keymasq-session`, never directly to `keymasqd`.

This means there is always a single linear path from GUI to hardware. No parallel connections can issue competing privileged commands.

## Trust boundaries

1. GUI/CLI → `keymasq-session` over the per-user session socket
2. `keymasq-session` → `keymasqd` over the daemon socket

Both layers enforce authorization. Session-side checks are not advisory, and daemon-side checks remain the final authority.

## Privileged helper path pinning

The GUI capture unlock flow uses `pkexec` to run the `keymasq-record` helper.

- Keymasq does not resolve that helper from `$PATH` during privileged execution
- Keymasq treats the helper path as a trusted absolute executable path
- The Polkit rule pins the same executable via `org.freedesktop.policykit.exec.path`
- Package builds may substitute a different absolute path, but the runtime helper path and Polkit path must match exactly

This is intentional. Allowing `$PATH` lookup for the `pkexec` target would weaken the trust boundary by letting environment-dependent command resolution influence which program runs with elevated privileges.

In practice:

- traditional distro packages use `/usr/bin/keymasq-record`
- Nix/NixOS builds stamp the helper to the package store path
- both remain safe because the elevated path is fixed by the package, not chosen from the caller's environment

## Daemon single-owner model

`keymasqd` allows exactly one active session-side client connection at a time.

- The first daemon client connection that passes peer validation becomes the
  active owner, identified by (`uid`, `pid`, `connection_id`).
- `keymasqd` denies additional daemon client connections while that owner is
  alive. It closes them immediately at accept time, before it processes any
  command.
- `keymasqd` releases ownership only when the owning connection disconnects.
  There is no takeover, transfer, or preemption path.

This prevents a second local process from concurrently issuing privileged daemon commands while a legitimate session broker is connected.

### First-valid-session ownership is intentional

Keymasq targets single-seat desktops, and first-valid-session ownership is the
deliberate design for that target, not a placeholder for something smarter:

- Ownership is **not** bound to an "installing user". Package installation
  cannot identify a reliable desktop owner. Installs commonly run as root,
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
`/etc/keymasq/security.toml` is the explicit control. It restricts which UIDs
may connect at all, so only a listed user can ever claim ownership.
This remains the supported mechanism. Do not rely on install-time or
seat-inference behavior that Keymasq intentionally does not have.

### Ownership lifecycle and cleanup

When the owning connection disconnects, whether through a clean shutdown, a
crash, or a daemon restart of `keymasq-session`, `keymasqd` runs disconnect
cleanup before the next client can claim ownership:

1. It clears the runtime capture unlock held by that owner.
2. It aborts any active recording without producing or persisting a recording.
3. It closes all live capture sessions and revokes unused capture authorizations.
4. It discards all non-slot pending (unsaved) recordings.
5. It releases all grabbed input devices, so hardware returns to passthrough.

`keymasqd` then logs the ownership release, and the owner slot becomes free. The
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
[troubleshooting.md](troubleshooting.md) for conflict scenarios such as fast
user switching and stale session processes.

## Daemon capability and service hardening

`keymasqd` runs as the dedicated `keymasq` system user inside a hardened
systemd service with no Linux capabilities at all:

```ini
NoNewPrivileges=true
CapabilityBoundingSet=
DevicePolicy=closed
DeviceAllow=char-input rw
DeviceAllow=/dev/uinput rw
DeviceAllow=char-hidraw r
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
PrivateTmp=true
ReadWritePaths=/run/keymasq /var/lib/keymasq
```

Everything the daemon touches is reachable through ordinary permissions:

- **Input devices and uinput.** Explicit ACLs (`setfacl` in `ExecStartPre`
  and `91-keymasq-acl.rules`) grant reads of `/dev/input/event*`, writes to
  `/dev/uinput`, and read-only hidraw access. The device
  policy additionally limits the service to input, uinput, and read-only
  hidraw nodes, so the daemon cannot open other users' devices, block
  devices, or anything else under `/dev` even if a rule or ACL is
  misconfigured. Its writable state lives in its own
  `RuntimeDirectory`/`StateDirectory`.
- **Source hide/restore.** Hiding a grabbed gamepad source writes a flag file
  under the daemon's own `/run/keymasq/hidden` directory. Making udev act on
  that flag needs `udevadm trigger`, which writes root-owned `/sys/.../uevent`
  files. The daemon requests that trigger as a bounded `keymasq-hardware@`
  root job (see Hardware masking jobs below). The job validates the `event*`
  and `js*` names, checks them against `/sys/class/input`, and runs the
  trigger. Per-node hide and restore, model-wide hotplug hiding, and the
  startup reconcile all use this path
  (`keymasq/keymasqd/runtime/source_hiding.py`). `ProtectKernelTunables`
  keeps sysfs read-only inside the daemon itself. No input waits on these
  jobs. A grabbed source is already being read when its hide job is requested,
  and a release closes every physical handle before any restore job runs.
- **Permission resets on hidden nodes.** The hide rules reset hidden
  `event*`/`js*` nodes to `root:root` mode `0600`, strip ACLs, and re-grant
  the `keymasq` ACL within the same udev event. The daemon opens and grabs a
  source before it asks to hide it and keeps that handle for the lifetime of
  the grab, so the reset never affects a grabbed node. Force-feedback
  passthrough (`keymasq/keymasqd/runtime/force_feedback.py`) writes `EV_FF`
  uploads, erases, and play events through that same handle. A node that
  udev has not finished processing yet, such as a reconnecting controller,
  can briefly refuse the open. Grabs retry on `EACCES` for a short bounded
  period instead of relying on a capability.

Failure messages are distinct per mechanism, so a missing input ACL, missing
uinput access, and a failing hide job are directly distinguishable in the
logs (see [troubleshooting.md](troubleshooting.md)).

Native motion drivers also use explicit ACLs in `91-keymasq-acl.rules`.
The daemon user gets read access to hidraw nodes, including devices with no
registered driver. The registry controls which endpoints Keymasq actually
opens. It is not a permission boundary. The rule does not grant write access
or change device ownership. The Ultimate 2 driver opens its node read-only
and sends no commands. Future drivers that write need an explicit access policy.

The containment around the daemon is retained deliberately: dedicated
service user, `NoNewPrivileges`, an empty capability bounding set, a closed
device policy, protected system and home paths, read-only kernel tunables,
and writable directories restricted to `/run/keymasq` and `/var/lib/keymasq`.
All maintained package formats (Debian, RPM, Arch/AUR, AppImage/SteamOS,
NixOS module) ship this same capability-free unit, and none adds an ambient or
bounding capability. The service files carry matching comments so the unit
and the code that depends on it stay in sync.

Delegating the udev trigger to a root job adds IPC surface and failure modes
of its own. They are bounded. The daemon supplies only `event*`/`js*` kernel
names, the job validates them again as root, and a failed or timed-out job is
logged distinctly while remapping keeps working.

## Peer identity and ACL

On each accepted Unix socket connection, Keymasq reads `SO_PEERCRED` (`pid`, `uid`, `gid`).

Optional UID allowlists can restrict which local users may connect to the session and daemon sockets.

## Recording guard

Keymasq treats recording and capture features as sensitive because they can observe original input.

- Macro recording requires a user opt-in recorded by the Polkit-backed
  `keymasq-record` helper. The GUI exposes this under
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
it into normal macro storage and leaves the slot in place. Deleting or
overwriting a slot removes the pending recording.

Saving a temporary slot into the macro library requires the capture unlock
flow when `unlock_required = true`. The GUI, the session broker, and the
daemon command handler all enforce this.

If `macro_edit_requires_unlock = true`, macro inspection and every persistent
edit operation (create, update, rename, and delete) are promoted into the same
sensitive class.

## Runtime unlock ownership chain

Runtime unlock refresh is bound to the same GUI process and same socket connection:

1. GUI performs the initial unlock
2. GUI claims a refresh lease from `keymasq-session`
3. GUI periodically refreshes that lease through `keymasq-session`
4. `keymasq-session` forwards refresh to `keymasqd`
5. `keymasqd` extends the runtime lease expiry directly

Ownership is checked at both hops:

- GUI → session: same GUI process and same session socket connection
- Session → daemon: same session process and same daemon connection

If the owner process or connection changes, refresh is rejected and the next lower layer actively clears the runtime unlock:

- On normal GUI shutdown, the owner explicitly locks the runtime unlock.
- If the GUI disconnects or crashes, `keymasq-session` clears the runtime unlock for that UID when the last same-UID session client disappears.
- If `keymasq-session` disconnects or crashes, `keymasqd` clears all runtime unlock files before releasing devices.

The runtime TTL remains a bounded fallback, but normal and abnormal disconnect paths now clean up the runtime unlock immediately instead of waiting for expiry.

## Sensitive command binding

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
- optionally `MACRO_GET`, `MACRO_CREATE`, `MACRO_UPDATE`, `MACRO_RENAME`, and
  `MACRO_DELETE` when macro editing is guarded

If an owner already exists for that UID, the same process and connection must continue issuing those commands. Otherwise the request is rejected with `sensitive_command_denied`.

`START_RECORDING` is deliberately not part of the capture unlock owner chain.
It is instead gated by the macro-recording opt-in file. This keeps the macro
recording workflow unified after opt-in while preventing recording from being
an available default attack surface.

## Combo capture security model

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

## Compositor dispatch

Keymasq routes compositor dispatch actions through the active window-listener implementation.

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

## Policy file

Security policy path:

`/etc/keymasq/security.toml`

Relevant controls:

- `session_allowed_uids`
- `daemon_allowed_uids`
- `[macro]`
  - `exec_timeout_max_ms`: maximum `exec_sync` wait time. The daemon clamps
    macro payloads to this limit. The session uses the same value as the
    subprocess timeout and kills the command if it is exceeded.
- `[gui]`
  - `emergency_cancel_combo_enabled`
- `[recording_guard]`
  - `unlock_required`
  - `macro_recording_time_limit`: maximum macro recording duration in whole
    minutes. It defaults to `10`, and `0` disables the time limit
  - `macro_edit_requires_unlock`

Policy boolean values must use the TOML literals `true` or `false`. Keymasq
rejects strings, numbers, arrays, and other types instead of interpreting them
by truthiness. An invalid security policy prevents both `keymasqd` and
`keymasq-session` from starting. Their logs identify the invalid field so the
administrator can correct `/etc/keymasq/security.toml` and restart the services.

Empty UID allowlists mean no UID restriction. This is the default and is appropriate for single-user desktops. On multi-user systems, populate `daemon_allowed_uids` and `session_allowed_uids` to restrict access to specific users.

`[recording_guard].unlock_required` controls whether sensitive original-input
observation flows require an explicit unlock before they are allowed.

`[recording_guard].macro_recording_time_limit` limits how long one macro recording
may remain active. Reaching the limit stops the recording normally, keeps the
temporary slot available for saving or playback, and notifies the desktop.
Set it to `0` only when recordings should have no time limit.

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
macro get/create/update/rename/delete APIs also require an unlock, in addition
to live recording and capture.

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
- tapping the combo cancels all running macro playback and releases tracked
  held outputs directly in `keymasqd` after a 200 ms double-tap window
- double-tapping the combo runs a daemon runtime reset, releases all grabbed
  devices, broadcasts `runtime_reset`, and lets the session reapply profiles

When `emergency_cancel_combo_enabled = false`, the daemon does not inject the
combo and the GUI allows it to be assigned like any other combo. Disabling it
is not recommended unless you intentionally need that exact trigger.

## Hardware masking jobs

Masking coordination runs inside `keymasqd`. The existing `keymasq-record`
entry point performs privileged hardware changes in short-lived systemd jobs.
There is no resident root masking process. A Polkit rule lets only the dedicated
`keymasq` account start the fixed `keymasq-hardware@<request-id>.service` template.
It does not authorize arbitrary units, unit properties, or commands. Recording
or capture authorization through pkexec does not authorize these hardware commands.

The helper opens a daemon-owned request inode with `O_NOFOLLOW`, rejects unsafe
permissions and hard links, and reads a bounded JSON request. It accepts only
activation, offline arming, interface refresh, recovery, and source-hiding udev
triggers. A trigger request carries at most a list of `event*`/`js*` kernel
names. The helper validates them, drops names absent from `/sys/class/input`,
and runs `udevadm trigger` for the rest, so the daemon cannot supply paths or
other arguments. Attachment identities
and current generations resolve through root's sysfs inventory. The daemon cannot
supply driver names, mutation paths, shell commands, or permission snapshots.
The response is written through the pinned descriptor, not a reopened path.
Offline arming reuses a selector previously discovered and stored by root.
Saved selector paths are resolved before containment checks and hardware reads.
They must remain inside the sysfs devices tree, including while devices are offline.

Masking subprocesses and generated udev rules use the same trusted executable
resolver. Nix packages pin commands to their dependency store paths. Other
packages and source checkouts search only `/usr/sbin`, `/usr/bin`, `/sbin`, `/bin`,
and `/run/current-system/sw/bin`, and they ignore the caller's `PATH`. Missing commands
are reported before arming restrictions, and a missing package-pinned executable
does not fall back to another location.

Hardware jobs allow filesystem writes to their own `RuntimeDirectory` and
`StateDirectory`, the daemon's `/run/keymasq/hardware-requests` directory,
`/run/udev/rules.d`, and the legacy `/run/keymasq/hidden` and
`/run/keymasq/hidden-hardware` directories when present, for recovery cleanup.
They do not make the rest of `/run` writable. Tmpfiles creates
the udev rules directory before jobs start. Each job opens the current request
directory, while an admitted request's response stays on its validated inode if
the daemon's runtime directory is replaced.

The daemon binds confirmed startup preferences to its authenticated desktop UID
and keeps per-attachment confirmation deadlines and recovery state. GUI-supplied
UIDs cannot change ownership. Multiple masks have independent identities and
confirmation tokens. Ordinary interface changes or trial expiry recover only the
affected attachment. Persistent USB rules enforce access restrictions while
hardware is disconnected, without a running privileged helper.

Masking state is shared between users admitted by `daemon_allowed_uids`.
The current daemon owner can see saved device names and identities from other
users and request physical access restoration. Status tokens guard against stale
requests. They are not authorization secrets. Automatic masking and changes to
saved startup preferences still check the authenticated owner's UID.

Root-owned journals and static permission baselines survive a daemon failure.
The daemon unit uses `KillMode=mixed` so SIGTERM reaches only the daemon,
allowing it to await its `systemctl` hardware-job clients during graceful
shutdown. Systemd kills remaining child processes when the daemon exits or
the stop timeout expires. Hardware jobs run in separate service cgroups.
The daemon's systemd cleanup hook stops all outstanding hardware jobs before
restoring permissions. Per-attachment locks serialize mutations, and a global
recovery lock excludes all hardware jobs. `ExecStopPost` restores every remaining
reservation, and `ExecStartPre` requires recovery to succeed before remapping
starts again. A 20-second systemd watchdog kills a blocked daemon, releasing all
its grabs and outputs. Heartbeats run on the input loop, and awaiting a bounded
hardware job does not suppress them. This protects ordinary remapping as well as masking, but
does not detect every logical error in a responsive loop.

The short-lived job's bounded capabilities permit device/sysfs access, ownership
and ACL restoration, and inspection of `/proc/*/fd` device identities.
`CAP_SYS_PTRACE` is required for the kernel's ptrace access check on other users'
FD targets. The scan selects USB reconnect when an application holds a direct
USB handle, then checks for remaining handles after takeover. Ordinary driver
rebind does not revoke an application's open usbfs handle. Inspection failures
therefore fail the operation. Inaccessible processes are not silently skipped.
The exception is an inaccessible descriptor whose `fdinfo` mount ID identifies
exactly one FUSE mount with the per-mount `nodev` option in that process's
`mountinfo`. Such mounts cannot open device files. Missing, unreadable, malformed,
or ambiguous metadata refuses the exception, as does a detached mount that no
longer appears in the process's mount table. Skipping a verified FUSE descriptor
does not skip the process's other descriptors.
`CAP_SYS_PTRACE` belongs only to the short-lived root job. The scan does not
inspect input content. USB reconnect records and validates the individual port's
identity before changing it, refuses hubs and ganged power switching, and repairs
an interrupted port operation during recovery. Current desktop grants come from
udev after static permissions are restored. Old session ACLs are not replayed.
`keymasqd` itself holds no capabilities. See
[Hardware masking](hardware-masking.md) for user-facing behavior and
[Hardware masking design](hardware-masking-design.md) for the transaction details.

## Socket paths

- daemon socket: `/run/keymasq/socket` (mode `0o666`)
- session socket: `/run/user/<uid>/keymasq/session.sock` (mode `0o600`)

The daemon socket is world-accessible because `keymasqd` starts as a system service before any user session exists. Any user's `keymasq-session` must be able to connect and claim ownership. Access control is not enforced at the filesystem level but through the single-owner model. Once a session claims the daemon, all other connections are rejected. On multi-user systems, use `daemon_allowed_uids` to restrict which UIDs may connect.

The session socket is restricted to the owning user via `XDG_RUNTIME_DIR` permissions and explicit `0o700` on the socket directory.

Socket permissions only gate connection attempts. Sensitive command authority still depends on peer identity, unlock state, and owner binding.

## Security goal

Default operation should avoid repeated auth prompts while still protecting privileged input-observation flows.

The effective security model is:

- peer credential checks on every socket connection
- optional UID admission controls at session and daemon boundaries
- owner-bound runtime unlock refresh
- owner-bound sensitive command execution
- recording guard for original-input observation features
- listener-scoped compositor dispatch instead of shell execution

## Build attestations

GitHub releases include build attestations for published artifacts (`.deb`,
`.rpm`, `SHA256SUMS`). These are signed statements from GitHub Actions that
prove the Keymasq release workflow produced the artifact.

With the GitHub CLI, verify an artifact:

```bash
gh attestation verify ./keymasq_*_all.deb -R nyrda/keymasq
gh attestation verify ./keymasq-*.fc*.rpm -R nyrda/keymasq
gh attestation verify ./SHA256SUMS -R nyrda/keymasq
```

This is useful if you want to verify the build chain, not just the checksum.
Most users installing from the package repository do not need this, because
repository packages are already signed.

## Diagnostics mode

Keymasqd includes an optional diagnostics mode for internal latency measurement.

- Enable: `keymasq diagnostics on --interval 5`
- Disable: `keymasq diagnostics off`

When enabled, keymasqd logs periodic latency stats (`p50`, `p95`, `p99`, `max`) for internal event buckets.

View logs with:

`journalctl -u keymasqd -f`
