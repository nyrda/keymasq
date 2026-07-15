from __future__ import annotations

from collections.abc import Callable
from typing import Any

from keymasq.keymasqd.runtime.macro.state import MacroRuntimeDeps

type MacroManager = Any
type BeginMouseSuppression = Callable[
    [MacroManager, float],
    None,
]
type EndMouseSuppression = Callable[[MacroManager], None]


def acquire_macro_mouse_inhibit(
    manager: MacroManager,
    timeout_s: float,
    *,
    deps: MacroRuntimeDeps,
    begin_suppression_fn: Callable[..., None] | None = None,
) -> None:
    manager.macro_state.mouse_inhibit_count += 1
    begin = begin_suppression_fn or begin_mouse_rel_suppression
    begin(manager, timeout_s=max(0.1, timeout_s), deps=deps)


def release_macro_mouse_inhibit(
    manager: MacroManager,
    *,
    end_suppression_fn: Callable[..., None] | None = None,
) -> None:
    if manager.macro_state.mouse_inhibit_count > 0:
        manager.macro_state.mouse_inhibit_count -= 1
    if manager.macro_state.mouse_inhibit_count == 0:
        end = end_suppression_fn or end_mouse_rel_suppression
        end(manager)


def renew_macro_mouse_suppression(
    manager: MacroManager,
    timeout_s: float,
    *,
    deps: MacroRuntimeDeps,
    begin_suppression_fn: Callable[..., None] | None = None,
) -> None:
    begin = begin_suppression_fn or begin_mouse_rel_suppression
    begin(manager, timeout_s=max(0.1, timeout_s), deps=deps)


def begin_mouse_rel_suppression(
    manager: MacroManager,
    timeout_s: float,
    *,
    deps: MacroRuntimeDeps,
) -> None:
    asyncio_mod = deps.asyncio_mod
    state = manager.macro_state
    state.mouse_rel_suppressed = True
    watchdog = state.mouse_rel_suppression_watchdog_task
    if watchdog and not watchdog.done():
        watchdog.cancel()
    state.mouse_rel_suppression_watchdog_task = asyncio_mod.create_task(
        mouse_rel_suppression_watchdog(manager, timeout_s, deps=deps)
    )


def end_mouse_rel_suppression(manager: MacroManager) -> None:
    state = manager.macro_state
    state.mouse_rel_suppressed = False
    watchdog = state.mouse_rel_suppression_watchdog_task
    if watchdog and not watchdog.done():
        watchdog.cancel()
    state.mouse_rel_suppression_watchdog_task = None


async def mouse_rel_suppression_watchdog(
    manager: MacroManager,
    timeout_s: float,
    *,
    deps: MacroRuntimeDeps,
) -> None:
    asyncio_mod = deps.asyncio_mod
    try:
        await asyncio_mod.sleep(timeout_s)
        if manager.macro_state.mouse_inhibit_count <= 0:
            manager.macro_state.mouse_rel_suppressed = False
    except asyncio_mod.CancelledError:
        pass
