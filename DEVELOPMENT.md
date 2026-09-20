# Development guide

Use the Nix dev shell for normal Keymasq development.

## Recommendation

- use `nix develop` for all local work
- run the source checkout directly from the repository
- do not use a Python virtualenv for the standard dev flow
- use `./scripts/dev.sh` to run and restart your local source checkout

## Enter the dev shell

```bash
nix develop
```

The dev shell provides the standard project tooling, including `pytest`, `ruff`,
and `basedpyright`.

## Recommended runtime flow

If the machine already has the normal Keymasq install and permissions set up,
open the tmux dev workspace in one terminal:

```bash
./scripts/dev.sh
```

The workspace shows daemon, session, and GUI logs with restart controls in the
header. F4 opens a searchable worktree picker. Opening `dev.sh` from another
worktree also switches over, after stopping the old processes. Run
`./scripts/dev.sh --help` for command-line options.

You can also run the individual launchers in separate terminals:

**Terminal 1, keymasqd from source as `keymasq`:**
```bash
./scripts/dev-keymasqd.sh -v
```

**Terminal 2, keymasq-session from source:**
```bash
./scripts/dev-session.sh -v
```

**Terminal 3, GUI from source:**
```bash
./scripts/dev-gui.sh
```

These helpers enter `nix develop` when needed and force Python to import the
current source tree. `dev-keymasqd.sh` stages the Python package under `/tmp`
before switching to the `keymasq` user so development works even when the
repository lives under a private home directory. The daemon and session helpers
also stop the corresponding installed `systemd` service before launching the
source checkout so they can take over the same sockets.

For install-like app entrypoint testing, use:

```bash
./scripts/dev-app.sh
```

`dev-app.sh` builds the Nix package and runs the wrapped `keymasq` binary. It
mirrors the installed entrypoint. No arguments launch the GUI, while command
arguments dispatch through the CLI path. Use it when validating packaging or
wrapper behavior, not as the fastest GUI iteration loop.

## Sudo rules for the development daemon

`dev-keymasqd.sh` needs root for a fixed set of commands: stopping the installed
`keymasqd.service`, creating `/run/keymasq` and `/var/lib/keymasq`, and starting
the daemon as the `keymasq` user through `sudo -u keymasq`. Like
`keymasqd.service`, the foreground daemon holds no capabilities. Source hiding
runs its `udevadm trigger` calls through the installed `keymasq-hardware@` jobs,
so hiding only works when the hardware job unit and its Polkit rule are
installed. Without them the daemon logs `udev trigger job failed` warnings (see
`docs/troubleshooting.md`) and everything else keeps working.

Without any sudo rule the launcher prompts for your password on every restart.
The three setup commands are fixed and safe to allow without a password. The
daemon start is not. It runs whatever the worktree contains as the daemon
account, with its input device access and its authority to start root hardware
jobs, and without the service's sandbox. A passwordless rule for it would hand
that to every process running as your desktop user, so let that command keep
prompting. Sudo's credential cache reduces the prompts, and
`Defaults timestamp_timeout=30` in your sudoers keeps one password per half
hour.

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

The equivalent plain sudoers entries, for example in
`/etc/sudoers.d/keymasq-dev`, are one line per command with the same
`NOPASSWD:NOSETENV:` options. On other distributions the launcher resolves
`install` and `systemctl` from the dev shell `PATH`, which points into the Nix
store. Check the exact paths with `command -v` inside `nix develop` before
pinning them.

## Running checks

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

Test modules live under `tests/common/`, `tests/keymasqd/`, `tests/session/`, or
`tests/gui/`. Pytest assigns each test its directory's category. Keep shared
test helpers in `tests/`. Changes to those helpers select the full check suite.

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

## Running integration tests

The VM integration suites are manual gates before PRs, merges, and releases. CI
does not run them, by design. `docs/vm-testing.md` defines which suites each
change category requires.

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
local builds include new or uncommitted VM files. These tests are VM-heavy, so
use a Linux host with KVM acceleration.

For detailed behavior and debugging notes, see `docs/listener-vm-tests.md` and
`docs/daemon-session-integration-test.md`. For the gate policy and the
change-category matrix, see `docs/vm-testing.md`.

## Local test input suppression

Some host-side `keymasqd` tests create real `uinput` devices to exercise the
remap runtime end to end. Install the local test rule if you want those devices
to stay out of libinput-based desktop sessions such as Hyprland while the tests
still read and write them through evdev/uinput:

```bash
sudo install -Dm644 udev/92-keymasq-test-input.rules /etc/udev/rules.d/92-keymasq-test-input.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=input --action=add
```

The rule ignores test devices named `keymasq-test-*`, the name that the
host-side pytest fixtures and test-mode output devices use.

## Testing on an AppImage host without rebuilding the AppImage

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

The script never modifies the installed runtime. It hardlink-clones
`/opt/keymasq/runtime/current` to `/opt/keymasq/dev-runtime`, gives the clone a
private copy of the `keymasq` package, and adds systemd drop-ins that set
`KEYMASQ_APPDIR` for both services and the privileged `keymasq-hardware@`
helper. The script rebuilds the clone after an AppImage update and preserves the
build-generated `common/build_paths.py`. Run the GUI or CLI on the synced code
with `KEYMASQ_APPDIR=/opt/keymasq/dev-runtime /opt/keymasq/bin/keymasq`.

Changes to bundled dependencies, native libraries, units, or udev rules still
need a real AppImage build.

## Notes

- `keymasqd` should run as the `keymasq` user during normal installed-host
  development.
- `keymasq-session` and the GUI run as your desktop user.
- For packaging work and broader install testing, see `docs/packaging.md`.

## Documentation URLs

Documentation filenames use lowercase words separated by hyphens. The MkDocs
build hook in `scripts/docs_redirects.py` preserves the uppercase public URLs
that existed before the September 2026 rename. Redirects stay within the current
documentation version and retain query strings and section anchors. Keep these
aliases for bookmarks, external links, and help links in older releases.
