import asyncio
import logging
from abc import abstractmethod
from pathlib import Path
from typing import ClassVar, Protocol

from keymasq.common.slurp import get_slurp_capture
from keymasq.session.dbus import SessionDBus
from keymasq.session.listeners.base import WindowChangeCallback, WindowListener

log = logging.getLogger("keymasq-session.listeners.wayland_toplevel")


class WaylandToplevelTracker(Protocol):
    def get_active_window(self) -> tuple[str, str]: ...

    async def next_active_window(
        self,
        timeout: float | None = None,
    ) -> tuple[str, str] | None: ...


class WaylandToplevelClient(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def run(self) -> None: ...


class WaylandToplevelListener[
    TrackerT: WaylandToplevelTracker,
    ClientT: WaylandToplevelClient,
](WindowListener):
    _logger: ClassVar[logging.Logger] = log
    _listener_error_label: ClassVar[str] = "Wayland listener"
    _start_error_message: ClassVar[str] = "Wayland listener requires an active Wayland socket"
    _started_log_message: ClassVar[str] = "Wayland listener started"
    _stopped_log_message: ClassVar[str] = "Wayland listener stopped"
    _slurp_compositor: ClassVar[str] = "wayland-wlr"

    def __init__(
        self,
        callback: WindowChangeCallback,
        tracker: TrackerT,
        client: object | None = None,
        dbus: SessionDBus | None = None,
    ) -> None:
        super().__init__(callback, client, dbus=dbus)
        self._tracker = tracker
        self._client: ClientT | None = None
        self._forward_task: asyncio.Task[None] | None = None
        self._last_class = ""
        self._last_title = ""
        self._slurp = get_slurp_capture()
        self._slurp.set_compositor(self._slurp_compositor)

    @classmethod
    @abstractmethod
    async def _pick_socket(cls) -> Path | None:
        raise NotImplementedError

    @abstractmethod
    def _create_client(self, socket_path: Path) -> ClientT:
        raise NotImplementedError

    @classmethod
    async def probe_available(cls, dbus: SessionDBus | None = None) -> bool:
        _ = dbus
        return await cls._pick_socket() is not None

    async def start(self) -> None:
        socket_path = await self.__class__._pick_socket()
        if socket_path is None:
            raise RuntimeError(self._start_error_message)

        self._client = self._create_client(socket_path)
        await self._client.start()

        self.running = True
        self._task = asyncio.create_task(self._listen())
        self._forward_task = asyncio.create_task(self._forward_active_window_changes())

        initial_class, initial_title = self._tracker.get_active_window()
        await self._emit_active_window_if_changed(initial_class, initial_title)
        self._logger.info(self._started_log_message)

    async def stop(self) -> None:
        self.running = False

        if self._forward_task:
            self._forward_task.cancel()
            try:
                await self._forward_task
            except asyncio.CancelledError:
                pass

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        if self._client is not None:
            await self._client.stop()
            self._client = None
        self._logger.info(self._stopped_log_message)

    async def _listen(self) -> None:
        try:
            if self._client is None:
                return
            await self._client.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception("%s error", self._listener_error_label)

    async def health_check(self) -> bool:
        if not await super().health_check():
            return False
        return await self.__class__._pick_socket() is not None

    async def _forward_active_window_changes(self) -> None:
        while self.running:
            update = await self._tracker.next_active_window()
            if update is None:
                continue
            await self._emit_active_window_if_changed(update[0], update[1])

    async def _emit_active_window_if_changed(
        self,
        window_class: str,
        window_title: str,
    ) -> None:
        if window_class == self._last_class and window_title == self._last_title:
            return

        self._last_class = window_class
        self._last_title = window_title
        self._logger.debug("Active window changed: app_id=%s, title=%s", window_class, window_title)
        await self.callback(window_class, window_title, [])

    async def get_active_window(self) -> tuple[str, str, list[str]]:
        window_class, window_title = self._tracker.get_active_window()
        return window_class, window_title, []
