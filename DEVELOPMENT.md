# Development Guide

Use the Nix dev shell for normal Keymasq development.

## Recommendation

- use `nix develop` for all local work
- run the source checkout directly from the repository
- do not use a Python virtualenv for the standard dev flow
- if Keymasq is already installed on the host, stop the installed services and run your local source changes manually

## Enter The Dev Shell

```bash
nix develop
```

The dev shell provides the standard project tooling, including `pytest`, `ruff`,
and `basedpyright`.

## Recommended Runtime Flow

If the machine already has the normal Keymasq install and permissions set up,
use one terminal per process:

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

## Notes

- `keymasqd` is expected to run as the `keymasq` user during normal installed-host development.
- `keymasq-session` and the GUI run as your desktop user.
- For packaging work and broader install testing, see `docs/PACKAGING.md`.
