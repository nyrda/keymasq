from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from dbus_next.constants import MessageType
from dbus_next.message import Message

import keymasq.session.manager.recording_unlock as recording_unlock_module
from keymasq.common.security import PeerCredentials
from keymasq.session.manager.core import SessionManager


def dbus_reply(body: list[object] | None = None, *, signature: str = "") -> Message:
    return Message(
        message_type=MessageType.METHOD_RETURN,
        reply_serial=1,
        signature=signature,
        body=body or [],
    )


def dbus_error_reply(
    message: str,
    *,
    error_name: str = "org.keymasq.TestError",
) -> Message:
    return Message(
        message_type=MessageType.ERROR,
        error_name=error_name,
        reply_serial=1,
        signature="s",
        body=[message],
    )


class FakeDBusBus:
    def __init__(
        self,
        *,
        call_replies: list[Message | None] | None = None,
        call_error: Exception | None = None,
    ) -> None:
        self.connected = True
        self.disconnect_calls = 0
        self.call_replies = list(call_replies or [])
        self.call_error = call_error
        self.messages: list[Message] = []
        self.handlers: list[Any] = []

    def disconnect(self) -> None:
        self.connected = False
        self.disconnect_calls += 1

    def add_message_handler(self, handler: Any) -> None:
        self.handlers.append(handler)

    def remove_message_handler(self, handler: Any) -> None:
        self.handlers.remove(handler)

    def emit(self, message: Message) -> None:
        for handler in list(self.handlers):
            handler(message)

    async def call(self, message: Message) -> Message | None:
        self.record_call(message)
        return self.call_replies.pop(0)

    def record_call(self, message: Message) -> None:
        self.messages.append(message)
        if self.call_error is not None:
            raise self.call_error


class FakeDBusProxy:
    def __init__(self, interfaces: dict[str, object]) -> None:
        self._interfaces = interfaces

    def get_interface(self, interface: str) -> object:
        return self._interfaces[interface]


class FakeIntrospectableDBusBus(FakeDBusBus):
    def __init__(
        self,
        *,
        call_replies: list[Message | None] | None = None,
        call_error: Exception | None = None,
        introspection_error: Exception | None = None,
    ) -> None:
        super().__init__(call_replies=call_replies, call_error=call_error)
        self.introspection_error = introspection_error
        self.introspection = object()
        self.introspection_calls: list[tuple[str, str]] = []
        self.proxy_requests: list[tuple[str, str, object]] = []
        self.interfaces: dict[str, object] = {}

    async def introspect(self, destination: str, path: str) -> object:
        self.introspection_calls.append((destination, path))
        if self.introspection_error is not None:
            raise self.introspection_error
        return self.introspection

    def get_proxy_object(
        self,
        destination: str,
        path: str,
        introspection: object,
    ) -> FakeDBusProxy:
        self.proxy_requests.append((destination, path, introspection))
        return FakeDBusProxy(self.interfaces)


class FakeDBusClient:
    def __init__(
        self,
        bus: object | None = None,
        *,
        bus_error: Exception | None = None,
    ) -> None:
        self._bus = bus
        self.bus_error = bus_error
        self.disconnect_calls = 0

    async def bus(self) -> object:
        if self.bus_error is not None:
            raise self.bus_error
        if self._bus is None:
            raise AssertionError("fake D-Bus client has no bus")
        return self._bus

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


def patch_session_message_bus(
    monkeypatch: pytest.MonkeyPatch,
    dbus_module: object,
    buses: list[FakeIntrospectableDBusBus],
) -> None:
    class _MessageBusFactory:
        async def connect(self) -> FakeIntrospectableDBusBus:
            return buses.pop(0)

    monkeypatch.setattr(dbus_module, "MessageBus", _MessageBusFactory)


def grant_recording_refresh_owner(
    manager: SessionManager,
    peer: PeerCredentials,
    writer: object,
    monkeypatch: pytest.MonkeyPatch,
    *,
    lease_id: str = "lease-test",
) -> AsyncMock:
    manager.unlock_state.refresh_owner = {
        "uid": peer.uid,
        "pid": peer.pid,
        "writer_id": id(writer),
        "lease_id": lease_id,
        "source": "runtime",
    }
    resolve_unlock_status_async = AsyncMock(
        return_value={"unlocked": True, "source": "runtime", "expires_at": 9999999999}
    )
    monkeypatch.setattr(
        recording_unlock_module,
        "resolve_unlock_status_async",
        resolve_unlock_status_async,
    )
    return resolve_unlock_status_async
