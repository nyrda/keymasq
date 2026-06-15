import array
import asyncio
import os
import socket
import struct
from dataclasses import dataclass

WL_DISPLAY_OBJECT_ID = 1


def pack_uint(value: int) -> bytes:
    return struct.pack("<I", int(value))


def encode_string(value: str) -> bytes:
    encoded = str(value or "").encode("utf-8") + b"\x00"
    size = len(encoded)
    padded = (size + 3) & ~3
    return pack_uint(size) + encoded + (b"\x00" * (padded - size))


def decode_string(payload: bytes, offset: int) -> tuple[str, int]:
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


def decode_array(payload: bytes, offset: int) -> tuple[bytes, int]:
    (size,) = struct.unpack_from("<I", payload, offset)
    cursor = offset + 4
    raw = payload[cursor : cursor + size]
    padded = (size + 3) & ~3
    next_offset = cursor + padded
    return raw, next_offset


def decode_uint_array(array_data: bytes) -> tuple[int, ...]:
    unit_size = 4
    if len(array_data) % unit_size != 0:
        return ()
    return tuple(
        int.from_bytes(array_data[idx : idx + unit_size], byteorder="little")
        for idx in range(0, len(array_data), unit_size)
    )


@dataclass(slots=True)
class WaylandObject:
    interface: str


@dataclass(frozen=True, slots=True)
class WaylandMessage:
    object_id: int
    opcode: int
    payload: bytes


class WaylandDisplayError(RuntimeError):
    def __init__(self, object_id: int, error_code: int, message: str) -> None:
        self.object_id = int(object_id)
        self.error_code = int(error_code)
        self.message = str(message or "")
        super().__init__(
            f"Wayland display error on object {self.object_id}: "
            f"code={self.error_code} message={self.message}"
        )


def build_request(object_id: int, opcode: int, payload: bytes) -> bytes:
    size = 8 + len(payload)
    header = struct.pack(
        "<II",
        int(object_id),
        ((size & 0xFFFF) << 16) | (opcode & 0xFFFF),
    )
    return header + payload


def pop_message(buffer: bytearray) -> WaylandMessage | None:
    if len(buffer) < 8:
        return None

    object_id, size_opcode = struct.unpack_from("<II", buffer, 0)
    size = size_opcode >> 16
    opcode = size_opcode & 0xFFFF
    if size < 8:
        raise RuntimeError("invalid Wayland message size")
    if len(buffer) < size:
        return None

    payload = bytes(buffer[8:size])
    del buffer[:size]
    return WaylandMessage(object_id, opcode, payload)


def callback_done(object_id: int, opcode: int, callback_id: int) -> bool:
    return object_id == callback_id and opcode == 0


def decode_registry_global(payload: bytes) -> tuple[int, str, int]:
    global_name = struct.unpack_from("<I", payload, 0)[0]
    interface_name, offset = decode_string(payload, 4)
    version = struct.unpack_from("<I", payload, offset)[0]
    return global_name, interface_name, version


