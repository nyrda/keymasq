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
import the source checkout from the current repository.

Before using them, stop any installed services that would conflict with the same
daemon or session sockets:

```bash
sudo systemctl stop keyforged
systemctl --user stop keyforge-session
```

## Running Checks

Run the full standard validation with:

```bash
./scripts/check.sh
```

This is the recommended way to run tests locally. `ruff` and `basedpyright` run
in the dev shell, and the full pytest suite runs in the Nix VM test harness
instead of directly on the host because many tests require `uinput` and other
system integration.

Run individual tools from the dev shell when needed:

```bash
nix develop -c ruff check keyforge tests
nix develop -c basedpyright
```

## Notes

- `keyforged` is expected to run as the `keyforge` user during normal installed-host development.
- `keyforge-session` and the GUI run as your desktop user.
- For packaging work and broader install testing, see `docs/PACKAGING.md`.
