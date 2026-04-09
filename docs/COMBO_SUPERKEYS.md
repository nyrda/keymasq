# Superkey Support In Combos

This note maps the implementation shape for combo-triggered superkeys onto the
current combo and superkey code paths.

Status:

- `Overload` support is implemented.
- `Pattern` support is implemented for single-step combos.
- Multi-step `Pattern` support uses only `Tap` and `Hold`.

## Current Blockers

Superkey combo actions are currently rejected or dropped in three places:

- `keyforge/gui/widgets/combo_editor_dialog.py` rejects
  `ActionType.SUPERKEY` during combo validation.
- `keyforge/session/manager/payloads.py` returns `None` for combo
  `superkey` actions, so they never reach the daemon.
- `keyforge/keyforged/runtime/combos.py` returns early from
  `start_combo_action()` when the parsed action type is `SUPERKEY`.

The daemon-side action parser already understands embedded serialized
superkey configs through `keyforge/keyforged/runtime/actions.py`. That means
the main missing work is policy, payloading, and combo-runtime execution.

## Overload First

`Overload` is the lowest-risk first step because it is already defined as an
ordered bundle of normal mapping actions.

### Why It Fits Well

- Combo actions already have a clean press/release lifecycle.
- Overload children are already stored as `list[MappingAction]`.
- Nested superkeys are already forbidden inside overload actions.
- The existing mapping-side overload runtime in
  `keyforge/keyforged/runtime/grabbed_device_actions.py` already proves the
  child-action fanout model.

### Practical Semantics For Combos

Treat a combo-triggered overload key as a bundle whose lifecycle is owned by
the combo:

- When the combo completes, start all overload child actions in list order.
- When the combo releases, stop any held child actions in reverse order.
- One-shot children like exec, compositor, profile, and recording-control
  actions still fire only on combo press.
- Child actions that already manage their own timing, like tap and
  rapidfire, keep using their existing action semantics.

This is slightly different from a device-mapped overload superkey because a
combo does not have a meaningful hardware repeat stream of its own. That is
fine for a first pass because combo actions are already modeled as
press/start plus release/stop.

### Minimal Code Shape

1. Allow combo actions to carry serialized superkey payloads in
   `keyforge/session/manager/payloads.py`.
2. Lift the combo-editor restriction for superkeys, but only allow saved
   superkeys whose mode is `overload` in the first pass.
3. Replace the current `SUPERKEY` early return in
   `keyforge/keyforged/runtime/combos.py` with overload fanout logic.
4. Extend `ComboActionState` so one combo can own multiple child action
   states, or store child states under synthetic ids such as
   `"{combo_id}#overload#{index}"`.

The main implementation choice is whether to:

- extract a small shared helper that fans out `list[MappingAction]` for both
  mapping-side overload and combo-side overload, or
- keep the runtimes separate and let combo runtime start and stop child
  actions using its existing combo-specific helpers.

The second option is probably the cheaper first patch because combo actions
already have their own `ComboActionState`, macro handling, and stop paths.

## Pattern Support

Pattern support is materially harder because the current combo engine decides
when the combo is complete, while the current `SuperkeyMachine` expects to
observe the source input cadence directly.

### Good Staging

- First support `Pattern` only for single-step combos.
- If multi-step pattern support is added later, keep it to `Tap` and `Hold`
  only.
- Do not support `Double Tap` or `Tap + Hold` for multi-step combos in the
  first version.

This matches the current runtime shape well enough to stay understandable.

### Why Single-Step Is Easier

Single-step combos already behave like a single completed chord. After the
final key press, the runtime can defer action selection and watch the release
timing of that completed step without also managing inter-step progression.

A workable model is:

- Combo matcher still owns trigger recognition.
- After final-step completion, combo runtime enters a pattern-pending state
  instead of firing immediately.
- Releasing before the hold threshold selects `Tap`.
- Crossing the hold threshold selects `Hold`.
- For single-step only, the runtime may optionally watch for a second press
  of the same final binding to implement `Double Tap` and `Tap + Hold`.

### Why Multi-Step Needs To Be Cut Down

For multi-step combos, double-tap style behavior collides with the combo
engine's own sequencing model:

- earlier steps already consumed and released their bindings;
- the final step is the only binding still available as a clean gesture
  source;
- waiting for a second tap after combo completion starts to look like a new
  combo attempt, not a continuation of the old one.

That makes `Tap` and `Hold` plausible, but `Double Tap` and `Tap + Hold`
become ambiguous and expensive.

## Suggested Runtime Shape For Pattern

If pattern support is added, it should probably not reuse `SuperkeyMachine`
unchanged.

The current machine is keyed to a physical source event name and owns its own
down/up sequence. Combo runtime already has different ownership:

- combo engine decides when a combo becomes active;
- combo runtime decides when the combo action starts and stops;
- the final-step bindings may belong to several hardware sources.

A smaller combo-specific pattern runner will likely be easier than forcing
the full `SuperkeyMachine` into this path.

Useful responsibilities for that runner:

- arm a hold timer after combo completion;
- choose `tap` versus `hold`;
- optionally watch a second press only for single-step combos;
- preserve current combo release semantics for held outputs.

## Suggested Order

1. Add payload support for combo `superkey` actions.
2. Implement combo-side `Overload` execution only.
3. Enable `Overload` in the combo editor.
4. Add tests for overload press, release, exec child actions, and mixed child
   bundles.
5. Add single-step `Pattern` with `Tap` and `Hold`.
6. Decide whether single-step `Double Tap` and `Tap + Hold` are worth the UX
   and runtime complexity.
7. If multi-step pattern is still wanted, add only `Tap` and `Hold`.

## Testing Focus

- Combo payload signatures should change when a referenced superkey changes.
- `set_combos()` should parse embedded superkey configs the same way device
  mappings already do.
- Overload combo actions should press in order and release in reverse order.
- Combo teardown and device-release cleanup must stop active overload
  children.
- Single-step pattern timing should be covered separately from combo-engine
  matching tests.
