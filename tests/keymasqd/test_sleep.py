import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest

from keymasq.keymasqd.sleep import (
    LOGIN1_MANAGER,
    LOGIN1_PATH,
    LOGIN1_SERVICE,
    LogindSleepCoordinator,
)


class _FakeLoginManager:
    def __init__(self, *, preparing: bool = False) -> None:
        self.preparing = preparing
        self.callback = None
        self.inhibit_calls: list[tuple[str, str, str, str]] = []
        self.fds = iter((41, 42, 43))

    def on_prepare_for_sleep(self, callback) -> None:
        self.callback = callback

    def off_prepare_for_sleep(self, callback) -> None:
        assert callback == self.callback
        self.callback = None

    async def call_inhibit(self, *args: str) -> int:
        self.inhibit_calls.append(args)
        return next(self.fds)

    async def get_preparing_for_sleep(self) -> bool:
        return self.preparing

    def emit(self, preparing: bool) -> None:
        assert self.callback is not None
        self.callback(preparing)


class _FakeBus:
    def __init__(self, manager: _FakeLoginManager) -> None:
        self.manager = manager
        self.disconnected = False

    async def connect(self):
        return self

    async def introspect(self, service: str, path: str):
        assert (service, path) == (LOGIN1_SERVICE, LOGIN1_PATH)
        return object()

    def get_proxy_object(self, service: str, path: str, _introspection):
        assert (service, path) == (LOGIN1_SERVICE, LOGIN1_PATH)
        return self

    def get_interface(self, interface: str):
        assert interface == LOGIN1_MANAGER
        return self.manager

    def disconnect(self) -> None:
        self.disconnected = True


async def _flush_worker() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_delay_inhibitor_surrounds_suspend_cleanup() -> None:
    manager = _FakeLoginManager()
    bus = _FakeBus(manager)
    prepare = AsyncMock()
    resume = AsyncMock()
    closed_fds: list[int] = []
    coordinator = LogindSleepCoordinator(
        prepare,
        resume,
        bus_factory=lambda: bus,
        close_fd=closed_fds.append,
    )

    assert await coordinator.start() is True
    assert manager.inhibit_calls == [
        (
            "sleep",
            "keymasqd",
            "Release active remapped input state",
            "delay",
        )
    ]

    manager.emit(True)
    await _flush_worker()
    prepare.assert_awaited_once()
    assert closed_fds == [41]

    manager.emit(False)
    await _flush_worker()
    resume.assert_awaited_once()
    assert len(manager.inhibit_calls) == 2

    await coordinator.stop()
    assert closed_fds == [41, 42]
    assert manager.callback is None
    assert bus.disconnected is True


@pytest.mark.asyncio
async def test_cleanup_failure_still_releases_delay_inhibitor() -> None:
    manager = _FakeLoginManager()
    bus = _FakeBus(manager)
    closed_fds: list[int] = []
    coordinator = LogindSleepCoordinator(
        AsyncMock(side_effect=RuntimeError("cleanup failed")),
        AsyncMock(),
        bus_factory=lambda: bus,
        close_fd=closed_fds.append,
    )

    assert await coordinator.start() is True
    manager.emit(True)
    await _flush_worker()

    assert closed_fds == [41]
    await coordinator.stop()


@pytest.mark.asyncio
async def test_resume_inhibitor_timeout_does_not_block_next_suspend() -> None:
    manager = _FakeLoginManager()
    bus = _FakeBus(manager)
    prepare = AsyncMock()
    resume = AsyncMock()
    rearm_started = asyncio.Event()
    second_prepare_started = asyncio.Event()

    async def prepare_for_sleep() -> None:
        if prepare.await_count == 2:
            second_prepare_started.set()

    prepare.side_effect = prepare_for_sleep

    async def inhibit(*args: str) -> int:
        manager.inhibit_calls.append(args)
        if len(manager.inhibit_calls) == 1:
            return 41
        rearm_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    manager.call_inhibit = inhibit  # type: ignore[method-assign]
    coordinator = LogindSleepCoordinator(
        prepare,
        resume,
        bus_factory=lambda: bus,
        setup_timeout_s=1,
    )

    assert await coordinator.start() is True
    manager.emit(True)
    await _flush_worker()
    manager.emit(False)
    await asyncio.wait_for(rearm_started.wait(), timeout=0.25)

    manager.emit(True)
    await asyncio.wait_for(second_prepare_started.wait(), timeout=0.25)

    assert prepare.await_count == 2
    resume.assert_not_awaited()
    await coordinator.stop()


