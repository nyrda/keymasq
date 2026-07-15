from __future__ import annotations

import contextlib
import random
import uuid
from collections.abc import Callable
from typing import Any

from keymasq.common.ipc import CommandType
from keymasq.keymasqd.runtime.macro import mouse
from keymasq.keymasqd.runtime.macro.state import MacroRuntimeDeps

type MacroManager = Any
type MouseSuppressionAction = Callable[..., None]


def complete_all_macro_exec_waiters(manager: MacroManager, returncode: int) -> None:
    for wait_id, waiter in list(manager.macro_state.exec_waiters.items()):
        if waiter.done():
            manager.macro_state.exec_waiters.pop(wait_id, None)
            continue
        waiter.set_result(int(returncode))


def complete_macro_exec_wait(
    manager: MacroManager,
    wait_id: str,
    returncode: int,
) -> dict[str, object]:
    wait_key = str(wait_id or "").strip()
    if not wait_key:
        return {"status": "error", "message": "missing wait_id"}

    waiter = manager.macro_state.exec_waiters.get(wait_key)
    if waiter and not waiter.done():
        waiter.set_result(int(returncode))
        return {"status": "ok", "matched": True}
    return {"status": "ok", "matched": False}


async def run_macro_control_action(
    manager: MacroManager,
    event: dict[str, object],
    *,
    renew_mouse_suppression: bool = False,
    deps: MacroRuntimeDeps,
    acquire_mouse_inhibit_fn: MouseSuppressionAction | None = None,
    release_mouse_inhibit_fn: MouseSuppressionAction | None = None,
    renew_mouse_suppression_fn: MouseSuppressionAction | None = None,
) -> float:
    """Execute one semantic control event and return its blocking duration."""

    str_value_fn = deps.str_value_fn
    int_value_fn = deps.int_value_fn
    acquire_mouse_inhibit = acquire_mouse_inhibit_fn or mouse.acquire_macro_mouse_inhibit
    release_mouse_inhibit = release_mouse_inhibit_fn or mouse.release_macro_mouse_inhibit
    renew_mouse_suppression_action = (
        renew_mouse_suppression_fn or mouse.renew_macro_mouse_suppression
    )
    action_type = str_value_fn(event.get("macro_action"), "")
    if action_type == "wait":
        duration_us = max(0, int_value_fn(event.get("duration_us"), 0))
        return await _sleep_control_action(
            manager,
            duration_us,
            renew_mouse_suppression=renew_mouse_suppression,
            deps=deps,
            renew_mouse_suppression_fn=renew_mouse_suppression_action,
        )

    if action_type == "wait_random":
        min_us = max(0, int_value_fn(event.get("min_us"), 0))
        max_us = max(min_us, int_value_fn(event.get("max_us"), min_us))
        return await _sleep_control_action(
            manager,
            random.randint(min_us, max_us),
            renew_mouse_suppression=renew_mouse_suppression,
            deps=deps,
            renew_mouse_suppression_fn=renew_mouse_suppression_action,
        )

    if action_type == "exec_async":
        command = str_value_fn(event.get("command"), "").strip()
        if command and manager.broadcast_callback:
            await manager.broadcast_callback(
                CommandType.ACTION_TRIGGER,
                {
                    "action_type": "exec",
                    "cmd": command,
                    "macro_exec_async": True,
                },
            )
        return 0.0

    if action_type == "exec_sync":
        return await _run_exec_sync(
            manager,
            event,
            renew_mouse_suppression=renew_mouse_suppression,
            deps=deps,
            acquire_mouse_inhibit_fn=acquire_mouse_inhibit,
            release_mouse_inhibit_fn=release_mouse_inhibit,
            renew_mouse_suppression_fn=renew_mouse_suppression_action,
        )

    if action_type == "compositor_dispatch":
        dispatcher = str_value_fn(event.get("dispatcher"), "").strip()
        if dispatcher and manager.broadcast_callback:
            await manager.broadcast_callback(
                CommandType.ACTION_TRIGGER,
                {
                    "action_type": "compositor_dispatch",
                    "compositor": str_value_fn(event.get("compositor"), "").strip(),
                    "dispatcher": dispatcher,
                    "args": str_value_fn(event.get("args"), "").strip(),
                },
            )
        return 0.0

    return 0.0


async def _sleep_control_action(
    manager: MacroManager,
    duration_us: int,
    *,
    renew_mouse_suppression: bool,
    deps: MacroRuntimeDeps,
    renew_mouse_suppression_fn: MouseSuppressionAction,
) -> float:
    if duration_us <= 0:
        return 0.0
    duration_s = duration_us / 1_000_000.0
    if renew_mouse_suppression:
        renew_mouse_suppression_fn(
            manager,
            timeout_s=duration_s + 1.0,
            deps=deps,
        )
    loop = deps.asyncio_mod.get_running_loop()
    started_at = loop.time()
    await deps.asyncio_mod.sleep(duration_s)
    return max(0.0, loop.time() - started_at)


async def _run_exec_sync(
    manager: MacroManager,
    event: dict[str, object],
    *,
    renew_mouse_suppression: bool,
    deps: MacroRuntimeDeps,
    acquire_mouse_inhibit_fn: MouseSuppressionAction,
    release_mouse_inhibit_fn: MouseSuppressionAction,
    renew_mouse_suppression_fn: MouseSuppressionAction,
) -> float:
    asyncio_mod = deps.asyncio_mod
    command = deps.str_value_fn(event.get("command"), "").strip()
    if not command:
        return 0.0

    timeout_ms = max(1, deps.int_value_fn(event.get("timeout_ms"), 30000))
    timeout_limit = getattr(manager, "macro_exec_timeout_max_ms", None)
    if timeout_limit is not None:
        with contextlib.suppress(TypeError, ValueError):
            timeout_ms = min(timeout_ms, max(1, int(timeout_limit)))
    inhibit_mouse = bool(event.get("inhibit_mouse", False))
    loop = asyncio_mod.get_running_loop()
    started_at = loop.time()
    if inhibit_mouse:
        acquire_mouse_inhibit_fn(
            manager,
            timeout_s=max(1.0, timeout_ms / 1000.0 + 1.0),
            deps=deps,
        )
    elif renew_mouse_suppression:
        renew_mouse_suppression_fn(
            manager,
            timeout_s=timeout_ms / 1000.0 + 1.0,
            deps=deps,
        )

    wait_id = uuid.uuid4().hex
    try:
        waiter = loop.create_future()
        manager.macro_state.exec_waiters[wait_id] = waiter

        if manager.broadcast_callback:
            await manager.broadcast_callback(
                CommandType.ACTION_TRIGGER,
                {
                    "action_type": "exec",
                    "cmd": command,
                    "macro_exec_wait_id": wait_id,
                    "macro_exec_timeout_ms": timeout_ms,
                },
            )
            with contextlib.suppress(asyncio_mod.TimeoutError):
                await asyncio_mod.wait_for(
                    waiter,
                    timeout=max(0.1, timeout_ms / 1000.0),
                )
    finally:
        manager.macro_state.exec_waiters.pop(wait_id, None)
        if inhibit_mouse:
            release_mouse_inhibit_fn(manager)
    return max(0.0, loop.time() - started_at)
