# Session Manager Refactor Plan

`keyforge/session/manager.py` is currently a single 3,420-line class that owns too many unrelated concerns. The result is a "god object" with high state coupling, duplicated serialization logic, and a command/event dispatch path that is hard to reason about safely.

This document proposes a split that improves maintainability and testability without changing runtime behavior.

## Current Problems

The file currently combines all of the following:

- Unix session socket lifecycle and per-client I/O
- Session ACL and sensitive-command enforcement
- Request routing for 39 session commands
- Daemon connection/reconnect logic
- Compositor detection, listener lifecycle, and window tracking
- Profile reevaluation and device grab/release/update logic
- Combo payload/signature generation
- Exec-ref allocation for device mappings, combos, and superkeys
- Recording unlock ownership and refresh lease handling
- Capture begin/read/end and combo capture
- Recording settings persistence and device selection
- Macro CRUD/playback helpers
- Notification helpers

The largest hotspots are:

- [`_handle_session_request`](../keyforge/session/manager.py) in [manager.py](/home/daniel/dev/keyforge/keyforge2/keyforge/session/manager.py:388), 446 lines
- [`_apply_resolved_device_profile`](../keyforge/session/manager.py) in [manager.py](/home/daniel/dev/keyforge/keyforge2/keyforge/session/manager.py:2138), 174 lines
- [`_start_recording`](../keyforge/session/manager.py) in [manager.py](/home/daniel/dev/keyforge/keyforge2/keyforge/session/manager.py:3135), 109 lines
- duplicated action serialization in [manager.py](/home/daniel/dev/keyforge/keyforge2/keyforge/session/manager.py:2520), [manager.py](/home/daniel/dev/keyforge/keyforge2/keyforge/session/manager.py:2699), and [manager.py](/home/daniel/dev/keyforge/keyforge2/keyforge/session/manager.py:2808)

The structural issue is not just size. The main problem is that unrelated state lives on the same object and is mutated across domains:

- request/router state
- compositor state
- device runtime state
- recording/capture state
- macro/edit state
- exec-ref bookkeeping

That makes local changes risky because invariants are spread across the whole class.

## Refactor Goals

- Keep `keyforge.session.manager.SessionManager` as the stable facade and process entrypoint.
- Preserve all request names, response payloads, retry timings, notifications, and security semantics.
- Reduce mutable shared state by moving ownership into focused controller objects.
- Replace large `if command == ...` chains with explicit command tables.
- Move payload/signature generation into pure or near-pure helpers.
- Make extracted pieces independently unit-testable.

## Recommended Split

Do not use mixins. Use composition with a thin `SessionManager` facade.

Recommended target layout:

- `keyforge/session/manager.py`
  - keeps `SessionManager`, `main()`, top-level wiring, and process lifecycle
- `keyforge/session/session_server.py`
  - Unix socket startup/shutdown
  - client registry
  - read loop, buffered request parsing, response writes
  - broadcast helper
- `keyforge/session/request_router.py`
  - ACL check and sensitive-command gate
  - command dispatch table
  - grouped request handlers that delegate to controllers/services
- `keyforge/session/compositor_runtime.py`
  - compositor supervisor loop
  - compositor detection/switching
  - listener start/stop/retry state
  - active-window query/normalization
  - compositor dispatch trigger handling
- `keyforge/session/device_runtime.py`
  - profile reevaluation
  - device activation/deactivation
  - mapping/combo pushes to `keyforged`
  - topology refresh and grab-retry handling
  - device connect/disconnect events
- `keyforge/session/recording_runtime.py`
  - recording unlock ownership and refresh flow
  - capture begin/read/end
  - combo capture
  - recording settings persistence
  - recording device selection/cache
  - start/stop/save/discard recording
- `keyforge/session/macro_service.py`
  - macro CRUD/playback helpers
  - policy sanitization for macro exec timeouts
- `keyforge/session/mapping_payloads.py`
  - mapping payload generation
  - combo payload generation
  - action/signature serialization
  - superkey serialization
  - exec-ref allocation hooks

This keeps the split aligned with current behavior boundaries instead of creating a new abstraction layer that the codebase does not need.

## Ownership Rules

Each extracted module should own its own mutable state.

