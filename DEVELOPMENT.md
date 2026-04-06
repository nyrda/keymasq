# Development Guide

Use the Nix dev shell for normal Keyforge development.

## Recommendation

- use `nix develop` for all local work
- run the source checkout directly from the repository
- do not use a Python virtualenv for the standard dev flow
- if Keyforge is already installed on the host, stop the installed services and run your local source changes manually

## Enter The Dev Shell

```bash
nix develop
```

The dev shell provides the standard project tooling, including `pytest`, `ruff`,
and `basedpyright`.

## Recommended Runtime Flow

If the machine already has the normal Keyforge install and permissions set up,
use one terminal per process:

**Terminal 1 - keyforged from source as `keyforge`:**
```bash
./scripts/dev-keyforged.sh -v
```

**Terminal 2 - keyforge-session from source:**
```bash
./scripts/dev-session.sh -v
```

**Terminal 3 - GUI from source:**
```bash
./scripts/dev-gui.sh
```

These helpers automatically enter `nix develop` when needed and force Python to
import the current source tree. `dev-keyforged.sh` stages the Python package
under `/tmp` before switching to the `keyforge` user so development works even
when the repository lives under a private home directory.

Before using them, stop any installed services that would conflict with the same
daemon or session sockets:

```bash
sudo systemctl stop keyforged
systemctl --user stop keyforge-session
```

## Running Checks

Run the full standard validation with:

```bash
./scripts/check.sh full
```

For fast local validation, run the narrowest category that matches the code you
changed:

```bash
./scripts/check.sh keyforged
./scripts/check.sh session
./scripts/check.sh gui
```

`./scripts/check.sh` runs `ruff`, `basedpyright`, and the selected pytest
subset from the dev shell. Use `full` for multi-area changes, shared code, or
before handing off a broad refactor.

If the host does not have usable `uinput` access, or if you want the selected
pytest category to run in the VM backend instead of the host backend, add
`--vm`:

```bash
./scripts/check.sh --vm keyforged
./scripts/check.sh --vm full
```

Run individual tools from the dev shell when needed:

```bash
nix develop -c ruff check keyforge tests
nix develop -c basedpyright
```

## Local Test Input Suppression

Some host-side `keyforged` tests create real `uinput` devices so the remap
runtime can be exercised end to end. Install the local test rule if you want
those devices to stay out of libinput-based desktop sessions such as Hyprland
while the tests still read and write them through evdev/uinput:

```bash
sudo install -Dm644 udev/92-keyforge-test-input.rules /etc/udev/rules.d/92-keyforge-test-input.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=input --action=add
```

The rule ignores test devices named `keyforge-test-*`, which is the identity
used by the host-side pytest fixtures and test-mode output devices.

## Notes

- `keyforged` is expected to run as the `keyforge` user during normal installed-host development.
- `keyforge-session` and the GUI run as your desktop user.
- For packaging work and broader install testing, see `docs/PACKAGING.md`.
