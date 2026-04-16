import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from dbus_next.aio.message_bus import MessageBus
from dbus_next.constants import MessageType
from dbus_next.message import Message

DBUS_SERVICE = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
DBUS_INTERFACE = "org.freedesktop.DBus"
NOTIFICATIONS_SERVICE = "org.freedesktop.Notifications"
NOTIFICATIONS_PATH = "/org/freedesktop/Notifications"
NOTIFICATIONS_INTERFACE = "org.freedesktop.Notifications"


class SessionDBus:
    def __init__(self) -> None:
        self._bus: MessageBus | None = None
        self._connect_lock = asyncio.Lock()
        self._introspection_cache: dict[tuple[str, str], Any] = {}

    async def connect(self) -> MessageBus:
        async with self._connect_lock:
            if self._bus is None:
                self._bus = await MessageBus().connect()
            return self._bus

    async def bus(self) -> MessageBus:
        return await self.connect()

    async def disconnect(self) -> None:
        async with self._connect_lock:
            bus = self._bus
            self._bus = None
            self._introspection_cache.clear()

        if bus is not None:
            bus.disconnect()

    async def introspect(self, destination: str, path: str) -> Any:
        key = (destination, path)
        cached = self._introspection_cache.get(key)
        if cached is not None:
            return cached

        bus = await self.bus()
        introspection = await bus.introspect(destination, path)
        self._introspection_cache[key] = introspection
        return introspection

    async def get_interface(self, destination: str, path: str, interface: str) -> Any:
        bus = await self.bus()
        introspection = await self.introspect(destination, path)
        proxy = bus.get_proxy_object(destination, path, introspection)
        return proxy.get_interface(interface)

    async def name_has_owner(self, name: str, *, timeout: float = 0.6) -> bool:
        iface = await self.get_interface(DBUS_SERVICE, DBUS_PATH, DBUS_INTERFACE)
        has_owner = await asyncio.wait_for(iface.call_name_has_owner(name), timeout=timeout)
        return bool(has_owner)

    async def notify(
        self,
        title: str,
        message: str,
        *,
        app_name: str = "keymasq",
        timeout_ms: int = 2000,
        app_icon: str = "",
    ) -> int | None:
        bus = await self.bus()
        reply = await bus.call(
            Message(
                destination=NOTIFICATIONS_SERVICE,
                path=NOTIFICATIONS_PATH,
                interface=NOTIFICATIONS_INTERFACE,
                member="Notify",
                signature="susssasa{sv}i",
                body=[app_name, 0, app_icon, title, message, [], {}, timeout_ms],
            )
        )
        if reply is None:
            raise RuntimeError("notification delivery failed")
        if reply.message_type == MessageType.ERROR:
            err = str(reply.body[0]) if reply.body else "notification delivery failed"
            raise RuntimeError(err)
        if not reply.body:
            return None
        return int(reply.body[0])


@asynccontextmanager
async def temporary_session_dbus() -> AsyncIterator[SessionDBus]:
    dbus = SessionDBus()
    try:
        await dbus.connect()
        yield dbus
    finally:
        await dbus.disconnect()


async def name_has_owner(
    name: str,
    dbus: SessionDBus | None = None,
    *,
    timeout: float = 0.6,
) -> bool:
    try:
        if dbus is not None:
            return await dbus.name_has_owner(name, timeout=timeout)
        async with temporary_session_dbus() as temporary_dbus:
            return await temporary_dbus.name_has_owner(name, timeout=timeout)
    except Exception:
        return False
