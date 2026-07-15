"""Generation-controlled scheduling for profile reconciliation."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from ..state import ProfileRuntimeState

if TYPE_CHECKING:
    from ..core import SessionManager

log = logging.getLogger("keymasq-session")
type ReconcileGeneration = Callable[["SessionManager", int, str], Awaitable[None]]
type ReevaluateProfiles = Callable[[str], Awaitable[None]]
type InvalidateRuntimeState = Callable[[], None]


@dataclass(slots=True)
class ReconciliationGenerationState:
    """Explicit state machine for superseding asynchronous profile applies.

    The state object is intentionally independent from ``SessionManager`` so
    cancellation and stale-generation behavior can be tested in isolation.
    """

    state: ProfileRuntimeState

    def begin(self, reason: str, *, current: asyncio.Task[object] | None) -> int:
        """Start a generation and cancel a previous in-flight apply."""
        self.state.apply_generation += 1
        generation = self.state.apply_generation
        previous = self.state.apply_task
        if previous is not None and previous is not current and not previous.done():
            previous.cancel()
        self.state.apply_reason = reason
        return generation

    def track(self, task: asyncio.Task[None]) -> None:
        self.state.apply_task = task

    def clear_if_current(self, task: asyncio.Task[None]) -> None:
        if self.state.apply_task is task:
            self.state.apply_task = None

    def is_current(self, generation: int | None) -> bool:
        return generation is None or generation == self.state.apply_generation

    def ensure_current(self, generation: int | None) -> None:
        if not self.is_current(generation):
            raise asyncio.CancelledError


async def request_profile_reevaluation(
    manager: "SessionManager",
    *,
    reason: str,
    wait: bool,
    reconcile: ReconcileGeneration,
) -> asyncio.Task[None]:
    """Schedule a newest-wins profile apply and optionally await its successor."""
    generations = ReconciliationGenerationState(manager.profile_state)
    generation = generations.begin(
        reason,
        current=cast(asyncio.Task[object] | None, asyncio.current_task()),
    )
    task = asyncio.create_task(_run_generation(manager, generation, reason, reconcile=reconcile))
    generations.track(task)
    task.add_done_callback(generations.clear_if_current)

    if wait:
        await _await_profile_apply_task(manager, task, generation)
        while not generations.is_current(generation):
            latest = cast(
                asyncio.Task[None] | None,
                manager.profile_state.__dict__.get("apply_task"),
            )
            if latest is None or latest is task:
                break
            task = latest
            generation = manager.profile_state.apply_generation
            await _await_profile_apply_task(manager, task, generation)
    return task


async def _run_generation(
    manager: "SessionManager",
    generation: int,
    reason: str,
    *,
    reconcile: ReconcileGeneration,
) -> None:
    try:
        await reconcile(manager, generation, reason)
    except asyncio.CancelledError:
        if manager.verbosity >= 1:
            log.debug(
                "Profile reevaluation interrupted: generation=%s reason=%s",
                generation,
                reason,
            )
    except Exception:
        log.exception(
            "Profile reevaluation failed: generation=%s reason=%s",
            generation,
            reason,
        )
        raise


async def _await_profile_apply_task(
    manager: "SessionManager",
    task: asyncio.Task[None],
    generation: int,
) -> None:
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # Stale apply tasks can be cancelled before their coroutine starts.
        if task.cancelled() and not profile_apply_is_current(manager, generation):
            return
        raise


def profile_apply_is_current(
    manager: "SessionManager",
    generation: int | None,
) -> bool:
    return ReconciliationGenerationState(manager.profile_state).is_current(generation)


def raise_if_stale_profile_apply(
    manager: "SessionManager",
    generation: int | None,
) -> None:
    ReconciliationGenerationState(manager.profile_state).ensure_current(generation)


def schedule_topology_refresh(
    manager: "SessionManager",
    debounce_s: float,
    retry_s: float,
    *,
    invalidate: InvalidateRuntimeState,
    reevaluate: ReevaluateProfiles,
) -> None:
    """Debounce topology changes and retry failed runtime reconciliation."""
    existing = manager.profile_state.topology_refresh_task
    if existing is not None and not existing.done():
        existing.cancel()

    async def _run() -> None:
        try:
            delay = debounce_s
            while True:
                await asyncio.sleep(delay)
                try:
                    invalidate()
                    await reevaluate("topology refresh")
                    return
                except asyncio.CancelledError:
                    raise
                except OSError as exc:
                    log.warning("Topology refresh failed: %s", exc)
                    delay = retry_s
                except Exception:
                    log.exception("Unexpected topology refresh failure")
                    delay = retry_s
        except asyncio.CancelledError:
            raise
        finally:
            task = manager.profile_state.topology_refresh_task
            if task is asyncio.current_task():
                manager.profile_state.topology_refresh_task = None

    manager.profile_state.topology_refresh_task = asyncio.create_task(_run())