`SessionServer` owns:

- session socket server
- connected client writers
- peer credential map

`CompositorRuntime` owns:

- `_window_listener`
- `_compositor_id`
- compositor candidate/hit counters
- listener retry/backoff bookkeeping
- `_current_window`

`DeviceRuntime` owns:

- `_grabbed_devices`
- `_grabbed_interfaces`
- `_grab_waiting_devices`
- `_grab_retry_tasks`
- `_topology_refresh_task`
- `_active_profile_names`
- `_resolved_devices`
- `_last_sent_mapping_signatures`
- `_last_sent_combo_signature`

`RecordingRuntime` owns:

- `_capture_locks`
- `_capture_resume_profiles`
- `_capture_tokens`
- `_recording_active`
- `_pending_recording_data`
- `_recording_start_cursor`
- `_recording_settings`
- `_recording_settings_pending_save`
- `_recording_settings_save_task`
- `_recording_devices_cache`
- `_recording_refresh_owner`
- `_runtime_refresh_claim_consumed_until`

`MappingPayloads` or an `ExecRefRegistry` helper owns:

- `_exec_refs`
- `_next_exec_ref`
- `_device_exec_refs`
- `_combo_exec_refs`
- `_superkey_exec_refs`
- `_next_superkey_exec_ref`

This matters because the current constructor is large mostly due to state that belongs to different subsystems.

## Request Router Shape

The current request handler is the biggest single refactor target.

Replace the long chain in [manager.py](/home/daniel/dev/keyforge/keyforge2/keyforge/session/manager.py:388) with a dispatch table:

```python
class SessionRequestRouter:
    async def handle(
        self,
        request: JsonObject,
        client_class: str,
        peer: PeerCredentials,
        writer: asyncio.StreamWriter,
    ) -> JsonObject:
        command = _str_value(request.get("command"), "")
        policy = self._policy()

        if not command_allowed(command, policy.session_command_acl, client_class):
            ...
        if self._is_sensitive(command, policy) and not self._is_refresh_owner(peer, writer):
            ...

        handler = self._handlers.get(command)
        if handler is None:
            return {"error": f"Unknown command: {command}"}
        return await handler(request, peer, writer)
```

Group handlers by domain:

- profile/status commands
- compositor/window commands
- recording/unlock/capture commands
- macro commands
- diagnostics/admin commands

Important guardrail: preserve the current single-policy-snapshot behavior. `policy = self._policy()` must remain a single snapshot for both ACL and sensitivity checks because tests currently assert this.

## Device Runtime Split

The device side is the second major hotspot. Keep these together in `device_runtime.py`:

- `_reevaluate_profiles`
- `_apply_resolved_device_profile`
- `_update_mapping`
- `_update_combos`
- `_deactivate_profile`
- `_get_interfaces_to_grab`
- `_schedule_topology_refresh`
- `_handle_device_grab_status_event`
- `_on_device_connected`
- `_on_device_disconnected`

Why keep them together:

- they share the same state
- they are driven by the same daemon connection lifecycle
- they are the most timing-sensitive part of the file

Do not split mapping updates, combo updates, and grab retry logic across different classes. Those operations form one runtime synchronization domain.

## Recording Runtime Split

Recording/capture/unlock logic should move as one cohesive unit even though it is large.

Keep together:

- sensitive-command ownership helpers
- refresh-lease claim/refresh/lock flow
- capture begin/read/end
- combo capture
- recording settings load/save
- recording device discovery/cache
- start/stop/save recording

Why:

- these methods share state and user-visible security behavior
- they already behave as one subsystem from the GUI point of view
- splitting unlock flow away from recording flow would create needless cross-controller chatter

One explicit non-goal for the refactor: do not "fix" behavior while moving code. For example, recording-settings persistence currently has existing semantics; preserve them during extraction and handle any functional change separately under dedicated tests.

## Mapping Payload Split

The payload/signature builders are a good extraction target because they are mostly deterministic transforms with one source of impurity: exec-ref allocation.

Move these into `mapping_payloads.py`:

