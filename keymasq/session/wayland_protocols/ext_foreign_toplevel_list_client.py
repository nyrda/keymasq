import struct

from keymasq.session.wayland_protocols import client_transport as _transport
from keymasq.session.wayland_protocols.ext_foreign_toplevel_list import (
    ExtForeignToplevelListTracker,
)

WL_DISPLAY_OBJECT_ID = _transport.WL_DISPLAY_OBJECT_ID
EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE = "ext_foreign_toplevel_list_v1"
EXT_FOREIGN_TOPLEVEL_HANDLE_INTERFACE = "ext_foreign_toplevel_handle_v1"
_pack_uint = _transport.pack_uint
_encode_string = _transport.encode_string
_decode_string = _transport.decode_string


class ExtForeignToplevelListClientBase(_transport.WaylandClientTransport):
    def __init__(
        self,
        tracker: ExtForeignToplevelListTracker,
        socket_path: str | None = None,
    ) -> None:
        super().__init__(socket_path)
        self._tracker = tracker
        self._list_id: int | None = None
        self._toplevel_handles: set[int] = set()

    def _check_required_globals(self) -> None:
        if self._list_id is None:
            raise RuntimeError("ext_foreign_toplevel_list_v1 is unavailable on this compositor")

    def _after_start_sync(self) -> None:
        self._tracker.mark_done()

    async def stop(self) -> None:
        self._running = False
        if self._socket is None:
            return

        await self._stop_extra_globals()
        if self._list_id is not None:
            for handle_id in list(self._toplevel_handles):
                await self._destroy_toplevel_handle(handle_id)
            try:
                await self._send_request(self._list_id, 1, b"")
            except Exception:
                pass
            self._objects.pop(self._list_id, None)
            self._list_id = None

        self._close_socket()

    async def _dispatch_protocol_event(
        self,
        interface: str,
        object_id: int,
        opcode: int,
        payload: bytes,
    ) -> None:
        if interface == EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE:
            await self._handle_list_event(object_id, opcode, payload)
            return
        if interface == EXT_FOREIGN_TOPLEVEL_HANDLE_INTERFACE:
            await self._handle_toplevel_event(object_id, opcode, payload)
            return
        await self._dispatch_extra_protocol_event(interface, object_id, opcode, payload)

    def _on_object_deleted(self, object_id: int) -> None:
        self._toplevel_handles.discard(object_id)
        self._after_toplevel_object_deleted(object_id)

    async def _handle_registry_global(
        self,
        registry_id: int,
        global_name: int,
        interface_name: str,
        version: int,
    ) -> None:
        if await self._bind_ext_foreign_toplevel_list_global(
            registry_id,
            global_name,
            interface_name,
            version,
        ):
            return
        await self._handle_extra_registry_global(
            registry_id,
            global_name,
            interface_name,
            version,
        )

    async def _bind_ext_foreign_toplevel_list_global(
        self,
        registry_id: int,
        global_name: int,
        interface_name: str,
        version: int,
    ) -> bool:
        if interface_name != EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE:
            return False
        if self._list_id is not None:
            return True

        bind_version = max(1, min(int(version), 1))
        list_id = self._allocate_object_id(EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE)
        await self._bind_registry_global(
            registry_id,
            global_name,
            EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE,
            bind_version,
            list_id,
        )
        self._list_id = list_id
        return True

    async def _handle_list_event(self, object_id: int, opcode: int, payload: bytes) -> None:
        if object_id != self._list_id:
            return

        if opcode == 0:
            (handle_object_id,) = struct.unpack_from("<I", payload, 0)
            self._add_object(handle_object_id, EXT_FOREIGN_TOPLEVEL_HANDLE_INTERFACE)
            self._toplevel_handles.add(handle_object_id)
            self._tracker.add_toplevel(str(handle_object_id))
            await self._after_toplevel_added(handle_object_id)
            return

        if opcode == 1:
            self._running = False

    async def _destroy_toplevel_handle(self, object_id: int) -> None:
        await self._before_destroy_toplevel_handle(object_id)
        try:
            await self._send_request(object_id, 0, b"")
        except Exception:
            pass
        self._objects.pop(object_id, None)
        self._toplevel_handles.discard(object_id)

    async def _handle_toplevel_event(self, object_id: int, opcode: int, payload: bytes) -> None:
        handle_id = str(object_id)

        if opcode == 0:
            self._tracker.close_toplevel(handle_id)
            await self._destroy_toplevel_handle(object_id)
            return

        if opcode == 1:
            return

        if opcode == 2:
            title, _ = _decode_string(payload, 0)
            self._tracker.update_title(handle_id, title)
            return

        if opcode == 3:
            app_id, _ = _decode_string(payload, 0)
            self._tracker.update_app_id(handle_id, app_id)
            return

        if opcode == 4:
            return

    async def _stop_extra_globals(self) -> None:
        pass

    async def _dispatch_extra_protocol_event(
        self,
        interface: str,
        object_id: int,
        opcode: int,
        payload: bytes,
    ) -> None:
        pass

    async def _handle_extra_registry_global(
        self,
        registry_id: int,
        global_name: int,
        interface_name: str,
        version: int,
    ) -> None:
        pass

    async def _after_toplevel_added(self, object_id: int) -> None:
        pass

    async def _before_destroy_toplevel_handle(self, object_id: int) -> None:
        pass

    def _after_toplevel_object_deleted(self, object_id: int) -> None:
        pass


class ExtForeignToplevelListWaylandClient(ExtForeignToplevelListClientBase):
    pass
