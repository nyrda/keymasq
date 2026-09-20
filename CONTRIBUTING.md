# Contributing

Keymasq accepts focused contributions that preserve the current architecture and
security model.

## Before you start

- Read [AGENTS.md](AGENTS.md) for the project map and local rules.
- Read the relevant docs in `docs/` before changing behavior:
  - `docs/profiles.md`
  - `docs/combos.md`
  - `docs/macros.md`
  - `docs/macro-editor.md`
  - `docs/security.md`
- Open a small issue or discussion before large design changes.

## Development setup

Use the development guide in [DEVELOPMENT.md](DEVELOPMENT.md).

The expected local quality gates are:

```bash
python -m pytest tests/ -v
ruff check keymasq tests
basedpyright
```

## Manual VM test gates

The NixOS VM integration suites are manual gates before PRs, merges, and
releases. CI does not run them because they are too resource-heavy, and there
is no plan to add them.

- Before opening a PR, run the VM suites required for your change category per
  [docs/vm-testing.md](docs/vm-testing.md) and record them in the PR template.
- Maintainers verify (and rerun where needed) the required suites before
  merging, and run the full set before releases.
- GUI changes must pass `scripts/check-doc-screenshots`, or the PR must include
  the regenerated documentation screenshots.

## Contribution expectations

- Keep changes local unless the task requires a broader refactor.
- Preserve the split between `keymasqd`, `keymasq-session`, and the GTK UI.
- Keep compositor-specific behavior modular.
- Do not weaken recording or combo-capture security checks.
- Update the relevant `docs/*.md` file when user-visible behavior or security semantics change.
- Add or update tests with behavior changes when practical.

## Pull requests

A pull request should include:

- a clear summary of the problem and the change
- any user-visible behavior changes
- any packaging or service impact
- tests run locally, including the required manual VM suites from
  [docs/vm-testing.md](docs/vm-testing.md)
- for GUI changes, a passing `scripts/check-doc-screenshots` run or the
  regenerated screenshots in the PR
- screenshots for GUI changes when useful

## Scope notes

The project targets Linux desktops. Packaging, service behavior, and desktop
integration changes should target real supported environments rather than
hypothetical portability layers.
