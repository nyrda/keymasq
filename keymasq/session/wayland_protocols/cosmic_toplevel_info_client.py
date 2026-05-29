import asyncio
import os
import socket
import struct
from dataclasses import dataclass

from keymasq.session.wayland_protocols.ext_foreign_toplevel_list import (
    ExtForeignToplevelListTracker,
)

WL_DISPLAY_OBJECT_ID = 1
EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE = "ext_foreign_toplevel_list_v1"
COSMIC_TOPLEVEL_INFO_INTERFACE = "zcosmic_toplevel_info_v1"


def _pack_uint(value: int) -> bytes:
    return struct.pack("<I", int(value))


def _encode_string(value: str) -> bytes:
    encoded = str(value or "").encode("utf-8") + b"\x00"
    size = len(encoded)
    padded = (size + 3) & ~3
    return _pack_uint(size) + encoded + (b"\x00" * (padded - size))


def _decode_string(payload: bytes, offset: int) -> tuple[str, int]:
    (size,) = struct.unpack_from("<I", payload, offset)
    cursor = offset + 4
    if size == 0:
        return "", cursor

    raw = payload[cursor : cursor + size]
    padded = (size + 3) & ~3
    next_offset = cursor + padded
    if raw.endswith(b"\x00"):
        raw = raw[:-1]
    return raw.decode("utf-8", errors="replace"), next_offset


def _decode_array(payload: bytes, offset: int) -> tuple[bytes, int]:
    (size,) = struct.unpack_from("<I", payload, offset)
    cursor = offset + 4
    raw = payload[cursor : cursor + size]
    padded = (size + 3) & ~3
    next_offset = cursor + padded
    return raw, next_offset


@dataclass(slots=True)
class _WaylandObject:
    interface: str


