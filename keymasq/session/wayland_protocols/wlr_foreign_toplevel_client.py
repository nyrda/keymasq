import logging
import struct

from keymasq.session.wayland_protocols._active_window_tracker import ActiveWindowTracker
from keymasq.session.wayland_protocols.client_transport import (
    WaylandClientTransport,
    WaylandDisplayError,
    decode_array,
    decode_string,
    decode_uint_array,
)

WLR_FOREIGN_TOPLEVEL_MANAGER_INTERFACE = "zwlr_foreign_toplevel_manager_v1"
WLR_TOPLEVEL_STATE_ACTIVATED = 2

log = logging.getLogger("keymasq-session.wayland.wlr_foreign_toplevel")


class WlrForeignToplevelWaylandClient(WaylandClientTransport):
    def __init__(
        self,
        tracker: ActiveWindowTracker,
        socket_path: str | None = None,
    ) -> None:
        super().__init__(socket_path)
        self._tracker = tracker
        self._manager_id: int | None = None
        self._toplevel_handles: set[int] = set()

    def _check_required_globals(self) -> None:
        if self._manager_id is None:
            raise RuntimeError("zwlr_foreign_toplevel_manager_v1 is unavailable on this compositor")

    async def stop(self) -> None:
        self._running = False
        if self._socket is not None:
            if self._manager_id is not None:
                for handle_id in list(self._toplevel_handles):
                    await self._destroy_toplevel_handle(handle_id)
                    self._tracker.close_toplevel(str(handle_id))
                self._toplevel_handles.clear()
                try:
                    await self._send_request(self._manager_id, 0, b"")
                except (OSError, WaylandDisplayError):
                    log.debug("Failed to destroy wlr foreign toplevel manager", exc_info=True)
                except Exception:
                    log.exception("Unexpected failure destroying wlr foreign toplevel manager")
                self._objects.pop(self._manager_id, None)
                self._manager_id = None
            self._close_socket()

    async def _dispatch_protocol_event(
        self,
        interface: str,
        object_id: int,
        opcode: int,
        payload: bytes,
    ) -> None:
        if interface == WLR_FOREIGN_TOPLEVEL_MANAGER_INTERFACE:
            self._handle_manager_event(object_id, opcode, payload)
            return
        if interface == "zwlr_foreign_toplevel_handle_v1":
            await self._handle_toplevel_event(object_id, opcode, payload)

    def _on_object_deleted(self, object_id: int) -> None:
        self._toplevel_handles.discard(object_id)

    async def _handle_registry_global(
        self,
        registry_id: int,
        global_name: int,
        interface_name: str,
        version: int,
    ) -> None:
        if interface_name != WLR_FOREIGN_TOPLEVEL_MANAGER_INTERFACE:
            return
        if self._manager_id is not None:
            return

        bind_version = max(1, min(int(version), 3))
        manager_id = self._allocate_object_id(WLR_FOREIGN_TOPLEVEL_MANAGER_INTERFACE)
        await self._bind_registry_global(
            registry_id,
            global_name,
            WLR_FOREIGN_TOPLEVEL_MANAGER_INTERFACE,
            bind_version,
            manager_id,
        )
        self._manager_id = manager_id

    def _handle_manager_event(self, object_id: int, opcode: int, payload: bytes) -> None:
        if object_id != self._manager_id:
            return
        if opcode != 0:
            return

        (handle_object_id,) = struct.unpack_from("<I", payload, 0)
        self._add_object(handle_object_id, "zwlr_foreign_toplevel_handle_v1")
        self._toplevel_handles.add(handle_object_id)
        self._tracker.add_toplevel(str(handle_object_id))

    async def _destroy_toplevel_handle(self, object_id: int) -> None:
        try:
            await self._send_request(object_id, 7, b"")
        except (OSError, WaylandDisplayError):
            log.debug("Failed to destroy wlr foreign toplevel handle", exc_info=True)
        except Exception:
            log.exception(
                "Unexpected failure destroying wlr foreign toplevel handle %s",
                object_id,
            )
        self._objects.pop(object_id, None)
        self._toplevel_handles.discard(object_id)

    async def _handle_toplevel_event(self, object_id: int, opcode: int, payload: bytes) -> None:
        handle_id = str(object_id)
        if opcode == 0:
            title, _ = decode_string(payload, 0)
            self._tracker.update_title(handle_id, title)
            return
        if opcode == 1:
            app_id, _ = decode_string(payload, 0)
            self._tracker.update_app_id(handle_id, app_id)
            return
        if opcode == 4:
            state_data, _ = decode_array(payload, 0)
            self._tracker.update_state(handle_id, decode_uint_array(state_data))
            return
        if opcode == 6:
            self._tracker.close_toplevel(handle_id)
            await self._destroy_toplevel_handle(object_id)
