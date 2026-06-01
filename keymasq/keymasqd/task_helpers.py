import asyncio
import contextlib
import logging
from collections.abc import Awaitable

log = logging.getLogger("keymasqd.devices")


def fire_and_observe(coro: Awaitable[object], label: str) -> asyncio.Task[object]:
    task = asyncio.ensure_future(coro)

    def _log_task_result(done: asyncio.Task[object]) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            exc = done.exception()
            if exc is not None:
                log.warning("%s failed: %s", label, exc)

    task.add_done_callback(_log_task_result)
    return task
