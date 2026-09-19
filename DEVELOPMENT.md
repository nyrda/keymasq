# Development Guide

Use the Nix dev shell for normal Keymasq development.

## Recommendation

- use `nix develop` for all local work
- run the source checkout directly from the repository
- do not use a Python virtualenv for the standard dev flow
- use `./scripts/dev.sh` to run and restart your local source checkout

## Enter The Dev Shell

```bash
nix develop
```

The dev shell provides the standard project tooling, including `pytest`, `ruff`,
and `basedpyright`.

## Recommended Runtime Flow

If the machine already has the normal Keymasq install and permissions set up,
open the tmux dev workspace in one terminal:

```bash
./scripts/dev.sh
```

The workspace shows daemon, session, and GUI logs with restart controls in
the header. F4 opens a searchable worktree picker. Opening `dev.sh` from another
worktree also switches over, gracefully stopping the old processes first.
Run `./scripts/dev.sh --help` for command-line options.

You can also run the individual launchers in separate terminals:

**Terminal 1 - keymasqd from source as `keymasq`:**
```bash
./scripts/dev-keymasqd.sh -v
```

**Terminal 2 - keymasq-session from source:**
```bash
./scripts/dev-session.sh -v
```

**Terminal 3 - GUI from source:**
```bash
./scripts/dev-gui.sh
```

These helpers automatically enter `nix develop` when needed and force Python to
import the current source tree. `dev-keymasqd.sh` stages the Python package
under `/tmp` before switching to the `keymasq` user so development works even
when the repository lives under a private home directory. The daemon and
session helpers also stop the corresponding installed `systemd` service before
launching the source checkout so they can take over the same sockets cleanly.

For install-like app entrypoint testing, use:

```bash
./scripts/dev-app.sh
```

`dev-app.sh` builds the Nix package and runs the wrapped `keymasq` binary. It
mirrors the installed entrypoint: no arguments launch the GUI, while command
arguments dispatch through the CLI path. Use it when validating packaging or
wrapper behavior, not as the fastest GUI iteration loop.

## Sudo Rules for the Development Daemon

`dev-keymasqd.sh` needs root for a fixed set of commands: stopping the installed
`keymasqd.service`, creating `/run/keymasq` and `/var/lib/keymasq`, and starting
the daemon. The daemon itself starts through `setpriv`, which switches to the
`keymasq` user and grants the single `CAP_DAC_OVERRIDE` ambient capability that
`keymasqd.service` also grants. Without it, `udevadm trigger` cannot write
sysfs `uevent` files and source hiding logs `Permission denied` warnings (see
`docs/TROUBLESHOOTING.md`). If `setpriv` is missing, the launcher falls back to
plain `sudo -u keymasq` and warns.

Without any sudo rule the launcher prompts for your password on every restart.
The three setup commands are fixed and safe to allow without a password. The
daemon start is not: it runs whatever the worktree contains, with a capability
that bypasses file permission checks and without the service's sandbox. A
passwordless rule for it would give every process running as your desktop user
root-equivalent access, so let that command keep prompting. Sudo's credential
cache reduces the prompts; `Defaults timestamp_timeout=30` in your sudoers
keeps one password per half hour.

The launcher prefers stable `/run/current-system/sw/bin` paths on NixOS so one
rule keeps matching across worktrees with different nixpkgs pins. Replace `alice`
with your desktop user:

```nix
security.sudo.extraRules = [
  {
    users = [ "alice" ];
    runAs = "root";
    commands = map (command: {
      inherit command;
      options = [ "NOPASSWD" "NOSETENV" ];
    }) [
      "/run/current-system/sw/bin/systemctl stop keymasqd.service"
      "/run/current-system/sw/bin/install -d -m 0755 -o keymasq -g keymasq /run/keymasq"
      "/run/current-system/sw/bin/install -d -m 0750 -o keymasq -g keymasq /var/lib/keymasq"
    ];
  }
];
```

The equivalent plain sudoers entries, for example in `/etc/sudoers.d/keymasq-dev`,
are one line per command with the same `NOPASSWD:NOSETENV:` options. On other
distributions the launcher resolves `install` and `systemctl` from the dev shell
`PATH`, which points into the Nix store; check the exact paths with `command -v`
inside `nix develop` before pinning them.

## Running Checks

Run the standard validation with:

```bash
./scripts/check.sh
```

By default, `check.sh` runs in `auto` mode and selects the narrowest safe
category from pending and untracked changes under `keymasq/` and `tests/`.
It falls back to `full` for shared, mixed, or broad changes.