class WaylandClientTransport:
    def __init__(self, socket_path: str | None = None) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._socket: socket.socket | None = None
        self._socket_path: str | None = socket_path
        self._running = False
        self._buffer = bytearray()
        self._next_object_id = 2
        self._objects: dict[int, WaylandObject] = {
            WL_DISPLAY_OBJECT_ID: WaylandObject("wl_display")
        }
        self._registry_id: int | None = None
        self._sync_waiters: set[int] = set()
        self._sync_futures: dict[int, asyncio.Future[None]] = {}

    async def start(self) -> None:
        try:
            await self._connect_and_request_registry()
            await self._roundtrip()
            self._check_required_globals()
            await self._roundtrip()
            self._after_start_sync()
        except Exception:
            self._close_socket()
            raise

    async def run(self) -> None:
        sock = self._socket
        loop = self._loop
        if sock is None or loop is None:
            raise RuntimeError("wayland client not started")

        self._running = True
        try:
            while self._running:
                data = await loop.sock_recv(sock, 4096)
                if not data:
                    break
                self._buffer.extend(data)
                await self._drain_messages()
        finally:
            self._running = False
            self._close_socket()

    def _check_required_globals(self) -> None:
        raise NotImplementedError

    def _after_start_sync(self) -> None:
        pass

    async def _connect_and_request_registry(self) -> None:
        socket_path = self._resolve_socket_path()
        self._loop = asyncio.get_running_loop()
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.setblocking(False)
        await self._loop.sock_connect(self._socket, socket_path)

        self._registry_id = self._allocate_object_id("wl_registry")
        await self._send_request(WL_DISPLAY_OBJECT_ID, 1, pack_uint(self._registry_id))

    def _resolve_socket_path(self) -> str:
        if self._socket_path is not None:
            return self._socket_path

        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "")
        display_name = os.environ.get("WAYLAND_DISPLAY", "")
        if not runtime_dir or not display_name:
            raise RuntimeError("WAYLAND_DISPLAY and XDG_RUNTIME_DIR are required")

        self._socket_path = os.path.join(runtime_dir, display_name)
        return self._socket_path

    def _close_socket(self) -> None:
        sock = self._socket
        if sock is None:
            return
        sock.close()
        self._socket = None

    def _allocate_object_id(self, interface: str) -> int:
        object_id = self._next_object_id
        self._next_object_id += 1
        self._add_object(object_id, interface)
        return object_id

    def _add_object(self, object_id: int, interface: str) -> None:
        self._objects[int(object_id)] = WaylandObject(interface)

    async def _roundtrip(self, timeout: float = 2.0) -> None:
        sync_id = await self._request_sync()
        if self._running:
            await self._wait_until_sync(sync_id, timeout=timeout)
        else:
            await self._pump_until_sync(sync_id, timeout=timeout)

    async def _request_sync(self) -> int:
        callback_id = self._allocate_object_id("wl_callback")
        self._sync_waiters.add(callback_id)
        self._sync_futures[callback_id] = asyncio.get_running_loop().create_future()
        await self._send_request(WL_DISPLAY_OBJECT_ID, 0, pack_uint(callback_id))
        return callback_id

    async def _wait_until_sync(self, callback_id: int, timeout: float = 2.0) -> None:
        future = self._sync_futures.get(callback_id)
        if future is None or future.done():
            return
        try:
            await asyncio.wait_for(future, timeout=timeout)
        except (TimeoutError, asyncio.CancelledError):
            self._sync_waiters.discard(callback_id)
            self._sync_futures.pop(callback_id, None)
            self._objects.pop(callback_id, None)
            raise

    async def _pump_until_sync(self, callback_id: int, timeout: float = 2.0) -> None:
        sock = self._socket
        loop = self._loop
        if sock is None or loop is None:
            raise RuntimeError("wayland client not started")

        current_loop = asyncio.get_running_loop()
        deadline = current_loop.time() + timeout
        while callback_id in self._sync_waiters:
            remaining = deadline - current_loop.time()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for Wayland sync")
            data = await asyncio.wait_for(loop.sock_recv(sock, 4096), timeout=remaining)
            if not data:
                raise RuntimeError("Wayland socket closed during sync")
            self._buffer.extend(data)
            await self._drain_messages()

    async def _send_request(self, object_id: int, opcode: int, payload: bytes) -> None:
        sock = self._socket
        loop = self._loop
        if sock is None or loop is None:
            raise RuntimeError("wayland socket is not open")

        await loop.sock_sendall(sock, build_request(object_id, opcode, payload))

    async def _send_request_with_fds(
        self,
        object_id: int,
        opcode: int,
        payload: bytes,
        fds: list[int],
    ) -> None:
        sock = self._socket
        loop = self._loop
        if sock is None or loop is None:
            raise RuntimeError("wayland socket is not open")

        data = build_request(object_id, opcode, payload)
        fd_array = array.array("i", [int(fd) for fd in fds])
        ancdata = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fd_array.tobytes())] if fds else []
        offset = 0
        send_ancdata = ancdata
        while offset < len(data):
            try:
                sent = sock.sendmsg([data[offset:]], send_ancdata)
            except (BlockingIOError, InterruptedError):
                await self._wait_socket_writable(sock)
                continue
            if sent <= 0:
                raise RuntimeError("wayland socket sendmsg returned no progress")
            offset += sent
            send_ancdata = []

    async def _wait_socket_writable(self, sock: socket.socket) -> None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()

        def _ready() -> None:
            if not future.done():
                future.set_result(None)

        loop.add_writer(sock.fileno(), _ready)
        try:
            await future
        finally:
            loop.remove_writer(sock.fileno())

    async def _bind_registry_global(
        self,
        registry_id: int,
        global_name: int,
        interface_name: str,
        version: int,
        object_id: int,
    ) -> None:
        bind_payload = (
            pack_uint(global_name)
            + encode_string(interface_name)
            + pack_uint(version)
            + pack_uint(object_id)
        )
        await self._send_request(registry_id, 0, bind_payload)

    async def _drain_messages(self) -> None:
        while (message := pop_message(self._buffer)) is not None:
            await self._dispatch_event(message.object_id, message.opcode, message.payload)

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
        await self._dispatch_protocol_event(interface, object_id, opcode, payload)

    async def _dispatch_protocol_event(
        self,
        interface: str,
        object_id: int,
        opcode: int,
        payload: bytes,
    ) -> None:
        raise NotImplementedError

    def _handle_display_event(self, opcode: int, payload: bytes) -> None:
        if opcode == 0:
            error_object_id, error_code, message = self._decode_display_error(payload)
            self._on_display_error(error_object_id, error_code, message)
            return
        if opcode != 1:
            return
        (deleted_object_id,) = struct.unpack_from("<I", payload, 0)
        self._objects.pop(deleted_object_id, None)
        self._on_object_deleted(deleted_object_id)

    def _on_object_deleted(self, object_id: int) -> None:
        pass

    def _decode_display_error(self, payload: bytes) -> tuple[int, int, str]:
        if len(payload) < 8:
            raise RuntimeError("invalid wl_display.error payload")
        error_object_id, error_code = struct.unpack_from("<II", payload, 0)
        message = ""
        if len(payload) > 8:
            message, _offset = decode_string(payload, 8)
        return error_object_id, error_code, message

    def _on_display_error(self, object_id: int, error_code: int, message: str) -> None:
        raise WaylandDisplayError(object_id, error_code, message)

    async def _handle_registry_event(self, object_id: int, opcode: int, payload: bytes) -> None:
        if object_id != self._registry_id:
            return
        if opcode == 1:
            (global_name,) = struct.unpack_from("<I", payload, 0)
            await self._handle_registry_global_remove(global_name)
            return
        if opcode != 0:
            return

        global_name, interface_name, version = decode_registry_global(payload)
        await self._handle_registry_global(object_id, global_name, interface_name, version)

    async def _handle_registry_global(
        self,
        registry_id: int,
        global_name: int,
        interface_name: str,
        version: int,
    ) -> None:
        raise NotImplementedError

    async def _handle_registry_global_remove(self, global_name: int) -> None:
        del global_name

    def _handle_callback_event(self, object_id: int, opcode: int) -> None:
        if not callback_done(object_id, opcode, object_id):
            return
        self._sync_waiters.discard(object_id)
        future = self._sync_futures.pop(object_id, None)
        if future is not None and not future.done():
            future.set_result(None)
