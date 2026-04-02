import asyncio

from keyforge.session.listeners.wayland_wlr import WlrootsWaylandListener


async def _noop_callback(_window_class: str, _window_title: str, _window_tags: list[str]) -> None:
    return


def test_wayland_wlr_probe_available(monkeypatch) -> None:
    async def _none(_cls):
        return None

    async def _some(_cls):
        return "/tmp/wayland-0"

    monkeypatch.setattr(WlrootsWaylandListener, "_pick_wayland_socket", classmethod(_none))
    assert asyncio.run(WlrootsWaylandListener.probe_available()) is False

    monkeypatch.setattr(WlrootsWaylandListener, "_pick_wayland_socket", classmethod(_some))
    assert asyncio.run(WlrootsWaylandListener.probe_available()) is True


def test_wayland_wlr_get_cursor_position_returns_none() -> None:
    listener = WlrootsWaylandListener(_noop_callback)

    async def run() -> None:
        result = await listener.get_cursor_position()
        assert result is None

    asyncio.run(run())
