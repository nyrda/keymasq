"""Per-source queues for action lists that wait on a natural cursor move."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from functools import partial
from typing import Any

from keymasq.common.model.core import ActionType

type FireAndObserve = Callable[[Awaitable[object], str], asyncio.Task[object]]
type SequenceStep = Callable[[], Coroutine[Any, Any, object]]

_NATURAL_MOVE_TYPES = (
    ActionType.MOUSE_MOVE_NATURAL_ABS,
    ActionType.MOUSE_MOVE_NATURAL_ABS.value,
)


def runs_natural_move(*action_lists: Iterable[object]) -> bool:
    return any(
        getattr(action, "action_type", None) in _NATURAL_MOVE_TYPES
        for actions in action_lists
        for action in actions
    )


class ActionSequences:
    """Run each source's action lists in order, off the caller once one waits on a move.

    A step runs inline while its source has nothing queued and ``detach`` is
    false. Otherwise it becomes a tracked task that starts after the source's
    previous step. Detached steps start eagerly, so a natural move joins the
    cursor lock queue at press time.
    """

    def __init__(self) -> None:
        self._lanes: dict[str, list[asyncio.Task[object]]] = {}
        self._owners: dict[str, object] = {}

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(self._lanes)

    def pending(self, key: str) -> bool:
        lane = self._lanes.get(key)
        return lane is not None and _pending_tail(lane) is not None

    def run(
        self,
        key: str,
        step: SequenceStep,
        *,
        detach: bool,
        fire_and_observe_fn: FireAndObserve,
        label: str,
        owner: object | None = None,
    ) -> bool:
        lane = self._lanes.get(key)
        if lane is None:
            if not detach:
                return False
            previous = None
        else:
            current = asyncio.current_task()
            if current is not None and current in lane:
                return False
            previous = _pending_tail(lane)
            if previous is None and not detach:
                return False
        task = fire_and_observe_fn(
            asyncio.eager_task_factory(
                asyncio.get_running_loop(),
                _run_after(previous, step),
            ),
            label,
        )
        if not task.done():
            self._lanes.setdefault(key, []).append(task)
            if owner is not None:
                self._owners[key] = owner
            task.add_done_callback(partial(self._forget, key))
        return True

    def cancel(self, key: str) -> None:
        self._owners.pop(key, None)
        for task in self._lanes.pop(key, ()):
            task.cancel()

    def cancel_all(self) -> None:
        for key in list(self._lanes):
            self.cancel(key)

    def cancel_owned_by(self, owners: Iterable[object]) -> None:
        retired = {id(owner) for owner in owners}
        for key, owner in list(self._owners.items()):
            if id(owner) in retired:
                self.cancel(key)

    def _forget(self, key: str, task: asyncio.Task[object]) -> None:
        lane = self._lanes.get(key)
        if lane is None:
            return
        with contextlib.suppress(ValueError):
            lane.remove(task)
        if not lane:
            del self._lanes[key]
            self._owners.pop(key, None)


def _pending_tail(lane: list[asyncio.Task[object]]) -> asyncio.Task[object] | None:
    tail = lane[-1] if lane else None
    if tail is None or tail.done():
        return None
    return tail


async def _run_after(previous: asyncio.Task[object] | None, step: SequenceStep) -> None:
    if previous is not None:
        await asyncio.wait((previous,))
    await step()
