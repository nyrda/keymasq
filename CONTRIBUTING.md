# Contributing

Keymasq accepts focused contributions that preserve the current architecture and
security model.

## Before You Start

- Read [AGENTS.md](AGENTS.md) for the project map and local rules.
- Read the relevant docs in `docs/` before changing behavior:
  - `docs/PROFILES.md`
  - `docs/COMBOS.md`
  - `docs/MACROS.md`
  - `docs/SECURITY.md`
- Prefer a small issue or discussion before large design changes.

## Development Setup

Use the development guide in [DEVELOPMENT.md](DEVELOPMENT.md).

The expected local quality gates are:

```bash
python -m pytest tests/ -v
ruff check keymasq tests
basedpyright
```

## Manual VM Test Gates

The NixOS VM integration suites are manual gates before PRs, merges, and
releases. They are intentionally not part of CI because they are too
resource-heavy, and they will not be added to it.

- Before opening a PR, run the VM suites required for your change category per
  [docs/VM_TESTING.md](docs/VM_TESTING.md) and record them in the PR template.
- Maintainers verify (and rerun where needed) the required suites before
  merging, and run the full set before releases.
- GUI changes must pass `scripts/check-doc-screenshots`, or the PR must include
  the regenerated documentation screenshots.

## Contribution Expectations

- Keep changes local unless the task genuinely requires a broader refactor.
- Preserve the split between `keymasqd`, `keymasq-session`, and the GTK UI.
- Keep compositor-specific behavior modular.
- Do not weaken recording or combo-capture security checks.
- Update the relevant `docs/*.md` file when user-visible behavior or security semantics change.
- Add or update tests with behavior changes when practical.

## Pull Requests

A good pull request should include:

- a clear summary of the problem and the change
- any user-visible behavior changes
- any packaging or service impact
- tests run locally, including the required manual VM suites from
  [docs/VM_TESTING.md](docs/VM_TESTING.md)
- for GUI changes, a passing `scripts/check-doc-screenshots` run or the
  regenerated screenshots in the PR
- screenshots for GUI changes when useful

## Scope Notes

The project targets Linux desktops. Packaging, service behavior, and desktop
integration changes should stay grounded in real supported environments rather
than hypothetical portability layers.
