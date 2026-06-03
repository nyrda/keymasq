import pytest
from dbus_next.constants import MessageType
from dbus_next.message import Message

import keymasq.session.dbus as dbus_module
from keymasq.session.dbus import (
    DBUS_INTERFACE,
    DBUS_PATH,
    DBUS_SERVICE,
    NOTIFICATIONS_INTERFACE,
    NOTIFICATIONS_PATH,
    NOTIFICATIONS_SERVICE,
    SessionDBus,
)


class _FakeProxy:
    def __init__(self, interfaces: dict[str, object]) -> None:
        self._interfaces = interfaces

    def get_interface(self, interface: str) -> object:
        return self._interfaces[interface]


class _FakeBus:
    def __init__(
        self,
        *,
        call_replies: list[Message | None] | None = None,
        call_error: Exception | None = None,
        introspection_error: Exception | None = None,
    ) -> None:
        self.connected = True
        self.disconnect_calls = 0
        self.call_replies = list(call_replies or [])
        self.call_error = call_error
        self.introspection_error = introspection_error
        self.introspection = object()
        self.introspection_calls: list[tuple[str, str]] = []
        self.proxy_requests: list[tuple[str, str, object]] = []
        self.messages: list[Message] = []
        self.interfaces: dict[str, object] = {}

    def disconnect(self) -> None:
        self.connected = False
        self.disconnect_calls += 1

    async def introspect(self, destination: str, path: str) -> object:
        self.introspection_calls.append((destination, path))
        if self.introspection_error is not None:
            raise self.introspection_error
        return self.introspection

    def get_proxy_object(self, destination: str, path: str, introspection: object) -> _FakeProxy:
        self.proxy_requests.append((destination, path, introspection))
        return _FakeProxy(self.interfaces)

    async def call(self, message: Message) -> Message | None:
        self.messages.append(message)
        if self.call_error is not None:
            raise self.call_error
        return self.call_replies.pop(0)


class _NameOwnerInterface:
    def __init__(self, result: object = True, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []

    async def call_name_has_owner(self, name: str) -> object:
        self.calls.append(name)
        if self.error is not None:
            raise self.error
        return self.result


class _SuppliedDBus:
    def __init__(self, result: bool, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, float]] = []

    async def name_has_owner(self, name: str, *, timeout: float = 0.6) -> bool:
        self.calls.append((name, timeout))
        if self.error is not None:
            raise self.error
        return self.result


def _reply(signature: str = "", body: list[object] | None = None) -> Message:
    return Message(
        message_type=MessageType.METHOD_RETURN,
        reply_serial=1,
        signature=signature,
        body=body or [],
    )


def _error_reply(message: str) -> Message:
    return Message(
        message_type=MessageType.ERROR,
        error_name="org.keymasq.TestError",
        reply_serial=1,
        signature="s",
        body=[message],
    )


def _patch_message_bus(monkeypatch: pytest.MonkeyPatch, buses: list[_FakeBus]) -> None:
    class _MessageBusFactory:
        async def connect(self) -> _FakeBus:
            return buses.pop(0)

    monkeypatch.setattr(dbus_module, "MessageBus", _MessageBusFactory)


@pytest.mark.asyncio
async def test_session_dbus_reconnects_when_cached_bus_is_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_bus = _FakeBus()
    second_bus = _FakeBus()
    _patch_message_bus(monkeypatch, [first_bus, second_bus])
    dbus = SessionDBus()

    assert await dbus.connect() is first_bus
    assert await dbus.bus() is first_bus

    dbus._introspection_cache[(DBUS_SERVICE, DBUS_PATH)] = object()
    first_bus.connected = False

    assert await dbus.connect() is second_bus
    assert dbus._introspection_cache == {}

    dbus._introspection_cache[(DBUS_SERVICE, DBUS_PATH)] = object()
    await dbus.disconnect()

    assert second_bus.disconnect_calls == 1
    assert dbus._introspection_cache == {}


@pytest.mark.asyncio
async def test_session_dbus_introspection_is_cached_and_disconnects_on_error() -> None:
    bus = _FakeBus()
    dbus = SessionDBus()
    dbus._bus = bus

    first = await dbus.introspect("org.example.Service", "/org/example/Object")
    second = await dbus.introspect("org.example.Service", "/org/example/Object")

    assert first is second
    assert bus.introspection_calls == [("org.example.Service", "/org/example/Object")]

    bus.introspection_error = RuntimeError("session bus went away")

    with pytest.raises(RuntimeError, match="session bus went away"):
        await dbus.introspect("org.example.Other", "/org/example/Other")

    assert bus.disconnect_calls == 1
    assert dbus._bus is None