@pytest.mark.asyncio
async def test_resume_inhibitor_failure_is_retried_until_rearmed() -> None:
    manager = _FakeLoginManager()
    closed_fds: list[int] = []
    rearmed = asyncio.Event()

    async def inhibit(*args: str) -> int:
        manager.inhibit_calls.append(args)
        if len(manager.inhibit_calls) == 1:
            return 41
        if len(manager.inhibit_calls) == 2:
            raise OSError("temporary logind failure")
        rearmed.set()
        return 42

    manager.call_inhibit = inhibit  # type: ignore[method-assign]
    resume = AsyncMock()
    coordinator = LogindSleepCoordinator(
        AsyncMock(),
        resume,
        bus_factory=lambda: _FakeBus(manager),
        close_fd=closed_fds.append,
        rearm_retry_s=0,
    )

    assert await coordinator.start() is True
    manager.emit(True)
    await _flush_worker()
    manager.emit(False)
    await rearmed.wait()
    await _flush_worker()

    assert len(manager.inhibit_calls) == 3
    assert coordinator._inhibitor_fd == 42
    resume.assert_awaited_once_with()
    await coordinator.stop()
    assert closed_fds == [41, 42]


@pytest.mark.asyncio
async def test_inhibitor_hangup_rearms_after_logind_restart() -> None:
    manager = _FakeLoginManager()
    closed_fds: list[int] = []
    readers: dict[int, Callable[[], None]] = {}
    prepare = AsyncMock()
    resume = AsyncMock()

    def add_reader(fd: int, callback) -> None:
        readers[fd] = callback

    def remove_reader(fd: int) -> None:
        readers.pop(fd, None)

    coordinator = LogindSleepCoordinator(
        prepare,
        resume,
        bus_factory=lambda: _FakeBus(manager),
        close_fd=closed_fds.append,
        add_fd_reader=add_reader,
        remove_fd_reader=remove_reader,
    )

    assert await coordinator.start() is True
    callback = readers[41]
    assert callable(callback)
    callback()
    await _flush_worker()

    assert closed_fds == [41]
    assert coordinator._inhibitor_fd == 42
    assert set(readers) == {42}
    prepare.assert_awaited_once_with()
    resume.assert_awaited_once_with()
    await coordinator.stop()
    assert closed_fds == [41, 42]


@pytest.mark.asyncio
async def test_stop_rejects_inflight_inhibitor_result() -> None:
    manager = _FakeLoginManager()
    closed_fds: list[int] = []
    added_readers: list[int] = []
    rearm_started = asyncio.Event()
    rearm_cancelled = asyncio.Event()
    release_rearm = asyncio.Event()

    async def inhibit(*args: str) -> int:
        manager.inhibit_calls.append(args)
        if len(manager.inhibit_calls) == 1:
            return 41
        rearm_started.set()
        try:
            await release_rearm.wait()
        except asyncio.CancelledError:
            rearm_cancelled.set()
            await release_rearm.wait()
        return 42

    manager.call_inhibit = inhibit  # type: ignore[method-assign]
    coordinator = LogindSleepCoordinator(
        AsyncMock(),
        AsyncMock(),
        bus_factory=lambda: _FakeBus(manager),
        close_fd=closed_fds.append,
        add_fd_reader=lambda fd, _callback: added_readers.append(fd),
    )

    assert await coordinator.start() is True
    coordinator._on_inhibitor_hangup()
    await asyncio.wait_for(rearm_started.wait(), timeout=0.25)
    stop_task = asyncio.create_task(coordinator.stop())
    await asyncio.wait_for(rearm_cancelled.wait(), timeout=0.25)
    release_rearm.set()
    await stop_task

    assert added_readers == [41]
    assert closed_fds == [41, 42]
    assert coordinator._rearm_task is None
    assert coordinator._inhibitor_fd is None


@pytest.mark.asyncio
async def test_inhibitor_hangup_does_not_rearm_during_stop() -> None:
    manager = _FakeLoginManager()
    readers: dict[int, Callable[[], None]] = {}
    setup_retry_cancelled = asyncio.Event()
    release_setup_retry = asyncio.Event()

    async def hold_setup_retry() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            setup_retry_cancelled.set()
            await release_setup_retry.wait()

    coordinator = LogindSleepCoordinator(
        AsyncMock(),
        AsyncMock(),
        bus_factory=lambda: _FakeBus(manager),
        add_fd_reader=lambda fd, callback: readers.__setitem__(fd, callback),
        remove_fd_reader=lambda fd: readers.pop(fd, None),
    )

    assert await coordinator.start() is True
    coordinator._setup_retry_task = asyncio.create_task(hold_setup_retry())
    stop_task = asyncio.create_task(coordinator.stop())
    await asyncio.wait_for(setup_retry_cancelled.wait(), timeout=0.25)
    readers[41]()
    await _flush_worker()

    assert len(manager.inhibit_calls) == 1
    assert coordinator._rearm_task is None
    release_setup_retry.set()
    await stop_task
    assert coordinator._inhibitor_fd is None