You can also run a category explicitly:

```bash
./scripts/check.sh keymasqd
./scripts/check.sh session
./scripts/check.sh gui
./scripts/check.sh full
```

`./scripts/check.sh` runs `ruff`, `basedpyright`, and the selected pytest
subset from the dev shell when `auto` finds relevant code changes. Use `full`
for multi-area changes, shared code, or before handing off a broad refactor.

Test modules live under `tests/common/`, `tests/keymasqd/`, `tests/session/`,
or `tests/gui/`. Pytest assigns each test its directory's category. Keep shared
test helpers in `tests/`; changes to those helpers select the full check suite.

If the host does not have usable `uinput` access, or if you want the selected
pytest category to run in the VM backend instead of the host backend, add
`--vm`:

```bash
./scripts/check.sh --vm keymasqd
./scripts/check.sh --vm full
```

Run individual tools from the dev shell when needed:

```bash
nix develop -c ruff check keymasq tests
nix develop -c basedpyright
```

## Running Integration Tests

The VM integration suites are manual gates before PRs, merges, and releases;
they are intentionally not part of CI. `docs/VM_TESTING.md` defines which
suites are required for each change category.

Keymasq has two NixOS VM integration suites:

- listener VM tests for compositor/window tracking under GNOME, KDE, Hyprland,
  Niri, XFCE/X11, COSMIC, and Sway
- the daemon/session runtime suite, which starts `keymasqd` and
  `keymasq-session`, drives virtual input devices, and checks remapped output

Use the integration helper from the repository root:

```bash
./scripts/integration.sh cosmic
./scripts/integration.sh daemon-session
```

List the available shortcuts with:

```bash
./scripts/integration.sh --help
```

The helper runs `nix build` against `path:.#checks.x86_64-linux...` targets so
new or uncommitted VM files are included during local development. These tests
are VM-heavy; a Linux host with KVM acceleration is strongly recommended.

For detailed behavior and debugging notes, see `docs/LISTENER_VM_TESTS.md` and
`docs/DAEMON_SESSION_INTEGRATION_TEST.md`. For the gate policy and the
change-category matrix, see `docs/VM_TESTING.md`.

## Local Test Input Suppression

Some host-side `keymasqd` tests create real `uinput` devices so the remap
runtime can be exercised end to end. Install the local test rule if you want
those devices to stay out of libinput-based desktop sessions such as Hyprland
while the tests still read and write them through evdev/uinput:

```bash
sudo install -Dm644 udev/92-keymasq-test-input.rules /etc/udev/rules.d/92-keymasq-test-input.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=input --action=add
```

The rule ignores test devices named `keymasq-test-*`, which is the identity
used by the host-side pytest fixtures and test-mode output devices.

## Testing On An AppImage Host Without Rebuilding The AppImage

Keymasq is pure Python, so Python-only changes do not need a new AppImage.
`scripts/appimage-dev-sync.sh` pushes the worktree's `keymasq/` package to a
remote host, such as a Steam Deck, that already has the AppImage installed and
is reachable over SSH with key login and passwordless sudo. Connect as the
desktop user that installed the AppImage, because `keymasq-session` runs in
that user's systemd manager:

```bash
export KEYMASQ_DEV_HOST=deck@<deck-ip>
./scripts/appimage-dev-sync.sh          # sync, then restart keymasqd and keymasq-session on it
./scripts/appimage-dev-sync.sh status   # which runtime the services use
./scripts/appimage-dev-sync.sh logs     # follow both service logs
./scripts/appimage-dev-sync.sh revert   # back to the installed AppImage runtime
```

The installed runtime is never modified. The script hardlink-clones
`/opt/keymasq/runtime/current` to `/opt/keymasq/dev-runtime`, gives the clone a
private copy of the `keymasq` package, and adds systemd drop-ins that set
`KEYMASQ_APPDIR` for both services. The clone is rebuilt automatically after an
AppImage update. The build-generated `common/build_paths.py` is preserved. Run
the GUI or CLI on the synced code with
`KEYMASQ_APPDIR=/opt/keymasq/dev-runtime /opt/keymasq/bin/keymasq`.

Changes to bundled dependencies, native libraries, units, or udev rules still
need a real AppImage build.

## Notes

- `keymasqd` is expected to run as the `keymasq` user during normal installed-host development.
- `keymasq-session` and the GUI run as your desktop user.
- For packaging work and broader install testing, see `docs/PACKAGING.md`.