@pytest.mark.asyncio
async def test_session_dbus_name_has_owner_uses_dbus_proxy_and_disconnects_on_error() -> None:
    iface = _NameOwnerInterface(result=1)
    bus = _FakeBus()
    bus.interfaces[DBUS_INTERFACE] = iface
    dbus = SessionDBus()
    dbus._bus = bus

    assert await dbus.name_has_owner("org.example.Service", timeout=0.1) is True
    assert iface.calls == ["org.example.Service"]
    assert bus.proxy_requests == [(DBUS_SERVICE, DBUS_PATH, bus.introspection)]

    error_bus = _FakeBus()
    error_bus.interfaces[DBUS_INTERFACE] = _NameOwnerInterface(
        error=RuntimeError("owner lookup failed")
    )
    error_dbus = SessionDBus()
    error_dbus._bus = error_bus

    with pytest.raises(RuntimeError, match="owner lookup failed"):
        await error_dbus.name_has_owner("org.example.Service", timeout=0.1)

    assert error_bus.disconnect_calls == 1


@pytest.mark.asyncio
async def test_session_dbus_notify_handles_success_empty_error_and_missing_replies() -> None:
    bus = _FakeBus(
        call_replies=[
            _reply("u", [42]),
            _reply(),
            _error_reply("notifications disabled"),
            None,
        ]
    )
    dbus = SessionDBus()
    dbus._bus = bus

    assert (
        await dbus.notify(
            "Saved",
            "Profile updated",
            app_name="keymasq-test",
            app_icon="dialog-information",
            timeout_ms=123,
        )
        == 42
    )
    sent = bus.messages[0]
    assert sent.destination == NOTIFICATIONS_SERVICE
    assert sent.path == NOTIFICATIONS_PATH
    assert sent.interface == NOTIFICATIONS_INTERFACE
    assert sent.member == "Notify"
    assert sent.body == [
        "keymasq-test",
        0,
        "dialog-information",
        "Saved",
        "Profile updated",
        [],
        {},
        123,
    ]

    assert await dbus.notify("Saved", "Profile updated") is None

    with pytest.raises(RuntimeError, match="notifications disabled"):
        await dbus.notify("Saved", "Profile updated")

    with pytest.raises(RuntimeError, match="notification delivery failed"):
        await dbus.notify("Saved", "Profile updated")


@pytest.mark.asyncio
async def test_session_dbus_notify_disconnects_after_call_error() -> None:
    bus = _FakeBus(call_error=RuntimeError("notifications offline"))
    dbus = SessionDBus()
    dbus._bus = bus

    with pytest.raises(RuntimeError, match="notifications offline"):
        await dbus.notify("Saved", "Profile updated")

    assert bus.disconnect_calls == 1


@pytest.mark.asyncio
async def test_temporary_session_dbus_connects_and_disconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = _FakeBus()
    _patch_message_bus(monkeypatch, [bus])

    async with dbus_module.temporary_session_dbus() as dbus:
        assert await dbus.bus() is bus

    assert bus.disconnect_calls == 1


@pytest.mark.asyncio
async def test_name_has_owner_helper_uses_supplied_or_temporary_dbus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supplied = _SuppliedDBus(True)

    assert await dbus_module.name_has_owner("org.example.Service", supplied, timeout=0.2) is True
    assert supplied.calls == [("org.example.Service", 0.2)]

    failing = _SuppliedDBus(False, error=RuntimeError("no bus"))
    assert await dbus_module.name_has_owner("org.example.Service", failing) is False

    temporary_bus = _FakeBus()
    temporary_iface = _NameOwnerInterface(result=True)
    temporary_bus.interfaces[DBUS_INTERFACE] = temporary_iface
    _patch_message_bus(monkeypatch, [temporary_bus])

    assert await dbus_module.name_has_owner("org.example.Temporary") is True
    assert temporary_iface.calls == ["org.example.Temporary"]
    assert temporary_bus.disconnect_calls == 1
