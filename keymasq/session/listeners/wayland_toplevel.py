import asyncio
import logging
from abc import abstractmethod
from pathlib import Path
from typing import ClassVar, Protocol

from keymasq.session.dbus import SessionDBus
from keymasq.session.listeners.base import WindowChangeCallback, WindowListener
from keymasq.session.wayland_protocols.layer_shell_cursor import LayerShellCursorTracker

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
        self._cursor_tracker: LayerShellCursorTracker | None = None
        self._cursor_task: asyncio.Task[None] | None = None
        self._last_class = ""
        self._last_title = ""

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
        await self._start_cursor_tracker(socket_path)

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

        await self._stop_cursor_tracker()

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

    @property
    def supports_realtime_cursor_position(self) -> bool:
        tracker = self._cursor_tracker
        return bool(tracker is not None and tracker.supports_cursor_tracking)

    async def prepare_cursor_position_tracking(self, duration_ms: int) -> None:
        tracker = self._cursor_tracker
        if tracker is not None:
            await tracker.prepare_cursor_position_tracking(duration_ms)

    async def get_cursor_position(self) -> tuple[int, int] | None:
        tracker = self._cursor_tracker
        if tracker is None:
            return None
        return await tracker.get_cursor_position()

    async def stop_cursor_position_tracking(self) -> None:
        tracker = self._cursor_tracker
        if tracker is not None:
            await tracker.stop_cursor_position_tracking()

    async def _start_cursor_tracker(self, socket_path: Path) -> None:
        tracker = LayerShellCursorTracker(self.client, socket_path=str(socket_path))
        try:
            await tracker.start()
        except (OSError, RuntimeError):
            self._logger.debug(
                "%s layer-shell cursor tracker unavailable",
                self._listener_error_label,
                exc_info=True,
            )
            await tracker.stop()
            return
        except Exception:
            self._logger.exception(
                "%s layer-shell cursor tracker failed",
                self._listener_error_label,
            )
            await tracker.stop()
            return

        self._cursor_tracker = tracker
        self._cursor_task = asyncio.create_task(
            tracker.run(),
            name=f"keymasq-session:{self.name}-layer-cursor",
        )

    async def _stop_cursor_tracker(self) -> None:
        tracker = self._cursor_tracker
        self._cursor_tracker = None
        if tracker is not None:
            await tracker.stop()

        task = self._cursor_task
        self._cursor_task = None
        if task is not None:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except (OSError, RuntimeError):
                self._logger.debug("Layer-shell cursor read loop stopped", exc_info=True)
