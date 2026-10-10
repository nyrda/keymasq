"""One native reader per connection, shared by independent bounded subscribers."""

import asyncio
import contextlib
import errno
import logging
import time
import weakref
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field, replace

from keymasq.common.masking import HARDWARE_JOB_TIMEOUT

from . import clock
from .transports import hid_bpf
from .transports.hidraw import reports
from .types import Binding, InputFrame

log = logging.getLogger("keymasqd.input_sources")
VALID_SAMPLE_TIMEOUT_S = 1.0
GAP_NS = 100_000_000
# The first HID-BPF sample waits for a privileged job to attach the program.
STARTUP_TIMEOUT_S = {"hid-bpf": HARDWARE_JOB_TIMEOUT + 5.0}
# Keep an idle attachment briefly so profile switches do not start a root job each time.
LINGER_S = {"hid-bpf": 30.0}
# These transports deliver complete state snapshots, so a quiet interval loses nothing.
SNAPSHOT_TRANSPORTS = frozenset({"hid-bpf"})


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
    linger: asyncio.TimerHandle | None = None


class SourceManager:
    def __init__(self, reader: Callable[[str], AsyncGenerator[bytes]] | None = None) -> None:
        self._reader = reader
        self._sessions: dict[str, _Session] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._expiring: set[asyncio.Task[None]] = set()

    @contextlib.asynccontextmanager
    async def subscribe(self, binding: Binding) -> AsyncGenerator[Subscription]:
        async with self._lifecycle_lock:
            session = self._sessions.get(binding.path)
            if session is None:
                session = _Session(binding)
                self._sessions[binding.path] = session
            elif session.linger is not None:
                session.linger.cancel()
                session.linger = None
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
                    delay = self._linger_s(session)
                    if delay:
                        session.linger = asyncio.get_running_loop().call_later(
                            delay, self._expire, session
                        )
                    else:
                        await self._close(session)

    def _linger_s(self, session: _Session) -> float:
        if self._reader is not None or session.error is not None:
            return 0.0
        return LINGER_S.get(session.binding.driver.transport, 0.0)

    async def _close(self, session: _Session) -> None:
        if self._sessions.get(session.binding.path) is session:
            del self._sessions[session.binding.path]
        if session.task is not None:
            session.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session.task

    def _expire(self, session: _Session) -> None:
        session.linger = None
        task = asyncio.create_task(self._close_if_idle(session))
        self._expiring.add(task)
        task.add_done_callback(self._expiring.discard)

    async def _close_if_idle(self, session: _Session) -> None:
        async with self._lifecycle_lock:
            if not session.subscribers and session.linger is None:
                await self._close(session)

    async def _run(self, session: _Session) -> None:
        binding = session.binding
        endpoint = binding.endpoint
        if self._reader is not None:
            stream = self._reader(endpoint.path)
        elif binding.driver.transport == "hid-bpf":
            stream = hid_bpf.reports(binding)
        else:
            stream = reports(endpoint.path, hid_parent=endpoint.hid_parent)
        timeout_s = STARTUP_TIMEOUT_S.get(binding.driver.transport, VALID_SAMPLE_TIMEOUT_S)
        last_valid_ns = time.monotonic_ns()
        suspended_at_ns = clock.suspended_ns()
        pending: asyncio.Task[bytes] | None = None
        try:
            async with contextlib.aclosing(stream):
                try:
                    while True:
                        if pending is None:
                            pending = asyncio.create_task(_next_report(stream))
                        due_ns = last_valid_ns + int(timeout_s * 1_000_000_000)
                        # A timeout must not cancel the read, which would close the stream.
                        await asyncio.wait(
                            (pending,),
                            timeout=max(0.0, (due_ns - time.monotonic_ns()) / 1_000_000_000),
                        )
                        if not pending.done():
                            if not clock.unobserved(due_ns, suspended_at_ns):
                                raise TimeoutError(f"no valid input report for {timeout_s:g} s")
                            log.info(
                                "Restarting the input timeout of %s after a suspend or stall",
                                binding.path,
                            )
                            last_valid_ns = time.monotonic_ns()
                            suspended_at_ns = clock.suspended_ns()
                            continue
                        task, pending = pending, None
                        report = task.result()
                        now = time.monotonic_ns()
                        values = binding.driver.decode(report)
                        if values is None:
                            if now > due_ns:
                                if not clock.unobserved(due_ns, suspended_at_ns):
                                    raise OSError(
                                        errno.ENOTSUP,
                                        "Input reports do not support this driver's channels",
                                    )
                                last_valid_ns = now
                                suspended_at_ns = clock.suspended_ns()
                            continue
                        last_valid_ns = now
                        suspended_at_ns = clock.suspended_ns()
                        timeout_s = VALID_SAMPLE_TIMEOUT_S
                        gap = (
                            binding.driver.transport not in SNAPSHOT_TRANSPORTS
                            and session.latest is not None
                            and now - session.latest.arrival_ns > GAP_NS
                        )
                        frame = InputFrame(values, now, now, discontinuity=gap)
                        session.latest = frame
                        for subscriber in session.subscribers:
                            subscriber.offer(frame)
                finally:
                    if pending is not None:
                        pending.cancel()
                        await asyncio.wait((pending,))
                        if not pending.cancelled():
                            pending.exception()
        except asyncio.CancelledError:
            raise
        except (OSError, TimeoutError, StopAsyncIteration) as exc:
            self._fail(session, OSError(errno.ENODEV, f"Native input unavailable: {exc}"))
        except Exception as exc:
            log.exception("Input driver failed for %s", session.binding.path)
            self._fail(session, OSError(errno.EIO, f"Input driver failed: {exc}"))

    def _fail(self, session: _Session, error: OSError) -> None:
        session.error = error
        session.latest = None
        for subscriber in session.subscribers:
            subscriber.offer(error)
        if session.linger is not None:
            session.linger.cancel()
            session.linger = None
            if self._sessions.get(session.binding.path) is session:
                del self._sessions[session.binding.path]


async def _next_report(stream: AsyncGenerator[bytes]) -> bytes:
    return await anext(stream)


_managers: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, SourceManager] = (
    weakref.WeakKeyDictionary()
)


def source_manager() -> SourceManager:
    loop = asyncio.get_running_loop()
    if loop not in _managers:
        _managers[loop] = SourceManager()
    return _managers[loop]
