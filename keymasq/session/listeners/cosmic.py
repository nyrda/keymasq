import logging
from pathlib import Path

from keymasq.session.dbus import SessionDBus
from keymasq.session.listeners._socket_helpers import (
    candidate_wayland_sockets,
    unix_socket_connectable,
)
from keymasq.session.listeners.base import WindowChangeCallback
from keymasq.session.listeners.wayland_toplevel import WaylandToplevelListener
from keymasq.session.wayland_protocols import (
    CosmicToplevelInfoWaylandClient,
    ExtForeignToplevelListTracker,
)
from keymasq.session.wayland_protocols.registry_probe import list_registry_globals

log = logging.getLogger("keymasq-session.listeners.cosmic")


class CosmicListener(
    WaylandToplevelListener[ExtForeignToplevelListTracker, CosmicToplevelInfoWaylandClient]
):
    _logger = log
    _listener_error_label = "COSMIC listener"
    _start_error_message = "COSMIC listener requires COSMIC desktop session"
    _started_log_message = "COSMIC listener started"
    _stopped_log_message = "COSMIC listener stopped"

    def __init__(
        self,
        callback: WindowChangeCallback,
        client: object | None = None,
        dbus: SessionDBus | None = None,
    ) -> None:
        super().__init__(callback, ExtForeignToplevelListTracker(), client, dbus=dbus)

    @property
    def name(self) -> str:
        return "cosmic"

    @classmethod
    async def _pick_cosmic_socket(cls) -> Path | None:
        required = {"ext_foreign_toplevel_list_v1", "zcosmic_toplevel_info_v1"}
        for socket_path in await candidate_wayland_sockets():
            if not await unix_socket_connectable(socket_path):
                continue
            globals_found = await list_registry_globals(socket_path)
            if required.issubset(globals_found):
                return socket_path
        return None

    @classmethod
    async def _pick_socket(cls) -> Path | None:
        return await cls._pick_cosmic_socket()

    def _create_client(self, socket_path: Path) -> CosmicToplevelInfoWaylandClient:
        return CosmicToplevelInfoWaylandClient(self._tracker, socket_path=str(socket_path))