@pytest.mark.asyncio
async def test_restart_after_prepare_processes_the_next_suspend() -> None:
    manager = _FakeLoginManager()
    bus = _FakeBus(manager)
    prepare = AsyncMock()
    closed_fds: list[int] = []
    coordinator = LogindSleepCoordinator(
        prepare,
        AsyncMock(),
        bus_factory=lambda: bus,
        close_fd=closed_fds.append,
    )

    assert await coordinator.start() is True
    manager.emit(True)
    await _flush_worker()
    await coordinator.stop()

    assert await coordinator.start() is True
    manager.emit(True)
    await _flush_worker()

    assert prepare.await_count == 2
    assert closed_fds == [41, 42]
    await coordinator.stop()


@pytest.mark.asyncio
async def test_stop_discards_queued_sleep_state_events() -> None:
    manager = _FakeLoginManager()
    coordinator = LogindSleepCoordinator(
        AsyncMock(),
        AsyncMock(),
        bus_factory=lambda: _FakeBus(manager),
    )

    assert await coordinator.start() is True
    manager.emit(False)
    await coordinator.stop()

    assert coordinator._events.empty()


@pytest.mark.asyncio
async def test_logind_unavailable_does_not_prevent_daemon_start() -> None:
    class _UnavailableBus:
        async def connect(self):
            raise OSError("no system bus")

    coordinator = LogindSleepCoordinator(
        AsyncMock(),
        AsyncMock(),
        bus_factory=_UnavailableBus,
    )

    assert await coordinator.start() is False
    await coordinator.stop()


@pytest.mark.asyncio
async def test_logind_setup_is_retried_after_transient_startup_failure() -> None:
    class _UnavailableBus:
        async def connect(self):
            raise OSError("system bus not ready")

    manager = _FakeLoginManager()
    bus = _FakeBus(manager)
    attempts = 0

    def bus_factory():
        nonlocal attempts
        attempts += 1
        return _UnavailableBus() if attempts == 1 else bus

    coordinator = LogindSleepCoordinator(
        AsyncMock(),
        AsyncMock(),
        bus_factory=bus_factory,
        rearm_retry_s=0,
    )

    assert await coordinator.start() is False
    for _ in range(10):
        if coordinator._inhibitor_fd == 41:
            break
        await asyncio.sleep(0)

    assert attempts == 2
    assert coordinator._inhibitor_fd == 41
    assert coordinator._worker is not None
    await coordinator.stop()


@pytest.mark.asyncio
async def test_failed_setup_discards_sleep_signal_before_retry() -> None:
    manager = _FakeLoginManager()
    prepare = AsyncMock()
    retry_ready = asyncio.Event()
    attempts = 0

    async def inhibit(*args: str) -> int:
        nonlocal attempts
        manager.inhibit_calls.append(args)
        attempts += 1
        if attempts == 1:
            manager.emit(True)
            raise OSError("logind setup failed")
        retry_ready.set()
        return 42

    manager.call_inhibit = inhibit  # type: ignore[method-assign]
    coordinator = LogindSleepCoordinator(
        prepare,
        AsyncMock(),
        bus_factory=lambda: _FakeBus(manager),
        rearm_retry_s=0,
    )

    assert await coordinator.start() is False
    await asyncio.wait_for(retry_ready.wait(), timeout=0.25)
    await _flush_worker()

    prepare.assert_not_awaited()
    assert coordinator._inhibitor_fd == 42
    await coordinator.stop()


@pytest.mark.asyncio
async def test_logind_setup_timeout_does_not_prevent_daemon_start() -> None:
    class _UnresponsiveBus:
        def __init__(self) -> None:
            self.disconnected = False

        async def connect(self):
            return self

        async def introspect(self, _service: str, _path: str):
            await asyncio.Event().wait()

        def disconnect(self) -> None:
            self.disconnected = True

    bus = _UnresponsiveBus()
    coordinator = LogindSleepCoordinator(
        AsyncMock(),
        AsyncMock(),
        bus_factory=lambda: bus,
        setup_timeout_s=0.01,
    )

    assert await coordinator.start() is False
    assert bus.disconnected is True
    await coordinator.stop()
