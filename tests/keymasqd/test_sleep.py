import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from keymasq.keymasqd.sleep import (
    LOGIN1_MANAGER,
    LOGIN1_PATH,
    LOGIN1_SERVICE,
    LogindSleepCoordinator,
)


class _FakeLoginManager:
    def __init__(self) -> None:
        self.callback = None
        self.inhibit_calls: list[tuple[str, str, str, str]] = []
        self.fds = iter((41, 42))

    def on_prepare_for_sleep(self, callback) -> None:
        self.callback = callback

    def off_prepare_for_sleep(self, callback) -> None:
        assert callback == self.callback
        self.callback = None

    async def call_inhibit(self, *args: str) -> int:
        self.inhibit_calls.append(args)
        return next(self.fds)

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
async def test_delay_inhibitor_surrounds_suspend_cleanup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = _FakeLoginManager()
    bus = _FakeBus(manager)
    actions: list[str] = []
    prepare = AsyncMock(side_effect=lambda: actions.append("cleanup"))
    closed_fds: list[int] = []

    def pause_runtime() -> None:
        actions.append("pause")

    def resume_runtime() -> None:
        actions.append("resume")

    def close_fd(fd: int) -> None:
        closed_fds.append(fd)
        actions.append(f"close:{fd}")

    coordinator = LogindSleepCoordinator(
        prepare,
        pause_runtime=pause_runtime,
        resume_runtime=resume_runtime,
        bus_factory=lambda: bus,
        close_fd=close_fd,
    )
    caplog.set_level(logging.INFO, logger="keymasqd.sleep")

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
    assert actions == ["pause", "cleanup", "close:41"]
    assert (
        "Received logind suspend signal; neutralizing input runtime before suspend"
        in caplog.messages
    )

    manager.emit(False)
    await _flush_worker()
    assert len(manager.inhibit_calls) == 2
    assert actions == ["pause", "cleanup", "close:41", "resume"]

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
        bus_factory=lambda: bus,
        close_fd=closed_fds.append,
    )

    assert await coordinator.start() is True
    manager.emit(True)
    await _flush_worker()

    assert closed_fds == [41]
    await coordinator.stop()


@pytest.mark.asyncio
async def test_stop_completes_queued_suspend_cleanup_before_closing_inhibitor() -> None:
    manager = _FakeLoginManager()
    bus = _FakeBus(manager)
    prepare = AsyncMock()
    closed_fds: list[int] = []
    runtime_events: list[str] = []
    coordinator = LogindSleepCoordinator(
        prepare,
        pause_runtime=lambda: runtime_events.append("pause"),
        resume_runtime=lambda: runtime_events.append("resume"),
        bus_factory=lambda: bus,
        close_fd=closed_fds.append,
    )

    assert await coordinator.start() is True
    manager.emit(True)
    await coordinator.stop()

    prepare.assert_awaited_once()
    assert closed_fds == [41]
    assert runtime_events == ["pause", "resume"]


@pytest.mark.asyncio
async def test_logind_unavailable_does_not_prevent_daemon_start() -> None:
    class _UnavailableBus:
        async def connect(self):
            raise OSError("no system bus")

    coordinator = LogindSleepCoordinator(
        AsyncMock(),
        bus_factory=_UnavailableBus,
    )

    assert await coordinator.start() is False
    await coordinator.stop()
