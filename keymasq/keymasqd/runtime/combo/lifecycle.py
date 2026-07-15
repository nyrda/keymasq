"""Topology cleanup and timeout lifecycle for the combo runtime."""

import contextlib
import time

from keymasq.keymasqd.runtime.combo.actions import stop_combo_action
from keymasq.keymasqd.runtime.combo.recall import binding_matches_scope
from keymasq.keymasqd.runtime.combo.state import ComboManager, ComboRuntimeDeps


async def clear_combo_runtime(
    manager: ComboManager,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    async with manager.combo_state.runtime_lock:
        await clear_combo_runtime_unlocked(manager, deps=deps)


async def clear_combo_runtime_unlocked(
    manager: ComboManager,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    manager.combo_state.progression.engine.reset()
    for combo_id in list(manager.combo_state.active_actions):
        await stop_combo_action(manager, combo_id, deps=deps)
    # Active actions perform their key-up transition. Cached machines still need
    # explicit teardown so their timers cannot outlive a runtime reset.
    machines = list(manager.combo_state.superkey_machines.values())
    manager.combo_state.superkey_machines.clear()
    manager.combo_state.superkey_machine_bindings.clear()
    for machine in machines:
        await machine.stop()
    _clear_tracked_outputs(manager)
    await _cancel_timeout_watchdog(manager, deps=deps)


async def clear_combo_runtime_except(
    manager: ComboManager,
    preserve_combo_ids: set[str],
    *,
    deps: ComboRuntimeDeps,
) -> None:
    async with manager.combo_state.runtime_lock:
        await clear_combo_runtime_except_unlocked(
            manager,
            preserve_combo_ids,
            deps=deps,
        )


async def clear_combo_runtime_except_unlocked(
    manager: ComboManager,
    preserve_combo_ids: set[str],
    *,
    deps: ComboRuntimeDeps,
) -> None:
    preserved = set(preserve_combo_ids)
    for combo_id in list(manager.combo_state.active_actions):
        root_combo_id = combo_id.split("#", 1)[0]
        if root_combo_id not in preserved:
            await stop_combo_action(manager, combo_id, deps=deps)

    for combo_id, machine in list(manager.combo_state.superkey_machines.items()):
        if combo_id in preserved:
            continue
        manager.combo_state.superkey_machines.pop(combo_id, None)
        manager.combo_state.superkey_machine_bindings.pop(combo_id, None)
        await machine.stop()

    active_output_roots = {
        combo_id.split("#", 1)[0] for combo_id in manager.combo_state.active_actions
    }
    if not any(root in preserved for root in active_output_roots):
        _clear_tracked_outputs(manager)

    await _cancel_timeout_watchdog(manager, deps=deps)


async def clear_combo_runtime_for_binding_scope(
    manager: ComboManager,
    hardware_id: str,
    source: str | None,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    async with manager.combo_state.runtime_lock:
        await clear_combo_runtime_for_binding_scope_unlocked(
            manager,
            hardware_id,
            source,
            deps=deps,
        )


async def clear_combo_runtime_for_binding_scope_unlocked(
    manager: ComboManager,
    hardware_id: str,
    source: str | None,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    normalized_hardware_id = str(hardware_id or "").lower()
    normalized_source = None if source is None else str(source or "").lower()
    active_combo_ids = manager.combo_state.progression.engine.drop_candidates_for_binding_scope(
        normalized_hardware_id,
        normalized_source,
    )
    for combo_id in active_combo_ids:
        await stop_combo_action(manager, combo_id, deps=deps)

    matching_machine_ids: list[str] = []
    for combo_id in manager.combo_state.superkey_machines:
        state = manager.combo_state.active_actions.get(combo_id)
        bindings = (
            tuple(state.trigger_bindings)
            if state is not None and state.trigger_bindings
            else manager.combo_state.superkey_machine_bindings.get(combo_id, ())
        )
        if any(
            binding_matches_scope(binding, normalized_hardware_id, normalized_source)
            for binding in bindings
        ):
            matching_machine_ids.append(combo_id)
    for combo_id in matching_machine_ids:
        machine = manager.combo_state.superkey_machines.pop(combo_id, None)
        manager.combo_state.superkey_machine_bindings.pop(combo_id, None)
        if machine is not None:
            await machine.stop()
    refresh_combo_timeout_watchdog(manager, deps=deps)


def refresh_combo_timeout_watchdog(
    manager: ComboManager,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    deadline = manager.combo_state.progression.engine.next_deadline()
    if deadline is None:
        task = manager.combo_state.timeout_task
        if task and not task.done():
            task.cancel()
        manager.combo_state.timeout_task = None
        return
    task = manager.combo_state.timeout_task
    if task and not task.done():
        task.cancel()
    manager.combo_state.timeout_task = deps.asyncio_mod.create_task(
        combo_timeout_watchdog(manager, deadline, deps=deps)
    )


async def combo_timeout_watchdog(
    manager: ComboManager,
    deadline: float,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    try:
        await deps.asyncio_mod.sleep(max(0.0, deadline - time.monotonic()))
        async with manager.combo_state.runtime_lock:
            manager.combo_state.progression.engine.expire_timeouts(time.monotonic())
            if manager.combo_state.timeout_task is deps.asyncio_mod.current_task():
                manager.combo_state.timeout_task = None
            refresh_combo_timeout_watchdog(manager, deps=deps)
    except deps.asyncio_mod.CancelledError:
        raise


def _clear_tracked_outputs(manager: ComboManager) -> None:
    for held in manager.combo_state.held_output_keys.values():
        held.clear()
    for refcounts in manager.combo_state.superkey_output_refcounts.values():
        refcounts.clear()


async def _cancel_timeout_watchdog(
    manager: ComboManager,
    *,
    deps: ComboRuntimeDeps,
) -> None:
    task = manager.combo_state.timeout_task
    if task and not task.done():
        task.cancel()
        with contextlib.suppress(deps.asyncio_mod.CancelledError):
            await task
    manager.combo_state.timeout_task = None
