# Temporary Plan: Superkeys In Combos

This is a working implementation plan and progress tracker for adding combo
support for superkey actions.

## Scope

Target behavior:

- Support combo actions that reference saved superkeys.
- Ship `Overload` first.
- Stage `Pattern` support:
  - single-step combos first;
  - multi-step combos only for `Tap` and `Hold`;
  - no multi-step `Double Tap`;
  - no multi-step `Tap + Hold`.

Current blockers in code:

- `keyforge/gui/widgets/combo_editor_dialog.py`
- `keyforge/session/manager/payloads.py`
- `keyforge/keyforged/runtime/combos.py`

Important implementation note:

- combo superkeys that contain `exec` actions cannot reuse the current
  device-mapping cleanup path unchanged. `clear_combo_exec_refs()` only clears
  `combo_exec_refs`, while serialized superkeys currently allocate refs through
  `superkey_exec_refs`.

## Phase 0: Plumbing And Guardrails

Goal:
Allow combo actions to carry serialized superkey configs to the daemon without
turning on all runtime paths at once.

Files:

- `keyforge/session/manager/payloads.py`
- `keyforge/gui/widgets/combo_editor_dialog.py`
- `docs/COMBOS.md`
- `docs/COMBO_SUPERKEYS.md`
- tests in `tests/test_session_manager_profiles.py` or nearby payload tests

Deliverables:

- combo payload signatures include referenced superkeys
- combo runtime payloads include serialized superkeys
- combo editor can distinguish unsupported versus supported superkey modes
- combo/superkey exec refs have an explicit cleanup owner

TODO:

- [x] decide ref ownership for combo superkeys with `exec` children
- [x] add combo superkey payload support in `combo_action_signature_payload()`
- [x] add combo superkey payload support in `combo_action_to_payload()`
- [x] add tests proving combo signatures change when a referenced superkey changes
- [x] add tests proving combo payloads include serialized superkeys
- [x] keep the GUI blocked for `Pattern` until later phases

## Phase 1: Overload Runtime Support

Goal:
Make combo actions run `Overload` superkeys using the combo press/release
lifecycle.

Files:

- `keyforge/keyforged/runtime/combos.py`
- possibly shared helpers in `keyforge/keyforged/runtime/grabbed_device_actions.py`
- `keyforge/keyforged/device_manager.py`
- tests in `tests/test_device_manager.py`

Preferred semantics:

- combo completion starts overload children in list order
- combo release stops held children in reverse order
- one-shot children fire only on combo press
- child rapidfire/tap behavior keeps current action semantics
- nested superkeys stay disallowed

Implementation options:

- Option A: extract shared fanout helpers for overload execution
- Option B: implement combo-local overload fanout in `runtime/combos.py`

Default choice:

- Option B first, because combo runtime already owns combo-specific action
  state and stop behavior

TODO:

- [x] choose combo-local versus shared overload helper strategy
- [x] replace the `SUPERKEY` early return in `start_combo_action()`
- [x] represent child combo action state cleanly
- [x] ensure `stop_combo_action()` releases overload children correctly
- [x] ensure combo runtime reset clears active overload children
- [x] add tests for overload key/button press and release
- [x] add tests for overload mixed child bundles
- [x] add tests for overload with `exec`, macro, and profile child actions
- [x] add tests for cleanup on device teardown and combo runtime reset

## Phase 2: GUI Enablement For Overload

Goal:
Expose `Overload` combo actions in the editor once runtime support is solid.

Files:

- `keyforge/gui/widgets/combo_editor_dialog.py`
- `keyforge/gui/widgets/key_selector_dialog.py`
- possibly `keyforge/gui/widgets/action_labels.py`
- tests in `tests/test_gui.py` or widget-specific tests

UX rules:

- allow selecting saved superkeys in combo actions
- allow `Pattern` too once runtime support lands
- label superkeys clearly in combo summaries

TODO:

- [x] allow superkey combo actions in the editor when mode is `overload`
- [x] keep `pattern` superkeys blocked with a clear message
- [x] update combo action summary labels if needed
- [x] add GUI validation tests for overload-only support

## Phase 3: Pattern Support For Single-Step Combos

Goal:
Support pattern superkeys on single-step combos.

Files:

- likely `keyforge/keyforged/runtime/combos.py`
- possibly a new combo-specific pattern helper module
- tests in `tests/test_device_manager.py` and `tests/test_combo_engine.py`

Recommended semantics:

- combo matcher still recognizes the trigger
- after completion, combo runtime decides between pattern outcomes
- `Tap` and `Hold` are required
- `Double Tap` and `Tap + Hold` are optional for single-step only

Design guidance:

- prefer a combo-specific pattern runner over forcing full reuse of
  `SuperkeyMachine`
- keep combo-engine matching separate from pattern timing logic

TODO:

- [x] decide whether to support single-step `Double Tap`
- [x] decide whether to support single-step `Tap + Hold`
- [x] implement single-step `Tap`
- [x] implement single-step `Hold`
- [x] add timing tests for single-step tap versus hold
- [x] add tests for interaction with combo release and reset paths

## Phase 4: Pattern Support For Multi-Step Combos

Goal:
Add limited pattern support for multi-step combos without introducing ambiguous
gesture ownership.

Scope for this phase:

- support `Tap`
- support `Hold`
- do not support `Double Tap`
- do not support `Tap + Hold`

Files:

- `keyforge/keyforged/runtime/combos.py`
- tests in `tests/test_device_manager.py` and integration tests
- docs in `docs/COMBOS.md` and `docs/SUPERKEYS.md`

TODO:

- [x] define exact semantics for multi-step tap versus hold
- [x] ensure final-step hold timing does not conflict with step timeouts
- [x] implement multi-step `Tap`
- [x] implement multi-step `Hold`
- [x] add tests for multi-step timeout plus pattern timing interaction
- [x] document unsupported multi-step pattern modes in the GUI and docs

## Cross-Cutting Test List

- [x] payload/signature tests for combo superkeys
- [x] combo parser tests for embedded superkey configs
- [x] overload combo runtime tests
- [x] combo reset and teardown cleanup tests
- [x] single-step pattern timing tests
- [x] multi-step pattern timing tests
- [x] GUI validation tests

## Out Of Scope For First Delivery

- [ ] nested superkeys anywhere inside combo-triggered superkeys
- [ ] multi-step `Double Tap`
- [ ] multi-step `Tap + Hold`
- [ ] broader combo matcher redesign

## Suggested Delivery Order

1. Phase 0
2. Phase 1
3. Phase 2
4. stop and validate whether `Overload` alone solves enough of the problem
5. Phase 3
6. Phase 4
