from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from keymasq.common.ipc import CommandType
from keymasq.session.listeners.base import WindowListener


class ConcreteWindowListener(WindowListener):
    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    @property
    def name(self) -> str:
        return "test"

    @classmethod
    async def probe_available(cls, dbus=None) -> bool:
        _ = dbus
        return True


async def _noop_callback(_window_class: str, _window_title: str, _tags: list[str]) -> None:
    return


@pytest.mark.asyncio
async def test_base_set_cursor_position_sends_keymasqd_cursor_command() -> None:
    client = SimpleNamespace(
        send_command=AsyncMock(return_value=SimpleNamespace(status="ok", data={"status": "ok"}))
    )
    listener = ConcreteWindowListener(_noop_callback, client=client)

    ok, message = await listener.set_cursor_position(123, 456)

    assert ok is True
    assert message == "ok"
    sent = client.send_command.await_args.args[0]
    assert sent.command == CommandType.SET_CURSOR_POSITION
    assert sent.data == {"x": 123, "y": 456}
