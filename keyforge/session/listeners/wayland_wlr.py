import asyncio
import logging
import os
from pathlib import Path

from keyforge.common.slurp import SlurpMode, get_slurp_capture
from keyforge.session.dbus import SessionDBus
from keyforge.session.listeners.base import WindowChangeCallback, WindowListener
from keyforge.session.slurp import trigger_slurp_macro
from keyforge.session.wayland_protocols import (
    WlrForeignToplevelManagerTracker,
    WlrForeignToplevelWaylandClient,
)
from keyforge.session.wayland_protocols.registry_probe import list_registry_globals

log = logging.getLogger("keyforge-session.listeners.wayland_wlr")


class WlrootsWaylandListener(WindowListener):
    def __init__(
        self,
        callback: WindowChangeCallback,
        client=None,
        dbus: SessionDBus | None = None,
    ) -> None:
        super().__init__(callback, client, dbus=dbus)
        self._tracker = WlrForeignToplevelManagerTracker()
        self._client: WlrForeignToplevelWaylandClient | None = None
        self._forward_task: asyncio.Task[None] | None = None
        self._last_class = ""
        self._last_title = ""
        self._slurp = get_slurp_capture()
        self._slurp.set_compositor("wayland-wlr")

    @property
    def name(self) -> str:
        return "wayland-wlr"

    @classmethod
    def _runtime_dir(cls) -> Path:
        env_dir = os.environ.get("XDG_RUNTIME_DIR")
        if env_dir:
            return Path(env_dir)
        return Path(f"/run/user/{os.getuid()}")

    @classmethod
    def _candidate_wayland_sockets(cls) -> list[Path]:
        runtime_dir = cls._runtime_dir()
        if not runtime_dir.exists():
            return []
        sockets: list[Path] = []
        for path in runtime_dir.glob("wayland-*"):
            if path.is_socket():
                sockets.append(path)
        sockets.sort(key=lambda p: p.name)
        return sockets

    @classmethod
    async def _socket_connectable(cls, socket_path: Path, timeout_s: float = 0.2) -> bool:
        try:
            connect_coro = asyncio.open_unix_connection(path=str(socket_path))
            _, writer = await asyncio.wait_for(connect_coro, timeout=timeout_s)
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False

    @classmethod
    async def _pick_wayland_socket(cls) -> Path | None:
        required = {"zwlr_foreign_toplevel_manager_v1"}
        for path in cls._candidate_wayland_sockets():
            if await cls._socket_connectable(path):
                globals_found = await list_registry_globals(path)
                if required.issubset(globals_found):
                    return path
        return None

    @classmethod
    async def probe_available(cls, dbus: SessionDBus | None = None) -> bool:
        _ = dbus
        return await cls._pick_wayland_socket() is not None

    async def start(self) -> None:
        socket_path = await self.__class__._pick_wayland_socket()
        if socket_path is None:
            raise RuntimeError("Wayland listener requires an active Wayland socket")

        self._client = WlrForeignToplevelWaylandClient(self._tracker, socket_path=str(socket_path))
        await self._client.start()

        self.running = True
        self._task = asyncio.create_task(self._listen())
        self._forward_task = asyncio.create_task(self._forward_active_window_changes())

        initial_class, initial_title = self._tracker.get_active_window()
        await self._emit_active_window_if_changed(initial_class, initial_title)
        log.info("Wayland wlr listener started")

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
        log.info("Wayland wlr listener stopped")

    async def _listen(self) -> None:
        try:
            if self._client is None:
                return
            await self._client.run()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"Wayland wlr listener error: {e}")

    async def health_check(self) -> bool:
        if not await super().health_check():
            return False
        return await self.__class__._pick_wayland_socket() is not None

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
        log.debug("Active window changed: app_id=%s, title=%s", window_class, window_title)
        await self.callback(window_class, window_title, [])

    async def get_active_window(self) -> tuple[str, str, list[str]]:
        window_class, window_title = self._tracker.get_active_window()
        return window_class, window_title, []

    async def get_cursor_position(self) -> tuple[int, int] | None:
        if not self._slurp.available:
            log.debug("Slurp cursor capture not available")
            return None

        client = self.client
        if client is None:
            log.debug("Slurp cursor capture requires client connection")
            return None

        try:
            result = await self._slurp.capture_point_async(
                mode=SlurpMode.POINT_IMMEDIATE,
                on_ready=lambda: trigger_slurp_macro(client),
            )
            if result:
                return (result.x, result.y)
        except Exception as e:
            log.debug("Slurp cursor capture failed: %s", e)
        return None
