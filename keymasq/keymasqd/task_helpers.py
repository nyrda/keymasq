import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable

log = logging.getLogger("keymasqd.devices")

LOOP_SLICE_S = 0.0005


def fire_and_observe(coro: Awaitable[object], label: str) -> asyncio.Task[object]:
    task = asyncio.ensure_future(coro)

    def _log_task_result(done: asyncio.Task[object]) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            exc = done.exception()
            if exc is not None:
                log.warning("%s failed: %s", label, exc)

    task.add_done_callback(_log_task_result)
    return task


class LoopSlice:
    """Bound how long synchronous work keeps grabbed input waiting on the event loop."""

    def __init__(self) -> None:
        self._started = time.perf_counter()

    async def checkpoint(self) -> None:
        if time.perf_counter() - self._started >= LOOP_SLICE_S:
            await asyncio.sleep(0)
            self._started = time.perf_counter()
