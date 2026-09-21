import asyncio
import logging
from unittest.mock import AsyncMock, Mock

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
    def __init__(
        self,
        fds: tuple[int, ...] = (41, 42),
        *,
        fail_on_inhibit_calls: frozenset[int] = frozenset(),
    ) -> None:
        self.callback = None
        self.inhibit_calls: list[tuple[str, str, str, str]] = []
        self.fds = iter(fds)
        self.fail_on_inhibit_calls = fail_on_inhibit_calls

    def on_prepare_for_sleep(self, callback) -> None:
        self.callback = callback

    def off_prepare_for_sleep(self, callback) -> None:
        assert callback == self.callback
        self.callback = None

    async def call_inhibit(self, *args: str) -> int:
        self.inhibit_calls.append(args)
        if len(self.inhibit_calls) in self.fail_on_inhibit_calls:
            raise OSError("transient inhibit failure")
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
async def test_resume_inhibitor_failure_reconnects_and_retries() -> None:
    first_manager = _FakeLoginManager((41,), fail_on_inhibit_calls=frozenset({2}))
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
    first_manager.emit(True)
    await _flush_worker()
    first_manager.emit(False)
    await _wait_until(lambda: len(first_manager.inhibit_calls) == 2)
    await _wait_until(lambda: len(second_manager.inhibit_calls) == 1)

    assert first_bus.disconnected is True
    assert second_manager.callback is not None
    await coordinator.stop()
    assert closed_fds == [41, 42]


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


