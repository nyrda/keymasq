import logging

import pytest
from dbus_next.errors import DBusError

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
from tests.session.support import (
    FakeIntrospectableDBusBus,
    dbus_error_reply,
    dbus_reply,
    patch_session_message_bus,
)


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


@pytest.mark.asyncio
async def test_session_dbus_reconnects_when_cached_bus_is_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_bus = FakeIntrospectableDBusBus()
    second_bus = FakeIntrospectableDBusBus()
    patch_session_message_bus(monkeypatch, dbus_module, [first_bus, second_bus])
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
    bus = FakeIntrospectableDBusBus()
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
    bus = FakeIntrospectableDBusBus()
    bus.interfaces[DBUS_INTERFACE] = iface
    dbus = SessionDBus()
    dbus._bus = bus

    assert await dbus.name_has_owner("org.example.Service", timeout=0.1) is True
    assert iface.calls == ["org.example.Service"]
    assert bus.proxy_requests == [(DBUS_SERVICE, DBUS_PATH, bus.introspection)]

    error_bus = FakeIntrospectableDBusBus()
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
    bus = FakeIntrospectableDBusBus(
        call_replies=[
            dbus_reply([42], signature="u"),
            dbus_reply(),
            dbus_error_reply("notifications disabled"),
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
    bus = FakeIntrospectableDBusBus(call_error=RuntimeError("notifications offline"))
    dbus = SessionDBus()
    dbus._bus = bus

    with pytest.raises(RuntimeError, match="notifications offline"):
        await dbus.notify("Saved", "Profile updated")

    assert bus.disconnect_calls == 1


@pytest.mark.asyncio
async def test_temporary_session_dbus_connects_and_disconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = FakeIntrospectableDBusBus()
    patch_session_message_bus(monkeypatch, dbus_module, [bus])

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

    failing = _SuppliedDBus(False, error=DBusError("org.example.Error", "no bus"))
    assert await dbus_module.name_has_owner("org.example.Service", failing) is False

    temporary_bus = FakeIntrospectableDBusBus()
    temporary_iface = _NameOwnerInterface(result=True)
    temporary_bus.interfaces[DBUS_INTERFACE] = temporary_iface
    patch_session_message_bus(monkeypatch, dbus_module, [temporary_bus])

    assert await dbus_module.name_has_owner("org.example.Temporary") is True
    assert temporary_iface.calls == ["org.example.Temporary"]
    assert temporary_bus.disconnect_calls == 1


@pytest.mark.asyncio
async def test_name_has_owner_helper_logs_expected_dbus_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failing = _SuppliedDBus(
        False,
        error=DBusError("org.example.Error", "owner lookup failed"),
    )

    with caplog.at_level(logging.DEBUG, logger="keymasq-session.dbus"):
        assert await dbus_module.name_has_owner("org.example.Service", failing) is False

    assert "D-Bus owner lookup failed for org.example.Service: owner lookup failed" in caplog.text


@pytest.mark.asyncio
async def test_name_has_owner_helper_logs_unexpected_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failing = _SuppliedDBus(False, error=RuntimeError("probe bug"))

    with caplog.at_level(logging.ERROR, logger="keymasq-session.dbus"):
        assert await dbus_module.name_has_owner("org.example.Service", failing) is False

    assert "Unexpected D-Bus owner lookup failure for org.example.Service" in caplog.text
    assert "probe bug" in caplog.text
