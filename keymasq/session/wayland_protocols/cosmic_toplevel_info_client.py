from keymasq.session.wayland_protocols import client_transport as _transport
from keymasq.session.wayland_protocols.ext_foreign_toplevel_list import (
    ExtForeignToplevelListTracker,
)
from keymasq.session.wayland_protocols.ext_foreign_toplevel_list_client import (
    EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE as EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE,
)
from keymasq.session.wayland_protocols.ext_foreign_toplevel_list_client import (
    ExtForeignToplevelListClientBase,
)

WL_DISPLAY_OBJECT_ID = _transport.WL_DISPLAY_OBJECT_ID
COSMIC_TOPLEVEL_INFO_INTERFACE = "zcosmic_toplevel_info_v1"
COSMIC_TOPLEVEL_HANDLE_INTERFACE = "zcosmic_toplevel_handle_v1"
_pack_uint = _transport.pack_uint
_encode_string = _transport.encode_string
_decode_array = _transport.decode_array
_decode_uint_array = _transport.decode_uint_array


class CosmicToplevelInfoWaylandClient(ExtForeignToplevelListClientBase):
    def __init__(
        self,
        tracker: ExtForeignToplevelListTracker,
        socket_path: str | None = None,
    ) -> None:
        super().__init__(tracker, socket_path)
        self._cosmic_info_id: int | None = None
        self._cosmic_handles: set[int] = set()
        self._ext_to_cosmic: dict[int, int] = {}
        self._cosmic_to_ext: dict[int, int] = {}

    def _check_required_globals(self) -> None:
        super()._check_required_globals()
        if self._cosmic_info_id is None:
            raise RuntimeError("zcosmic_toplevel_info_v1 is unavailable on this compositor")

    def _after_start_sync(self) -> None:
        pass

    async def _stop_extra_globals(self) -> None:
        if self._cosmic_info_id is not None:
            for cosmic_id in list(self._cosmic_handles):
                await self._destroy_cosmic_handle(cosmic_id)
            self._ext_to_cosmic.clear()
            self._cosmic_to_ext.clear()
            self._objects.pop(self._cosmic_info_id, None)
            self._cosmic_info_id = None

    async def _dispatch_extra_protocol_event(
        self,
        interface: str,
        object_id: int,
        opcode: int,
        payload: bytes,
    ) -> None:
        if interface == COSMIC_TOPLEVEL_INFO_INTERFACE:
            self._handle_cosmic_info_event(opcode)
            return
        if interface == COSMIC_TOPLEVEL_HANDLE_INTERFACE:
            self._handle_cosmic_toplevel_event(object_id, opcode, payload)

    def _after_toplevel_object_deleted(self, object_id: int) -> None:
        cosmic_id = self._ext_to_cosmic.pop(object_id, None)
        if cosmic_id is not None:
            self._cosmic_to_ext.pop(cosmic_id, None)
            self._cosmic_handles.discard(cosmic_id)

    async def _handle_extra_registry_global(
        self,
        registry_id: int,
        global_name: int,
        interface_name: str,
        version: int,
    ) -> None:
        if interface_name == COSMIC_TOPLEVEL_INFO_INTERFACE and self._cosmic_info_id is None:
            advertised_version = int(version)
            if advertised_version < 2:
                return

            bind_version = min(advertised_version, 3)
            cosmic_id = self._allocate_object_id(COSMIC_TOPLEVEL_INFO_INTERFACE)
            await self._bind_registry_global(
                registry_id,
                global_name,
                COSMIC_TOPLEVEL_INFO_INTERFACE,
                bind_version,
                cosmic_id,
            )
            self._cosmic_info_id = cosmic_id

    async def _after_toplevel_added(self, object_id: int) -> None:
        if self._cosmic_info_id is None:
            return

        cosmic_handle_id = self._allocate_object_id(COSMIC_TOPLEVEL_HANDLE_INTERFACE)
        request_payload = _pack_uint(cosmic_handle_id) + _pack_uint(object_id)
        await self._send_request(self._cosmic_info_id, 1, request_payload)
        self._cosmic_handles.add(cosmic_handle_id)
        self._ext_to_cosmic[object_id] = cosmic_handle_id
        self._cosmic_to_ext[cosmic_handle_id] = object_id

    def _handle_cosmic_info_event(self, opcode: int) -> None:
        if opcode == 2:
            self._tracker.mark_done()

    async def _destroy_cosmic_handle(self, object_id: int) -> None:
        try:
            await self._send_request(object_id, 0, b"")
        except Exception:
            pass
        self._objects.pop(object_id, None)
        self._cosmic_handles.discard(object_id)
        ext_id = self._cosmic_to_ext.pop(object_id, None)
        if ext_id is not None and self._ext_to_cosmic.get(ext_id) == object_id:
            self._ext_to_cosmic.pop(ext_id, None)

    async def _before_destroy_toplevel_handle(self, object_id: int) -> None:
        cosmic_id = self._ext_to_cosmic.get(object_id)
        if cosmic_id is not None:
            await self._destroy_cosmic_handle(cosmic_id)

    def _handle_cosmic_toplevel_event(self, object_id: int, opcode: int, payload: bytes) -> None:
        ext_id = self._cosmic_to_ext.get(object_id)
        if ext_id is None:
            return
        if opcode != 8:
            return
        state_data, _ = _decode_array(payload, 0)
        self._tracker.update_state(str(ext_id), _decode_uint_array(state_data))
