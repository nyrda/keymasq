# Superkey Expansion Implementation

Status: complete

## Checklist

- [x] Create tracking file and implementation checklist.
- [x] Add shared model support for pattern bundles and overload-mode superkeys.
- [x] Add strict TOML load/save for the new superkey schema.
- [x] Update session payload/signature serialization for bundled and overload superkeys.
- [x] Update daemon/runtime payload parsing for bundled and overload superkeys.
- [x] Implement bundled pattern-slot execution in the superkey state machine.
- [x] Implement overload-mode fanout execution with normal down/repeat/up semantics.
- [x] Update superkey GUI/editor for mode selection and ordered action lists.
- [x] Update action summaries for bundled superkey slots.
- [x] Add storage/runtime/device tests for the new behavior.
- [x] Update end-user docs for the new superkey modes and bundle semantics.
- [x] Run targeted tests for superkey runtime and device behavior.
- [x] Run full project checks.

## Decisions

- Superkeys now have two exclusive modes: `pattern` and `overload`.
- Pattern-mode slots (`tap`, `double_tap`, `hold`, `tap_hold`) store ordered action lists.
- Pattern-mode bundle presses run in list order and releases run in reverse order.
- Overload mode stores one ordered action list and fans out the source event to each child action.
- Overload children cannot be nested superkeys.
- Superkey files must use explicit `mode` and list-based action bundles.

## Progress Notes

- Initial tracking file created.
- Added `pattern` vs `overload` superkey modes to the shared model.
- Pattern slots now store ordered action bundles; overload mode stores ordered
  `MappingAction` children.
- Session superkey TOML now round-trips bundle syntax and overload child
  mappings with one strict schema.
- Runtime payloads now serialize and parse both new modes, including overload
  `exec` references.
- The pattern state machine now executes bundles in order and releases them in
  reverse order.
- Overload mode now fans out normal down/repeat/up events and refcounts shared
  held outputs across multiple source keys.
- The GTK superkey editor now exposes a mode selector and ordered list editors
  for both pattern slots and overload actions.
- Added targeted tests for strict schema loading, bundle ordering, overload
  payload parsing, overload fanout, and shared-output refcount behavior.
- Final verification passed via `./scripts/check.sh full`.
