import logging
from pathlib import Path

from keymasq.session.dbus import SessionDBus
from keymasq.session.listeners._socket_helpers import (
    candidate_wayland_sockets,
    runtime_dir,
    unix_socket_connectable,
)
from keymasq.session.listeners.base import WindowChangeCallback
from keymasq.session.listeners.wayland_toplevel import WaylandToplevelListener
from keymasq.session.wayland_protocols._active_window_tracker import ActiveWindowTracker
from keymasq.session.wayland_protocols.registry_probe import list_registry_globals
from keymasq.session.wayland_protocols.wlr_foreign_toplevel_client import (
    WLR_TOPLEVEL_STATE_ACTIVATED,
    WlrForeignToplevelWaylandClient,
)

log = logging.getLogger("keymasq-session.listeners.wayland_wlr")


class WlrootsWaylandListener(
    WaylandToplevelListener[ActiveWindowTracker, WlrForeignToplevelWaylandClient]
):
    _logger = log
    _listener_error_label = "Wayland wlr listener"
    _started_log_message = "Wayland wlr listener started"
    _stopped_log_message = "Wayland wlr listener stopped"

    def __init__(
        self,
        callback: WindowChangeCallback,
        client: object | None = None,
        dbus: SessionDBus | None = None,
    ) -> None:
        tracker = ActiveWindowTracker(activated_state=WLR_TOPLEVEL_STATE_ACTIVATED)
        super().__init__(callback, tracker, client, dbus=dbus)

    @property
    def name(self) -> str:
        return "wayland-wlr"

    @classmethod
    def _runtime_dir(cls) -> Path:
        return runtime_dir()

    @classmethod
    async def candidate_wayland_sockets(cls) -> list[Path]:
        return await candidate_wayland_sockets()

    @classmethod
    async def socket_connectable(cls, socket_path: Path, timeout_s: float = 0.2) -> bool:
        return await unix_socket_connectable(socket_path, timeout_s=timeout_s)

    @classmethod
    async def _pick_wayland_socket(cls) -> Path | None:
        required = {"zwlr_foreign_toplevel_manager_v1"}
        for path in await cls.candidate_wayland_sockets():
            if await cls.socket_connectable(path):
                globals_found = await list_registry_globals(path)
                if required.issubset(globals_found):
                    return path
        return None

    @classmethod
    async def _pick_socket(cls) -> Path | None:
        return await cls._pick_wayland_socket()

    def _create_client(self, socket_path: Path) -> WlrForeignToplevelWaylandClient:
        return WlrForeignToplevelWaylandClient(self._tracker, socket_path=str(socket_path))
