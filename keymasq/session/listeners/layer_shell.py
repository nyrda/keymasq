import asyncio
import logging
from pathlib import Path

from keymasq.session.dbus import SessionDBus
from keymasq.session.listeners._socket_helpers import (
    candidate_wayland_sockets,
    unix_socket_connectable,
)
from keymasq.session.listeners.base import WindowChangeCallback, WindowListener
from keymasq.session.wayland_protocols.layer_shell_cursor import (
    LayerShellCursorTracker,
)
from keymasq.session.wayland_protocols.registry_probe import list_registry_globals

log = logging.getLogger("keymasq-session.listeners.layer_shell")

LAYER_SHELL_CURSOR_REQUIRED_GLOBALS = {
    "wl_compositor",
    "wl_output",
    "wl_seat",
    "wl_shm",
    "zxdg_output_manager_v1",
    "zwlr_layer_shell_v1",
}


async def pick_layer_shell_cursor_socket() -> Path | None:
    for socket_path in await candidate_wayland_sockets():
        if not await unix_socket_connectable(socket_path):
            continue
        globals_found = await list_registry_globals(socket_path)
        if LAYER_SHELL_CURSOR_REQUIRED_GLOBALS.issubset(globals_found):
            return socket_path
    return None


class LayerShellCursorListener(WindowListener):
    def __init__(
        self,
        callback: WindowChangeCallback,
        client: object | None = None,
        dbus: SessionDBus | None = None,
    ) -> None:
        super().__init__(callback, client, dbus=dbus)
        self._cursor_tracker: LayerShellCursorTracker | None = None

    @property
    def name(self) -> str:
        return "wayland-layer-shell"

    @property
    def supports_realtime_cursor_position(self) -> bool:
        tracker = self._cursor_tracker
        return bool(tracker is not None and tracker.supports_cursor_tracking)

    @classmethod
    async def probe_available(cls, dbus: SessionDBus | None = None) -> bool:
        _ = dbus
        return await pick_layer_shell_cursor_socket() is not None

    async def start(self) -> None:
        socket_path = await pick_layer_shell_cursor_socket()
        if socket_path is None:
            raise RuntimeError("Wayland layer-shell cursor listener requires layer-shell")

        tracker = LayerShellCursorTracker(self.client, socket_path=str(socket_path))
        try:
            await tracker.start()
        except Exception:
            await tracker.stop()
            raise

        self._cursor_tracker = tracker
        self.running = True
        self._task = asyncio.create_task(
            tracker.run(),
            name="keymasq-session:wayland-layer-shell-cursor",
        )
        log.info("Wayland layer-shell cursor listener started")

    async def stop(self) -> None:
        self.running = False
        tracker = self._cursor_tracker
        self._cursor_tracker = None
        if tracker is not None:
            await tracker.stop()

        task = self._task
        self._task = None
        if task is not None:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except (OSError, RuntimeError):
                log.debug("Wayland layer-shell cursor read loop stopped", exc_info=True)
        log.info("Wayland layer-shell cursor listener stopped")

    async def get_cursor_position(self) -> tuple[int, int] | None:
        tracker = self._cursor_tracker
        if tracker is None:
            return None
        return await tracker.get_cursor_position()

    async def prepare_cursor_position_tracking(self, duration_ms: int) -> None:
        tracker = self._cursor_tracker
        if tracker is not None:
            await tracker.prepare_cursor_position_tracking(duration_ms)

    async def stop_cursor_position_tracking(self) -> None:
        tracker = self._cursor_tracker
        if tracker is not None:
            await tracker.stop_cursor_position_tracking()
