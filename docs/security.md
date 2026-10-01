# Security model

## How Keymasq works around Wayland restrictions

Wayland intentionally prevents applications from reading or injecting input
across windows. Keymasq bypasses this by operating at the kernel level using
evdev and uinput, the same layer where compositors themselves read input.

A privileged system daemon (`keymasqd`) reads from `/dev/input/*` devices and
writes to a virtual device via `/dev/uinput`. This happens below Wayland, so
the compositor sees Keymasq's output as normal hardware input.

**Why this is still safe:**

- The daemon runs as a dedicated `keymasq` system user, not root, and holds no
  Linux capabilities
- GUI and CLI never touch input devices directly. They talk to a per-user
  session broker, which talks to the daemon
- The daemon accepts only one session connection at a time. Other local users
  cannot issue commands while a desktop session owns it, and
  `daemon_allowed_uids` restricts which users may own it at all
- The session socket is private to its user and lives outside the default
  view of sandboxed apps
- Root work runs only in short-lived, bounded hardware jobs and the daemon
  unit's recovery hooks

The rest of this document covers the security model in detail.

---

## Threat model

### Trusted: code running as the desktop user

Keymasq does not try to protect the desktop user's input from arbitrary,
unconfined code running with that user's authority. Such code controls the
user's Keymasq configuration, and that is enough to observe input:

- It can write profile or superkey configs whose shell command actions run on
  every press and release, for example an overload superkey that keeps each
  key's normal output and runs a command for it. The user loses no
  functionality and sees nothing. No capture or recording API is involved.

An authentication prompt in front of Keymasq's capture and recording APIs
therefore does not establish a reliable boundary against that code, and
Keymasq does not add one.

Two further points show how little such a prompt would add, though neither is
the main argument:

- A replacement for `keymasq-session` is an ordinary daemon client. It does
  not gain any authorization the daemon enforces itself, but it can use the
  same remapping features as the configuration route above.
- Outside Keymasq, same-user code can often observe input through the desktop
  itself, depending on the environment. Examples are a GNOME Shell extension
  or a KWin plugin loaded at the next login, `LD_PRELOAD` in
  `~/.config/environment.d` for services started after the change, a Hyprland
  plugin when Hyprland's plugin permissions are not enforced, a wrapped `sudo`
  in a shell rc file, or a malicious input method.

The normal GUI and session workflow keeps input observation explicit. These
are usability safeguards, not guarantees against hostile code running as the
desktop user, because a client that talks to the daemon directly does not have
to show anything:

- Macro recording is started from a **Toggle Recording** mapping or the
  **Record** button. The session sends a desktop notification when it starts
  and stops. Every recording writes into an explicitly chosen slot.
- Live capture, combo capture, and the Device Inspector run while the GUI
  shows them.

The daemon enforces the parts that do not depend on the client:
`macro_recording_time_limit` stops long recordings, combo capture is limited to
15 seconds, and owner disconnect ends every recording and capture.

### What Keymasq defends

Keymasq defends these boundaries:

- **Between local users.** Every connection is identified by `SO_PEERCRED`.
  `daemon_allowed_uids` and `session_allowed_uids` restrict which UIDs may
  connect. Each user's session socket sits in a `0700` directory under their
  `XDG_RUNTIME_DIR`, so other users cannot reach it. The daemon has a single
  owner and rejects every other connection while that owner is connected, so
  no other user can issue commands or observe input through it during a
  session.
- **Between sandboxed apps and the host.** The session socket lives in
  `XDG_RUNTIME_DIR` and the daemon socket in `/run/keymasq`. Sandboxes such as
  Flatpak do not expose these paths by default. An app granted access to them,
  or to the host filesystem, is not sandboxed from Keymasq.
- **Around the privileged side.** `keymasqd` runs as its own system user with
  an empty capability set and a closed device policy. Root work runs only as
  bounded `keymasq-hardware@` jobs with validated requests. HID-BPF programs
  are built from bundled drivers and handed to the daemon over a root-only
  socket. See the sections below.

### Known limitation: unowned daemon

With the default empty `daemon_allowed_uids`, any local UID can claim the daemon
while no session owns it. That happens before anyone logs in, during the
session's reconnect backoff, and after logout. The owner can grab devices and
observe input on them. On machines shared with other local users, set
`daemon_allowed_uids` to the desktop users who should own the daemon.