@pytest.mark.asyncio
@pytest.mark.parametrize("wake_during_neutralization", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_wake_resumes_input_before_slow_hardware_cleanup_finishes(
    monkeypatch, wake_during_neutralization, cleanup_fails
):
    from keymasq.keymasqd.daemon import Daemon
    from keymasq.keymasqd.device_manager import DeviceManager
    from keymasq.keymasqd.hardware_masking import HardwareMasking

    neutralizing = asyncio.Event()
    finish_neutralizing = asyncio.Event()
    restoring = asyncio.Event()
    finish_restoring = asyncio.Event()
    resumed_hardware = Mock()

    async def neutralize(_self):
        neutralizing.set()
        await finish_neutralizing.wait()

    async def restore(_self):
        restoring.set()
        await finish_restoring.wait()
        if cleanup_fails:
            raise OSError("hardware restoration failed")

    monkeypatch.setattr(DeviceManager, "neutralize_runtime", neutralize)
    monkeypatch.setattr(HardwareMasking, "suspend", restore)
    monkeypatch.setattr(HardwareMasking, "resume", resumed_hardware)
    daemon = Daemon()
    coordinator = daemon.sleep_coordinator
    manager = _FakeLoginManager()
    coordinator._bus_factory = lambda: _FakeBus(manager)
    closed_fds = []
    coordinator._close_fd = closed_fds.append
    assert await coordinator.start()
    try:
        manager.emit(True)
        await asyncio.wait_for(neutralizing.wait(), 1)
        if wake_during_neutralization:
            manager.emit(False)
            assert daemon.device_manager.runtime_input_paused()
        finish_neutralizing.set()
        await asyncio.wait_for(restoring.wait(), 1)
        if not wake_during_neutralization:
            assert daemon.device_manager.runtime_input_paused()
            manager.emit(False)

        assert not daemon.device_manager.runtime_input_paused()
        resumed_hardware.assert_not_called()
        assert closed_fds == []
        finish_restoring.set()
        await _wait_until(lambda: resumed_hardware.call_count == 1)
        assert closed_fds == [41]
        assert not daemon.device_manager.runtime_input_paused()
    finally:
        finish_neutralizing.set()
        finish_restoring.set()
        await coordinator.stop()


@pytest.mark.asyncio
async def test_new_suspend_during_cleanup_does_not_resume_on_stale_wake():
    manager = _FakeLoginManager()
    restoring = asyncio.Event()
    finish_restoring = asyncio.Event()
    runtime_events = []
    resumed_hardware = Mock()
    neutralize = AsyncMock()

    async def restore():
        restoring.set()
        await finish_restoring.wait()

    coordinator = LogindSleepCoordinator(
        neutralize,
        pause_runtime=lambda: runtime_events.append("pause"),
        resume_runtime=lambda: runtime_events.append("resume"),
        cleanup_hardware=restore,
        resume_hardware=resumed_hardware,
        bus_factory=lambda: _FakeBus(manager),
        close_fd=lambda _fd: None,
    )
    assert await coordinator.start()
    try:
        manager.emit(True)
        await asyncio.wait_for(restoring.wait(), 1)
        manager.emit(False)
        assert runtime_events == ["pause", "resume"]
        manager.emit(True)
        finish_restoring.set()
        await _wait_until(lambda: neutralize.await_count == 2)
        assert runtime_events == ["pause", "resume", "pause"]
        resumed_hardware.assert_not_called()
        manager.emit(False)
        await _wait_until(lambda: resumed_hardware.call_count == 1)
        assert runtime_events == ["pause", "resume", "pause", "resume"]
    finally:
        finish_restoring.set()
        await coordinator.stop()


@pytest.mark.asyncio
async def test_bus_disconnect_resumes_input_during_hardware_cleanup():
    first_manager = _FakeLoginManager((41,))
    second_manager = _FakeLoginManager((42,))
    first_bus = _FakeBus(first_manager)
    second_bus = _FakeBus(second_manager)
    buses = iter((first_bus, second_bus))
    restoring = asyncio.Event()
    finish_restoring = asyncio.Event()
    resumed_input = Mock()
    resumed_hardware = Mock()

    async def restore():
        restoring.set()
        await finish_restoring.wait()

    coordinator = LogindSleepCoordinator(
        AsyncMock(),
        resume_runtime=resumed_input,
        cleanup_hardware=restore,
        resume_hardware=resumed_hardware,
        bus_factory=lambda: next(buses),
        close_fd=lambda _fd: None,
    )
    assert await coordinator.start()
    try:
        first_manager.emit(True)
        await asyncio.wait_for(restoring.wait(), 1)
        first_bus.drop()
        await _wait_until(lambda: resumed_input.call_count == 1)
        resumed_hardware.assert_not_called()
        finish_restoring.set()
        await _wait_until(lambda: resumed_hardware.call_count == 1)
    finally:
        finish_restoring.set()
        await coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_step", ["masking", "neutralize"])
async def test_daemon_sleep_cleanup_attempts_both_steps_before_releasing_inhibitor(
    monkeypatch, failed_step
):
    from keymasq.keymasqd.daemon import Daemon

    daemon = Daemon()
    manager = _FakeLoginManager()
    order = []

    async def neutralize():
        order.append("neutralize")
        if failed_step == "neutralize":
            raise OSError("neutralization failed")

    async def masking():
        order.append("masking")
        if failed_step == "masking":
            raise OSError("masking recovery failed")

    monkeypatch.setattr(daemon.device_manager, "neutralize_runtime", neutralize)
    monkeypatch.setattr(daemon.hardware_masking, "suspend", masking)
    coordinator = LogindSleepCoordinator(
        daemon.device_manager.neutralize_runtime,
        pause_runtime=daemon.device_manager.pause_runtime_input,
        resume_runtime=daemon.device_manager.resume_runtime_input,
        cleanup_hardware=daemon.hardware_masking.suspend,
        resume_hardware=daemon.hardware_masking.resume,
        bus_factory=lambda: _FakeBus(manager),
        close_fd=lambda fd: order.append(f"close:{fd}"),
    )
    assert await coordinator.start()
    manager.emit(True)
    await _wait_until(lambda: "close:41" in order)
    assert order == ["neutralize", "masking", "close:41"]
    await coordinator.stop()
