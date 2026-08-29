"""Coordinate keymasqd pre-suspend output cleanup with systemd-logind."""

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from dbus_next.aio.message_bus import MessageBus
from dbus_next.constants import BusType

log = logging.getLogger("keymasqd.sleep")

LOGIN1_SERVICE = "org.freedesktop.login1"
LOGIN1_PATH = "/org/freedesktop/login1"
LOGIN1_MANAGER = "org.freedesktop.login1.Manager"
SETUP_TIMEOUT_S = 2.0

type AsyncCallback = Callable[[], Awaitable[object]]
type BusFactory = Callable[[], Any]


def _system_bus() -> MessageBus:
    return MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True)


class LogindSleepCoordinator:
    """Run one cleanup callback before logind suspends the machine."""

    def __init__(
        self,
        prepare_for_sleep: AsyncCallback,
        *,
        bus_factory: BusFactory = _system_bus,
        close_fd: Callable[[int], None] = os.close,
    ) -> None:
        self._prepare_for_sleep = prepare_for_sleep
        self._bus_factory = bus_factory
        self._close_fd = close_fd
        self._bus: Any | None = None
        self._manager: Any | None = None
        self._inhibitor_fd: int | None = None
        self._events: asyncio.Queue[bool] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._subscribed = False

    async def start(self) -> bool:
        """Connect to logind, or return false when it is unavailable."""

        if self._bus is not None:
            return True

        try:
            async with asyncio.timeout(SETUP_TIMEOUT_S):
                bus = await self._bus_factory().connect()
                self._bus = bus
                introspection = await bus.introspect(LOGIN1_SERVICE, LOGIN1_PATH)
                proxy = bus.get_proxy_object(LOGIN1_SERVICE, LOGIN1_PATH, introspection)
                manager = proxy.get_interface(LOGIN1_MANAGER)
                self._manager = manager
                manager.on_prepare_for_sleep(self._on_prepare_for_sleep)
                self._subscribed = True
                await self._acquire_inhibitor()
        except Exception as exc:  # noqa: BLE001 - logind integration is optional.
            log.warning(
                "systemd-logind sleep notification is unavailable; "
                "pre-suspend cleanup is disabled: %s",
                exc,
            )
            await self._close_connection()
            return False

        self._worker = asyncio.create_task(self._run(), name="keymasqd-logind-sleep")
        log.info("Enabled systemd-logind pre-suspend cleanup")
        return True

    async def stop(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        await self._close_connection()

    def _on_prepare_for_sleep(self, preparing: bool) -> None:
        self._events.put_nowait(bool(preparing))

    async def _run(self) -> None:
        while True:
            preparing = await self._events.get()
            if preparing:
                log.info(
                    "Received logind suspend signal; running pre-suspend output cleanup"
                )
                try:
                    await self._prepare_for_sleep()
                except Exception:
                    log.exception("Pre-suspend cleanup failed")
                finally:
                    self._release_inhibitor()
                continue

            try:
                await self._acquire_inhibitor()
            except Exception:
                log.exception("Failed to reacquire the logind sleep inhibitor after resume")

    async def _acquire_inhibitor(self) -> None:
        manager = self._manager
        if manager is None or self._inhibitor_fd is not None:
            return
        self._inhibitor_fd = int(
            await manager.call_inhibit(
                "sleep",
                "keymasqd",
                "Release active remapped input state",
                "delay",
            )
        )

    def _release_inhibitor(self) -> None:
        inhibitor_fd = self._inhibitor_fd
        self._inhibitor_fd = None
        if inhibitor_fd is None:
            return
        try:
            self._close_fd(inhibitor_fd)
        except OSError:
            log.debug("Failed to close logind sleep inhibitor", exc_info=True)

    async def _close_connection(self) -> None:
        manager = self._manager
        if manager is not None and self._subscribed:
            with contextlib.suppress(Exception):
                manager.off_prepare_for_sleep(self._on_prepare_for_sleep)
        self._subscribed = False
        self._manager = None
        self._release_inhibitor()

        bus = self._bus
        self._bus = None
        if bus is not None:
            with contextlib.suppress(Exception):
                bus.disconnect()
