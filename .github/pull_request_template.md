## Summary

-
-

## Testing

VM integration suites are manual gates, not CI. Pick the required suites from
the change-category matrix in `docs/vm-testing.md`.

- [ ] `./scripts/check.sh`
- [ ] Required VM suites per `docs/vm-testing.md` ran and passed.
  - Suites run: `./scripts/integration.sh <suite ...>`
  - Suites skipped, with reason:
- [ ] GUI changes: `scripts/check-doc-screenshots` passed, or the regenerated
      screenshots are part of this PR / not applicable:
- [ ] Other testing:

## Notes

- packaging impact:
- service or security impact:
- GUI impact:
