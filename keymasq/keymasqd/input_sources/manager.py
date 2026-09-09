"""One native reader per connection, shared by independent bounded subscribers."""

import asyncio
import contextlib
import errno
import logging
import time
import weakref
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field, replace

from .transports.hidraw import reports
from .types import Binding, InputFrame

log = logging.getLogger("keymasqd.input_sources")


@dataclass(eq=False)
class Subscription:
    queue: asyncio.Queue[InputFrame | OSError] = field(default_factory=lambda: asyncio.Queue(256))
    dropped_frames: int = 0

    async def read(self) -> InputFrame:
        item = await self.queue.get()
        if isinstance(item, OSError):
            raise item
        return item

    def offer(self, item: InputFrame | OSError) -> None:
        if self.queue.full() or isinstance(item, OSError):
            # Discard stale backlog and mark the first retained complete frame.
            while not self.queue.empty():
                self.queue.get_nowait()
                self.dropped_frames += 1
            if isinstance(item, InputFrame):
                item = replace(item, discontinuity=True)
        self.queue.put_nowait(item)


@dataclass
class _Session:
    binding: Binding
    subscribers: set[Subscription] = field(default_factory=set)
    latest: InputFrame | None = None
    task: asyncio.Task[None] | None = None
    error: OSError | None = None


class SourceManager:
    def __init__(self, reader: Callable[[str], AsyncGenerator[bytes]] | None = None) -> None:
        self._reader = reader
        self._sessions: dict[str, _Session] = {}
        self._lifecycle_lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def subscribe(self, binding: Binding) -> AsyncGenerator[Subscription]:
        async with self._lifecycle_lock:
            session = self._sessions.get(binding.path)
            if session is None:
                session = _Session(binding)
                self._sessions[binding.path] = session
            subscriber = Subscription()
            session.subscribers.add(subscriber)
            if session.error is not None:
                subscriber.offer(session.error)
            elif session.latest is not None:
                subscriber.offer(session.latest)
            if session.task is None:
                session.task = asyncio.create_task(self._run(session))
        try:
            yield subscriber
        finally:
            async with self._lifecycle_lock:
                session.subscribers.discard(subscriber)
                if not session.subscribers:
                    self._sessions.pop(binding.path, None)
                    session.task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await session.task

    async def _run(self, session: _Session) -> None:
        endpoint = session.binding.endpoint
        stream = (
            self._reader(endpoint.path)
            if self._reader is not None
            else reports(endpoint.path, hid_parent=endpoint.hid_parent)
        )
        started_ns = time.monotonic_ns()
        try:
            async with contextlib.aclosing(stream):
                while True:
                    report = await asyncio.wait_for(anext(stream), 1.0)
                    now = time.monotonic_ns()
                    values = session.binding.driver.decode(report)
                    if values is None:
                        if session.latest is None and now - started_ns > 1_000_000_000:
                            raise OSError(
                                errno.ENOTSUP, "Input reports do not support this driver's channels"
                            )
                        continue
                    gap = (
                        session.latest is not None and now - session.latest.arrival_ns > 100_000_000
                    )
                    frame = InputFrame(values, now, now, discontinuity=gap)
                    session.latest = frame
                    for subscriber in session.subscribers:
                        subscriber.offer(frame)
        except asyncio.CancelledError:
            raise
        except (OSError, TimeoutError, StopAsyncIteration) as exc:
            self._fail(session, OSError(errno.ENODEV, f"Native input unavailable: {exc}"))
        except Exception as exc:
            log.exception("Input driver failed for %s", session.binding.path)
            self._fail(session, OSError(errno.EIO, f"Input driver failed: {exc}"))

    @staticmethod
    def _fail(session: _Session, error: OSError) -> None:
        session.error = error
        session.latest = None
        for subscriber in session.subscribers:
            subscriber.offer(error)


_managers: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, SourceManager] = (
    weakref.WeakKeyDictionary()
)


def source_manager() -> SourceManager:
    loop = asyncio.get_running_loop()
    if loop not in _managers:
        _managers[loop] = SourceManager()
    return _managers[loop]