- `_resolved_mapping_signature`
- `_resolved_combos_signature`
- `_action_signature_payload`
- `_combo_action_signature_payload`
- `_profile_to_mapping`
- `_resolved_combos_payload`
- `_combo_action_to_payload`
- `_serialize_superkey`
- `_serialize_superkey_signature`
- `_serialize_superkey_action`
- `_serialize_superkey_action_signature`

Recommended structure:

- `ExecRefRegistry`
  - allocates and clears device/combo/superkey exec refs
- `MappingPayloadBuilder`
  - depends on `SuperkeyManager`
  - uses `ExecRefRegistry`
  - exposes `build_mapping`, `build_combos`, `mapping_signature`, `combo_signature`

This removes a large amount of duplication from `SessionManager` and makes serialization testable without daemon or socket setup.

## What Stays In SessionManager

The end-state `SessionManager` should stay as the orchestration facade:

- dependency construction
- `start()` / `stop()`
- signal/reload hooks
- daemon connect loop
- event fan-out from `KeyforgedClient`
- light wrapper methods only where compatibility is useful

Target size should be roughly 200-400 lines, not because of a hard rule, but because that is enough to keep it as a coordinator rather than a feature bucket.

## Migration Strategy

Use incremental extraction, not a big-bang rewrite.

### Phase 1: Pure helpers first

- extract JSON/value helper functions if desired
- extract `ExecRefRegistry`
- extract `MappingPayloadBuilder`
- keep `SessionManager` methods as wrappers that call the new helper

This is the safest first step because behavior is deterministic and well covered by tests.

### Phase 2: Device runtime

- move profile reevaluation and grab/update/release code into `DeviceRuntime`
- keep `SessionManager._reevaluate_profiles()` and related methods as forwarding wrappers initially
- preserve notification timing and log messages

### Phase 3: Recording runtime

- move unlock ownership, capture, recording, and settings persistence into `RecordingRuntime`
- keep wrapper methods on `SessionManager` so existing tests keep patching the same names

### Phase 4: Request router and session server

- move session socket/client code to `SessionServer`
- replace `_handle_session_request` chain with router table
- keep `SessionManager._handle_session_request()` as a one-line delegate until tests are updated

### Phase 5: Compositor runtime

- move compositor supervisor and listener lifecycle into `CompositorRuntime`
- keep `SessionManager.on_window_change()` delegating to compositor/device runtime

This order minimizes risk because the biggest test surface stays on `SessionManager` while implementation moves behind it.

## Compatibility Strategy

The current tests in [tests/test_session_manager_stability.py](/home/daniel/dev/keyforge/keyforge2/tests/test_session_manager_stability.py:1) patch many `SessionManager` internals directly. That strongly argues for a facade-first refactor.

Recommended approach during migration:

- keep the same `SessionManager` method names first
- convert them into thin wrappers one domain at a time
- only migrate tests to target subcontrollers after the wrapper layer is stable

That keeps behavior constant and avoids turning the refactor into a test rewrite.

## Behavior-Preservation Checklist

The refactor must preserve:

- exact session command names and response shapes
- sensitive-command gating semantics
- single-policy-snapshot ACL behavior
- active-profile ordering semantics
- combo prefix-shadowing behavior
- capture lock behavior during reevaluation
- daemon reconnect behavior and reset of grabbed/runtime state
- current grab retry timing and topology refresh debounce timing
- notification conditions and wording unless intentionally changed later
- exec-ref allocation/cleanup semantics for device mappings, combos, and superkeys
- compositor degraded-mode retry behavior

## Test Plan

Keep the existing session-manager stability tests as the regression harness.

Add focused unit tests for extracted modules:

- `tests/test_session_mapping_payloads.py`
- `tests/test_session_request_router.py`
- `tests/test_session_device_runtime.py`
- `tests/test_session_recording_runtime.py`

The rule should be:

- existing `SessionManager` tests prove no behavior regression
- new controller/helper tests prove the split is easier to reason about

## Recommendation Summary

The clean split is not "many partial files for one giant class". The clean split is:

1. keep `SessionManager` as a stable facade
2. move stateful domains into composed controller objects
3. extract payload generation into dedicated helpers
4. replace the command chain with an explicit router table
5. migrate in phases while preserving the current method surface

That gives smaller files, clearer ownership, and better tests without changing user-visible behavior.
