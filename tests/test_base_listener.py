import pytest

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
async def test_base_set_cursor_position_reports_unsupported() -> None:
    listener = ConcreteWindowListener(_noop_callback)

    ok, message = await listener.set_cursor_position(123, 456)

    assert ok is False
    assert message == "test does not implement compositor cursor positioning"
