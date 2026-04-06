import asyncio
import logging

from keyforge.common.slurp import SlurpMode, get_slurp_capture
from keyforge.session.dbus import SessionDBus
from keyforge.session.listeners.base import WindowChangeCallback, WindowListener
from keyforge.session.listeners.wayland_wlr import WlrootsWaylandListener
from keyforge.session.slurp import trigger_slurp_macro
from keyforge.session.wayland_protocols import (
    CosmicToplevelInfoWaylandClient,
    ExtForeignToplevelListTracker,
)
from keyforge.session.wayland_protocols.registry_probe import list_registry_globals

log = logging.getLogger("keyforge-session.listeners.cosmic")


class CosmicListener(WindowListener):
    def __init__(
        self,
        callback: WindowChangeCallback,
        client=None,
        dbus: SessionDBus | None = None,
    ) -> None:
        super().__init__(callback, client, dbus=dbus)
        self._tracker = ExtForeignToplevelListTracker()
        self._client: CosmicToplevelInfoWaylandClient | None = None
        self._forward_task: asyncio.Task | None = None
        self._last_class = ""
        self._last_title = ""
        self._slurp = get_slurp_capture()
        self._slurp.set_compositor("wayland-wlr")

    @property
    def name(self) -> str:
        return "cosmic"

    @classmethod
    async def probe_available(cls, dbus: SessionDBus | None = None) -> bool:
        _ = dbus
        return await cls._pick_cosmic_socket() is not None

    @classmethod
    async def _pick_cosmic_socket(cls):
        required = {"ext_foreign_toplevel_list_v1", "zcosmic_toplevel_info_v1"}
        for socket_path in WlrootsWaylandListener._candidate_wayland_sockets():
            if not await WlrootsWaylandListener._socket_connectable(socket_path):
                continue
            globals_found = await list_registry_globals(socket_path)
            if required.issubset(globals_found):
                return socket_path
        return None

    async def start(self) -> None:
        if not await self.__class__.probe_available():
            raise RuntimeError("COSMIC listener requires COSMIC desktop session")

        socket_path = await self.__class__._pick_cosmic_socket()
        if socket_path is None:
            raise RuntimeError(
                "COSMIC listener requires ext_foreign_toplevel_list_v1 and zcosmic_toplevel_info_v1"
            )

        self._client = CosmicToplevelInfoWaylandClient(self._tracker, socket_path=str(socket_path))
        await self._client.start()

        self.running = True
        self._task = asyncio.create_task(self._listen())
        self._forward_task = asyncio.create_task(self._forward_active_window_changes())

        initial_class, initial_title = self._tracker.get_active_window()
        await self._emit_active_window_if_changed(initial_class, initial_title)
        log.info("COSMIC listener started")

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
        log.info("COSMIC listener stopped")

    async def _listen(self) -> None:
        try:
            if self._client is None:
                return
            await self._client.run()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"COSMIC listener error: {e}")

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
            return None
        client = self.client
        if client is None:
            return None
        try:
            result = await self._slurp.capture_point_async(
                mode=SlurpMode.POINT_IMMEDIATE,
                on_ready=lambda: trigger_slurp_macro(client),
            )
            if result:
                return (result.x, result.y)
        except Exception:
            pass
        return None

    async def health_check(self) -> bool:
        if not await super().health_check():
            return False
        return await self.__class__.probe_available()