Recording slots and saved macros are shared by every UID that can own the
daemon. See [Stored recordings and macros](#stored-recordings-and-macros).

## Architecture

Keymasq uses a two-broker design:

- `keymasqd`: privileged daemon for evdev/uinput, macro storage, recording, and capture
- `keymasq-session`: per-user broker for GUI/CLI requests, profile logic, and compositor integration

GUI and CLI do not access kernel input devices directly.

`keymasq-helper` is not resident. It runs as root only in short-lived hardware
jobs (see [Hardware masking jobs](#hardware-masking-jobs)), in the daemon
unit's hardware recovery hooks, and from package removal scripts.

## Target environment

Keymasq is designed for single-user Linux desktops. The default security policy
reflects this, with open access and optional UID allowlists for multi-user
systems.

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

Both layers check peer credentials against their UID allowlist, and the daemon
enforces its single owner. Daemon-side checks remain the final authority.

## Peer identity and ACL

On each accepted Unix socket connection, Keymasq reads `SO_PEERCRED` (`pid`, `uid`, `gid`).

Optional UID allowlists can restrict which local users may connect to the session and daemon sockets.

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
deliberate design for that target:

- Ownership is **not** bound to an "installing user". Package installation
  cannot identify a reliable desktop owner. Installs commonly run as root,
  through configuration-management automation, inside an image build, or as
  declarative NixOS configuration. None of those contexts name the human who
  will sit at the machine, and several produce systems with no such user at
  install time at all.
- Ownership does **not** infer an "active seat". Keymasq does not track logind
  seats, active VTs, or greeter sessions.

On a single-seat desktop, the first allowed `keymasq-session` to connect is the
logged-in user's broker, which is exactly the process that should own the
daemon. For shared-system installations, `daemon_allowed_uids` in
`/etc/keymasq/security.toml` is the explicit control. It restricts which UIDs
may connect at all, so only a listed user can ever claim ownership. See
[Known limitation: unowned daemon](#known-limitation-unowned-daemon).

### Ownership lifecycle and cleanup

When the owning connection disconnects, whether through a clean shutdown, a
crash, or a daemon restart of `keymasq-session`, `keymasqd` runs disconnect
cleanup before the next client can claim ownership:

1. It restores active hardware masks.
2. It aborts any active recording without producing or persisting a recording.
3. It discards all pending (unsaved) recordings that are not stored in a slot.
4. It closes all live capture sessions.
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

## Macro recording

Macro recording observes original input. In the normal workflow it is explicit:

- It is started from a **Toggle Recording** mapping or the **Record** button in
  Macro Manager.
- The session sends a desktop notification whenever the daemon reports that a
  recording started or stopped, whatever triggered it.
- Every recording writes into one of four explicitly chosen temporary slots.
  Keymasq never infers a slot.

The daemon itself enforces `macro_recording_time_limit`, which stops a
recording that runs too long, and ends every recording when its owner
disconnects. The notifications come from the session and are not a guarantee
against a client that talks to the daemon directly.

Administrators can turn recording off with
`[recording_guard] macro_recording_allowed = false`. The daemon then refuses to
start recordings with `macro_recording_disabled`, the session shows a
"Macro recording disabled" notification when a recording trigger fires, and the
GUI disables its record controls. Existing slots and saved macros can still be
played. This switch turns off the built-in macro recorder only. It is not an
anti-keylogging policy: live capture, combo capture, the Device Inspector, and
command actions in mappings keep working.

Temporary macro slots are pending recording handles, not inspectable macro
bodies. They can be replayed only through an explicit slot playback action and
cannot be fetched through the macro body APIs. Slot data is kept in
daemon-private storage so slots survive daemon restarts. Saving a slot copies
it into normal macro storage and leaves the slot in place. Deleting or
overwriting a slot removes the pending recording.

Saved macros live in `/var/lib/keymasq/macros/`, owned by the `keymasq` system
user. Clients read and change them only through the session broker and daemon.

### Stored recordings and macros

Keymasq targets single-user desktops and keeps one set of recording slots and
one macro library in the daemon. Single-owner admission keeps other users out
while an owner is connected, but it does not separate stored data between
successive owners. Any UID that can own the daemon can list, replay, and save
retained slots and read and change saved macros, including those recorded by
another user. On machines with more than one desktop user, set
`daemon_allowed_uids` to the users who may share them.

## Input capture and inspection

Live key capture, combo capture, and the Device Inspector observe original input
for the admitted daemon owner. They need no additional
authorization, for the reasons in [Threat model](#threat-model).

- Capture is performed by `keymasqd`, not by GUI key events.
- Combo capture observes raw events from the hardware interfaces associated
  with the selected profile. The daemon clamps each combo capture to at most
  15 seconds, so a stale request cannot leave a capture running.
- The Device Inspector force-grabs the configured interfaces of one device
  while its window is open and can block that device's remap output. Closing
  the window ends inspection and suppression.
- When the owning session disconnects, the daemon closes every capture session.

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
- Sway dispatch is sent unchanged through Sway's IPC socket, the same as
  `swaymsg`. Keymasq does not filter these commands, because profiles can
  already run programs through exec actions
- GNOME dispatch and cursor-position requests are restricted to allowlisted RPCs
  handled by the Keymasq GNOME Shell bridge

This keeps compositor-specific control inside the listener boundary. Keymasq never starts a shell to send a compositor action.

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
  - `macro_recording_allowed`: whether the built-in macro recorder may start.
    It defaults to `true`. It does not restrict live capture, combo capture,
    the Device Inspector, or command actions
  - `macro_recording_time_limit`: maximum macro recording duration in whole
    minutes. It defaults to `10`, and `0` disables the time limit

Policy boolean values must use the TOML literals `true` or `false`. Keymasq
rejects strings, numbers, arrays, and other types instead of interpreting them
by truthiness. An invalid security policy prevents both `keymasqd` and
`keymasq-session` from starting. Their logs identify the invalid field so the
administrator can correct `/etc/keymasq/security.toml` and restart the services.

`keymasqd` and `keymasq-session` read the policy at startup. Restart both after
changing it:

```sh
sudo systemctl restart keymasqd
systemctl --user restart keymasq-session
```

Older installs may still contain `[recording_guard] unlock_required` or
`macro_edit_requires_unlock`. Keymasq no longer uses these keys. It ignores
them and logs a warning, and they can be deleted.

Empty UID allowlists mean no UID restriction. This is the default and is
appropriate for single-user desktops. On multi-user systems, populate
`daemon_allowed_uids` and `session_allowed_uids` to restrict access to specific
users.

`[recording_guard].macro_recording_allowed = false` blocks new macro recordings
for every user. It does not block other input observation. See
[Macro recording](#macro-recording).

`[recording_guard].macro_recording_time_limit` limits how long one macro recording
may remain active. Reaching the limit stops the recording normally, keeps the
temporary slot available for saving or playback, and notifies the desktop.
Set it to `0` only when recordings should have no time limit.

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

Masking coordination runs inside `keymasqd`. The existing `keymasq-helper`
entry point performs privileged hardware changes in short-lived systemd jobs.
There is no resident root masking process. A Polkit rule lets only the dedicated
`keymasq` account start the fixed `keymasq-hardware@<request-id>.service` template.
It does not authorize arbitrary units, unit properties, or commands.

The helper opens a daemon-owned request inode with `O_NOFOLLOW`, rejects unsafe
permissions and hard links, and reads a bounded JSON request. It accepts only
activation, offline arming, interface refresh, recovery, source-hiding udev
triggers, and HID-BPF attachments for native input drivers. A trigger request carries at most a list of `event*`/`js*` kernel
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

Masking only narrows who can open a device and never returns input to the
caller. Ordinary remapping already grabs configured devices exclusively and
hides grabbed gamepad nodes through the same job path.

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

## Native input programs

Some native input drivers, such as Steam Deck touch, read reports inside
the kernel through HID-BPF. Loading one needs `CAP_BPF` and `CAP_PERFMON`, which
only the short-lived hardware job holds. An attach request names a bundled
driver, a HID device name, and a one-time token. The job resolves the device in
sysfs, requires the driver to match it, and assembles the program from that
driver's code. Requests cannot supply programs, maps, BTF, or paths. The program
only observes reports and never modifies them.

The job hands the program's link and map descriptors to `keymasqd` over
`/run/keymasq/handoff`. The daemon accepts connections there only from root and
closes descriptors that arrive without a pending token. The job refuses to send
unless the socket's peer is the `keymasq` account. The daemon needs no BPF
privilege to read the maps: it maps them and polls the ring buffer. Closing the
link descriptor detaches the program, so a stopped or crashed daemon leaves
nothing attached. The daemon does not see other devices' reports, and the kernel
drops the program when the HID device disappears.

## Socket paths

- daemon socket: `/run/keymasq/socket` (mode `0o666`)
- privileged handoff socket: `/run/keymasq/handoff` (mode `0o600`, root peers only)
- session socket: `/run/user/<uid>/keymasq/session.sock` (mode `0o600`)

The daemon socket is world-accessible because `keymasqd` starts as a system
service before any user session exists. Any allowed user's `keymasq-session`
must be able to connect and claim ownership. Access control is not enforced at
the filesystem level but through peer credentials, `daemon_allowed_uids`, and
the single-owner model. Once a session claims the daemon, all other connections
are rejected. On multi-user systems, use `daemon_allowed_uids` to restrict which
UIDs may connect.

The session socket is restricted to the owning user via `XDG_RUNTIME_DIR`
permissions and explicit `0o700` on the socket directory.

## Security goal

Keymasq should not add a way for one local user to control or observe another
user's input, for a sandboxed app to reach Keymasq, or for the daemon to gain
more privilege than input access. It does not try to protect a user from code
already running as that user.

The effective security model is:

- peer credential checks on every socket connection
- optional UID admission controls at session and daemon boundaries
- a single daemon owner with cleanup on disconnect
- a private per-user session socket
- a capability-free daemon and bounded root hardware jobs
- a daemon-enforced time limit on macro recordings, and an administrator
  switch for the built-in recorder
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
