import asyncio
from pathlib import Path
from typing import ClassVar

from keymasq.session.listeners.base import WindowChangeCallback
from keymasq.session.listeners.wayland_toplevel import WaylandToplevelListener


class _FakeTracker:
    def __init__(self) -> None:
        self._active_window = ("initial.app", "Initial")
        self._updates: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

    def get_active_window(self) -> tuple[str, str]:
        return self._active_window

    async def next_active_window(
        self,
        timeout: float | None = None,
    ) -> tuple[str, str] | None:
        _ = timeout
        return await self._updates.get()

    def emit_active_window(self, window_class: str, window_title: str) -> None:
        self._active_window = (window_class, window_title)
        self._updates.put_nowait(self._active_window)


class _FakeClient:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def run(self) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        await future


class _FakeListener(WaylandToplevelListener[_FakeTracker, _FakeClient]):
    _socket_path: ClassVar[Path] = Path("/tmp/keymasq-wayland-test")

    def __init__(self, callback: WindowChangeCallback) -> None:
        self.created_client: _FakeClient | None = None
        super().__init__(callback, _FakeTracker())

    @property
    def name(self) -> str:
        return "fake-wayland"

    @property
    def tracker(self) -> _FakeTracker:
        return self._tracker

    @classmethod
    async def _pick_socket(cls) -> Path | None:
        return cls._socket_path

    def _create_client(self, socket_path: Path) -> _FakeClient:
        assert socket_path == self._socket_path
        self.created_client = _FakeClient()
        return self.created_client


def test_wayland_toplevel_listener_lifecycle_forwards_active_window_changes() -> None:
    async def run() -> None:
        events: asyncio.Queue[tuple[str, str, list[str]]] = asyncio.Queue()

        async def callback(
            window_class: str,
            window_title: str,
            window_tags: list[str],
        ) -> None:
            events.put_nowait((window_class, window_title, window_tags))

        listener = _FakeListener(callback)
        await listener.start()

        assert await asyncio.wait_for(events.get(), timeout=1) == (
            "initial.app",
            "Initial",
            [],
        )

        client = listener.created_client
        assert client is not None
        assert client.started is True

        listener.tracker.emit_active_window("next.app", "Next")
        assert await asyncio.wait_for(events.get(), timeout=1) == ("next.app", "Next", [])

        await listener.stop()

        assert listener.running is False
        assert client.stopped is True

    asyncio.run(run())
