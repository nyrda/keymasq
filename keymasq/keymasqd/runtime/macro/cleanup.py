from __future__ import annotations

import contextlib
from collections.abc import Callable
from functools import partial
from typing import Any

from keymasq.keymasqd.runtime.macro import controls, mouse, outputs
from keymasq.keymasqd.runtime.macro.loops import running_macro_instance_ids
from keymasq.keymasqd.runtime.macro.state import MacroRuntimeDeps

type MacroManager = Any
type ReleaseHeldOutputs = Callable[..., None]
type EndMouseSuppression = Callable[..., None]


def expire_paused_instance(
    manager: MacroManager, expired_id: int, *, deps: MacroRuntimeDeps
) -> None:
    """Cancel the expired instance and its descendants, leaving ancestors and siblings alone."""
    state = manager.macro_state
    if expired_id in state.cancel_instance_ids or expired_id not in state.instance_meta:
        return
    ids = state.descendant_instance_ids([expired_id])
    state.cancel_instance_ids.update(ids)
    for instance_id in ids:
        pause = state.pauses.get(instance_id)
        if pause is not None:
            pause.clear_timeout()
        outputs.release_macro_held_for_instance(manager, instance_id, deps=deps)
        task = state.tasks.get(instance_id)
        if task is not None and not task.done():
            task.cancel()
            # Also reap instances cancelled before their scheduler starts.
            task.add_done_callback(partial(_forget_cancelled_instance, state, instance_id))
        else:
            state.forget_instance(instance_id)
    deps.log.debug("Discarded paused macro instance %s after its timeout", expired_id)


def _forget_cancelled_instance(state: Any, instance_id: int, _task: object) -> None:
    state.forget_instance(instance_id)


async def cancel_macro_instances(
    manager: MacroManager,
    instance_ids: list[int],
    *,
    deps: MacroRuntimeDeps,
    release_held_fn: ReleaseHeldOutputs = outputs.release_macro_held_for_instance,
) -> int:
    """Cancel selected tasks after synchronously neutralizing their outputs."""

    asyncio_mod = deps.asyncio_mod
    unique_ids = manager.macro_state.descendant_instance_ids(instance_ids)
    if not unique_ids:
        return 0

    state = manager.macro_state
    state.cancel_instance_ids.update(unique_ids)
    for instance_id in unique_ids:
        release_held_fn(manager, instance_id, deps=deps)

    tasks = [
        task
        for instance_id, task in state.tasks.items()
        if instance_id in unique_ids and not task.done()
    ]
    for task in tasks:
        task.cancel()

    if tasks:
        with contextlib.suppress(Exception):
            await asyncio_mod.wait_for(
                asyncio_mod.gather(*tasks, return_exceptions=True),
                timeout=1.0,
            )

    for instance_id in unique_ids:
        state.forget_instance(instance_id)
    return len(tasks)


async def cancel_macro_playback(
    manager: MacroManager,
    *,
    deps: MacroRuntimeDeps,
    cancel_instances_fn: Callable[..., Any] = cancel_macro_instances,
    end_suppression_fn: EndMouseSuppression = mouse.end_mouse_rel_suppression,
) -> dict[str, object]:
    """Cancel every running macro and reset shared macro-owned resources."""

    running_ids = running_macro_instance_ids(manager.macro_state)
    cancelled = await cancel_instances_fn(manager, running_ids, deps=deps)
    controls.complete_all_macro_exec_waiters(manager, -1)
    manager.macro_state.mouse_inhibit_count = 0
    end_suppression_fn(manager)
    return {"status": "ok", "cancelled": cancelled > 0}
