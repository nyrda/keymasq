import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from keymasq.keymasqd.sleep import (
    DBUS_INTERFACE,
    DBUS_PATH,
    DBUS_SERVICE,
    LOGIN1_MANAGER,
    LOGIN1_PATH,
    LOGIN1_SERVICE,
    LogindSleepCoordinator,
)


class _FakeLoginManager:
    def __init__(self, fds: tuple[int, ...] = (41, 42)) -> None:
        self.callback = None
        self.inhibit_calls: list[tuple[str, str, str, str]] = []
        self.fds = iter(fds)

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


class _FakeDBusManager:
    def __init__(self) -> None:
        self.callback = None

    def on_name_owner_changed(self, callback) -> None:
        self.callback = callback

    def off_name_owner_changed(self, callback) -> None:
        assert callback == self.callback
        self.callback = None

    def emit(self, name: str, old_owner: str, new_owner: str) -> None:
        assert self.callback is not None
        self.callback(name, old_owner, new_owner)


class _FakeProxy:
    def __init__(self, interface: str, implementation: object) -> None:
        self.interface = interface
        self.implementation = implementation

    def get_interface(self, interface: str):
        assert interface == self.interface
        return self.implementation


class _FakeBus:
    def __init__(self, manager: _FakeLoginManager) -> None:
        self.manager = manager
        self.dbus_manager = _FakeDBusManager()
        self.disconnected = False
        self.disconnect_event = asyncio.Event()

    async def connect(self):
        return self

    async def introspect(self, service: str, path: str):
        assert (service, path) in {
            (DBUS_SERVICE, DBUS_PATH),
            (LOGIN1_SERVICE, LOGIN1_PATH),
        }
        return object()

    def get_proxy_object(self, service: str, path: str, _introspection):
        if (service, path) == (DBUS_SERVICE, DBUS_PATH):
            return _FakeProxy(DBUS_INTERFACE, self.dbus_manager)
        assert (service, path) == (LOGIN1_SERVICE, LOGIN1_PATH)
        return _FakeProxy(LOGIN1_MANAGER, self.manager)

    async def wait_for_disconnect(self) -> None:
        await self.disconnect_event.wait()

    def drop(self) -> None:
        self.disconnect_event.set()

    def disconnect(self) -> None:
        self.disconnected = True
        self.disconnect_event.set()


async def _flush_worker() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def _wait_until(predicate) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


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
    assert bus.dbus_manager.callback is None
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
async def test_logind_restart_reconnects_and_reacquires_inhibitor() -> None:
    first_manager = _FakeLoginManager((41,))
    second_manager = _FakeLoginManager((42,))
    first_bus = _FakeBus(first_manager)
    second_bus = _FakeBus(second_manager)
    buses = iter((first_bus, second_bus))
    closed_fds: list[int] = []
    coordinator = LogindSleepCoordinator(
        AsyncMock(),
        bus_factory=lambda: next(buses),
        close_fd=closed_fds.append,
    )

    assert await coordinator.start() is True
    first_bus.dbus_manager.emit(LOGIN1_SERVICE, ":1.10", ":1.11")
    await _wait_until(lambda: len(second_manager.inhibit_calls) == 1)

    assert first_bus.disconnected is True
    assert first_manager.callback is None
    assert closed_fds == [41]
    assert second_manager.callback is not None

    await coordinator.stop()
    assert closed_fds == [41, 42]


@pytest.mark.asyncio
async def test_bus_disconnect_resumes_input_and_reconnects() -> None:
    first_manager = _FakeLoginManager((41,))
    second_manager = _FakeLoginManager((42,))
    first_bus = _FakeBus(first_manager)
    second_bus = _FakeBus(second_manager)
    buses = iter((first_bus, second_bus))
    runtime_events: list[str] = []
    coordinator = LogindSleepCoordinator(
        AsyncMock(),
        pause_runtime=lambda: runtime_events.append("pause"),
        resume_runtime=lambda: runtime_events.append("resume"),
        bus_factory=lambda: next(buses),
        close_fd=lambda _fd: None,
    )

    assert await coordinator.start() is True
    first_manager.emit(True)
    await _flush_worker()
    assert runtime_events == ["pause"]

    first_bus.drop()
    await _wait_until(lambda: len(second_manager.inhibit_calls) == 1)
    await _wait_until(lambda: runtime_events == ["pause", "resume"])

    assert second_manager.callback is not None
    await coordinator.stop()


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
