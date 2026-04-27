from typing import Any

import pytest
from dbus_next.constants import MessageType
from dbus_next.message import Message
from dbus_next.signature import Variant

from keymasq.session import gnome_shell
from keymasq.session.dbus import SessionDBus


class _FakeBus:
    def __init__(self, replies: list[Message]) -> None:
        self.replies = replies
        self.messages: list[Message] = []

    async def call(self, message: Message) -> Message:
        self.messages.append(message)
        return self.replies.pop(0)


class _FakeDBus(SessionDBus):
    def __init__(self, bus: _FakeBus) -> None:
        self._fake_bus = bus

    async def bus(self) -> Any:
        return self._fake_bus


def _method_return(signature: str, body: list[object]) -> Message:
    return Message(
        message_type=MessageType.METHOD_RETURN,
        reply_serial=1,
        signature=signature,
        body=body,
    )


@pytest.mark.asyncio
async def test_get_extension_info_uses_gnome_shell_extensions_dbus() -> None:
    bus = _FakeBus(
        [
            _method_return(
                "a{sv}",
                [
                    {
                        "uuid": Variant("s", "gnome-bridge@keymasq.tools"),
                        "enabled": Variant("b", True),
                        "shell-version": Variant("as", ["49"]),
                    }
                ],
            )
        ]
    )

    info = await gnome_shell.get_extension_info(
        "gnome-bridge@keymasq.tools",
        _FakeDBus(bus),
    )

    assert info["uuid"] == "gnome-bridge@keymasq.tools"
    assert info["enabled"] is True
    assert info["shell-version"] == ["49"]
    assert bus.messages[0].destination == gnome_shell.GNOME_EXTENSIONS_SERVICE
    assert bus.messages[0].path == gnome_shell.GNOME_EXTENSIONS_PATH
    assert bus.messages[0].interface == gnome_shell.GNOME_EXTENSIONS_INTERFACE
    assert bus.messages[0].member == "GetExtensionInfo"
    assert bus.messages[0].body == ["gnome-bridge@keymasq.tools"]


@pytest.mark.asyncio
async def test_set_user_extensions_enabled_uses_dbus_property() -> None:
    bus = _FakeBus([_method_return("", [])])

    await gnome_shell.set_user_extensions_enabled(True, _FakeDBus(bus))

    message = bus.messages[0]
    assert message.interface == gnome_shell.DBUS_PROPERTIES_INTERFACE
    assert message.member == "Set"
    assert message.body[:2] == [
        gnome_shell.GNOME_EXTENSIONS_INTERFACE,
        "UserExtensionsEnabled",
    ]
    assert isinstance(message.body[2], Variant)
    assert message.body[2].signature == "b"
    assert message.body[2].value is True


@pytest.mark.asyncio
async def test_extension_enabled_falls_back_to_active_state() -> None:
    bus = _FakeBus(
        [
            _method_return(
                "a{sv}",
                [
                    {
                        "uuid": Variant("s", "gnome-bridge@keymasq.tools"),
                        "state": Variant("d", 1.0),
                    }
                ],
            )
        ]
    )

    enabled = await gnome_shell.extension_enabled("gnome-bridge@keymasq.tools", _FakeDBus(bus))

    assert enabled is True