class CosmicToplevelInfoWaylandClient:
    def __init__(
        self,
        tracker: ExtForeignToplevelListTracker,
        socket_path: str | None = None,
    ) -> None:
        self._tracker = tracker
        self._loop: asyncio.AbstractEventLoop | None = None
        self._socket: socket.socket | None = None
        self._socket_path: str | None = socket_path
        self._running = False
        self._buffer = bytearray()
        self._next_object_id = 2
        self._objects: dict[int, _WaylandObject] = {
            WL_DISPLAY_OBJECT_ID: _WaylandObject("wl_display")
        }
        self._registry_id: int | None = None
        self._list_id: int | None = None
        self._cosmic_info_id: int | None = None
        self._sync_waiters: set[int] = set()
        self._toplevel_handles: set[int] = set()
        self._cosmic_handles: set[int] = set()
        self._ext_to_cosmic: dict[int, int] = {}
        self._cosmic_to_ext: dict[int, int] = {}

    async def start(self) -> None:
        if self._socket_path is None:
            runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "")
            display_name = os.environ.get("WAYLAND_DISPLAY", "")
            if not runtime_dir or not display_name:
                raise RuntimeError("WAYLAND_DISPLAY and XDG_RUNTIME_DIR are required")
            self._socket_path = os.path.join(runtime_dir, display_name)

        self._loop = asyncio.get_running_loop()
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.setblocking(False)
        await self._loop.sock_connect(self._socket, self._socket_path)

        self._registry_id = self._allocate_object_id("wl_registry")
        await self._send_request(WL_DISPLAY_OBJECT_ID, 1, _pack_uint(self._registry_id))

        sync_id = await self._request_sync()
        await self._pump_until_sync(sync_id)

        if self._list_id is None:
            raise RuntimeError("ext_foreign_toplevel_list_v1 is unavailable on this compositor")
        if self._cosmic_info_id is None:
            raise RuntimeError("zcosmic_toplevel_info_v1 is unavailable on this compositor")

        post_bind_sync = await self._request_sync()
        await self._pump_until_sync(post_bind_sync)
        self._tracker.mark_done()

    async def run(self) -> None:
        if self._socket is None or self._loop is None:
            raise RuntimeError("wayland client not started")

        self._running = True
        while self._running:
            data = await self._loop.sock_recv(self._socket, 4096)
            if not data:
                break
            self._buffer.extend(data)
            await self._drain_messages()

    async def stop(self) -> None:
        self._running = False
        if self._socket is None:
            return

        if self._cosmic_info_id is not None:
            for cosmic_id in list(self._cosmic_handles):
                try:
                    await self._send_request(cosmic_id, 0, b"")
                except Exception:
                    pass
                self._objects.pop(cosmic_id, None)
            self._cosmic_handles.clear()
            self._ext_to_cosmic.clear()
            self._cosmic_to_ext.clear()
            self._objects.pop(self._cosmic_info_id, None)
            self._cosmic_info_id = None

        if self._list_id is not None:
            for handle_id in list(self._toplevel_handles):
                try:
                    await self._send_request(handle_id, 0, b"")
                except Exception:
                    pass
                self._objects.pop(handle_id, None)
                self._toplevel_handles.discard(handle_id)
            try:
                await self._send_request(self._list_id, 0, b"")
            except Exception:
                pass
            try:
                await self._send_request(self._list_id, 1, b"")
            except Exception:
                pass
            self._objects.pop(self._list_id, None)
            self._list_id = None

        self._socket.close()
        self._socket = None

    def _allocate_object_id(self, interface: str) -> int:
        object_id = self._next_object_id
        self._next_object_id += 1
        self._objects[object_id] = _WaylandObject(interface)
        return object_id

    async def _request_sync(self) -> int:
        callback_id = self._allocate_object_id("wl_callback")
        self._sync_waiters.add(callback_id)
        await self._send_request(WL_DISPLAY_OBJECT_ID, 0, _pack_uint(callback_id))
        return callback_id

    async def _pump_until_sync(self, callback_id: int, timeout: float = 2.0) -> None:
        if self._socket is None or self._loop is None:
            raise RuntimeError("wayland client not started")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while callback_id in self._sync_waiters:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for Wayland sync")
            data = await asyncio.wait_for(
                self._loop.sock_recv(self._socket, 4096), timeout=remaining
            )
            if not data:
                raise RuntimeError("Wayland socket closed during sync")
            self._buffer.extend(data)
            await self._drain_messages()

    async def _send_request(self, object_id: int, opcode: int, payload: bytes) -> None:
        if self._socket is None or self._loop is None:
            raise RuntimeError("wayland socket is not open")

        size = 8 + len(payload)
        header = struct.pack("<II", int(object_id), ((size & 0xFFFF) << 16) | (opcode & 0xFFFF))
        await self._loop.sock_sendall(self._socket, header + payload)

    async def _drain_messages(self) -> None:
        while len(self._buffer) >= 8:
            object_id, size_opcode = struct.unpack_from("<II", self._buffer, 0)
            size = size_opcode >> 16
            opcode = size_opcode & 0xFFFF
            if size < 8:
                raise RuntimeError("invalid Wayland message size")
            if len(self._buffer) < size:
                return

            payload = bytes(self._buffer[8:size])
            del self._buffer[:size]
            await self._dispatch_event(object_id, opcode, payload)

    async def _dispatch_event(self, object_id: int, opcode: int, payload: bytes) -> None:
        wayland_object = self._objects.get(object_id)
        if wayland_object is None:
            return

        interface = wayland_object.interface
        if interface == "wl_display":
            self._handle_display_event(opcode, payload)
            return
        if interface == "wl_registry":
            await self._handle_registry_event(object_id, opcode, payload)
            return
        if interface == "wl_callback":
            self._handle_callback_event(object_id, opcode)
            return
        if interface == EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE:
            await self._handle_list_event(object_id, opcode, payload)
            return
        if interface == COSMIC_TOPLEVEL_INFO_INTERFACE:
            self._handle_cosmic_info_event(opcode)
            return
        if interface == "ext_foreign_toplevel_handle_v1":
            self._handle_toplevel_event(object_id, opcode, payload)
            return
        if interface == "zcosmic_toplevel_handle_v1":
            self._handle_cosmic_toplevel_event(object_id, opcode, payload)

    def _handle_display_event(self, opcode: int, payload: bytes) -> None:
        if opcode != 1:
            return
        (deleted_object_id,) = struct.unpack_from("<I", payload, 0)
        self._objects.pop(deleted_object_id, None)
        self._toplevel_handles.discard(deleted_object_id)
        cosmic_id = self._ext_to_cosmic.pop(deleted_object_id, None)
        if cosmic_id is not None:
            self._cosmic_to_ext.pop(cosmic_id, None)
            self._cosmic_handles.discard(cosmic_id)

    async def _handle_registry_event(self, object_id: int, opcode: int, payload: bytes) -> None:
        if object_id != self._registry_id or opcode != 0:
            return

        global_name = struct.unpack_from("<I", payload, 0)[0]
        interface_name, offset = _decode_string(payload, 4)
        version = struct.unpack_from("<I", payload, offset)[0]

        if interface_name == EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE and self._list_id is None:
            bind_version = max(1, min(int(version), 1))
            list_id = self._allocate_object_id(EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE)
            bind_payload = (
                _pack_uint(global_name)
                + _encode_string(EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE)
                + _pack_uint(bind_version)
                + _pack_uint(list_id)
            )
            await self._send_request(object_id, 0, bind_payload)
            self._list_id = list_id
            return

        if interface_name == COSMIC_TOPLEVEL_INFO_INTERFACE and self._cosmic_info_id is None:
            advertised_version = int(version)
            if advertised_version < 2:
                return

            bind_version = min(advertised_version, 3)
            cosmic_id = self._allocate_object_id(COSMIC_TOPLEVEL_INFO_INTERFACE)
            bind_payload = (
                _pack_uint(global_name)
                + _encode_string(COSMIC_TOPLEVEL_INFO_INTERFACE)
                + _pack_uint(bind_version)
                + _pack_uint(cosmic_id)
            )
            await self._send_request(object_id, 0, bind_payload)
            self._cosmic_info_id = cosmic_id

    def _handle_callback_event(self, object_id: int, opcode: int) -> None:
        if opcode != 0:
            return
        self._sync_waiters.discard(object_id)

    async def _handle_list_event(self, object_id: int, opcode: int, payload: bytes) -> None:
        if object_id != self._list_id:
            return

        if opcode == 0:
            (handle_object_id,) = struct.unpack_from("<I", payload, 0)
            self._objects[handle_object_id] = _WaylandObject("ext_foreign_toplevel_handle_v1")
            self._toplevel_handles.add(handle_object_id)
            self._tracker.add_toplevel(str(handle_object_id))

            if self._cosmic_info_id is not None:
                cosmic_handle_id = self._allocate_object_id("zcosmic_toplevel_handle_v1")
                request_payload = _pack_uint(cosmic_handle_id) + _pack_uint(handle_object_id)
                await self._send_request(self._cosmic_info_id, 1, request_payload)
                self._cosmic_handles.add(cosmic_handle_id)
                self._ext_to_cosmic[handle_object_id] = cosmic_handle_id
                self._cosmic_to_ext[cosmic_handle_id] = handle_object_id
            return

        if opcode == 1:
            self._running = False

    def _handle_cosmic_info_event(self, opcode: int) -> None:
        if opcode == 2:
            self._tracker.mark_done()

    def _handle_toplevel_event(self, object_id: int, opcode: int, payload: bytes) -> None:
        handle_id = str(object_id)

        if opcode == 0:
            self._tracker.close_toplevel(handle_id)
            self._toplevel_handles.discard(object_id)
            cosmic_id = self._ext_to_cosmic.pop(object_id, None)
            if cosmic_id is not None:
                self._cosmic_to_ext.pop(cosmic_id, None)
                self._cosmic_handles.discard(cosmic_id)
                self._objects.pop(cosmic_id, None)
            return

        if opcode == 2:
            title, _ = _decode_string(payload, 0)
            self._tracker.update_title(handle_id, title)
            return

        if opcode == 3:
            app_id, _ = _decode_string(payload, 0)
            self._tracker.update_app_id(handle_id, app_id)

    def _handle_cosmic_toplevel_event(self, object_id: int, opcode: int, payload: bytes) -> None:
        ext_id = self._cosmic_to_ext.get(object_id)
        if ext_id is None:
            return
        if opcode != 8:
            return
        state, _ = _decode_array(payload, 0)
        self._tracker.update_state(str(ext_id), state)
