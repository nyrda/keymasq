import asyncio
import socket
import struct
from pathlib import Path


def _decode_registry_interface(payload: bytes) -> str | None:
    name_len = struct.unpack_from("<I", payload, 4)[0]
    cursor = 8
    if cursor + name_len > len(payload):
        return None

    raw = payload[cursor : cursor + name_len]
    if raw.endswith(b"\x00"):
        raw = raw[:-1]
    interface_name = raw.decode("utf-8", errors="replace")
    return interface_name or None


def list_registry_globals_sync(socket_path: Path, timeout_s: float = 0.6) -> set[str]:
    registry_id = 2
    callback_id = 3
    globals_found: set[str] = set()

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        sock.connect(str(socket_path))

        def _send_request(object_id: int, opcode: int, payload: bytes) -> None:
            size = 8 + len(payload)
            header = struct.pack("<II", int(object_id), ((size & 0xFFFF) << 16) | (opcode & 0xFFFF))
            sock.sendall(header + payload)

        _send_request(1, 1, struct.pack("<I", registry_id))
        _send_request(1, 0, struct.pack("<I", callback_id))

        buffer = bytearray()
        done = False
        while not done:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buffer.extend(chunk)

            while len(buffer) >= 8:
                object_id, size_opcode = struct.unpack_from("<II", buffer, 0)
                size = size_opcode >> 16
                opcode = size_opcode & 0xFFFF
                if size < 8 or len(buffer) < size:
                    break

                payload = bytes(buffer[8:size])
                del buffer[:size]

                if object_id == registry_id and opcode == 0:
                    try:
                        interface_name = _decode_registry_interface(payload)
                        if interface_name:
                            globals_found.add(interface_name)
                    except Exception:
                        pass
                elif object_id == callback_id and opcode == 0:
                    done = True
                    break
    except Exception:
        return set()
    finally:
        try:
            sock.close()
        except Exception:
            pass

    return globals_found


async def list_registry_globals(socket_path: Path, timeout_s: float = 0.6) -> set[str]:
    registry_id = 2
    callback_id = 3
    globals_found: set[str] = set()

    try:
        connect_coro = asyncio.open_unix_connection(path=str(socket_path))
        reader, writer = await asyncio.wait_for(connect_coro, timeout=timeout_s)
    except Exception:
        return set()

    try:

        def _build_request(object_id: int, opcode: int, payload: bytes) -> bytes:
            size = 8 + len(payload)
            header = struct.pack(
                "<II",
                int(object_id),
                ((size & 0xFFFF) << 16) | (opcode & 0xFFFF),
            )
            return header + payload

        writer.write(_build_request(1, 1, struct.pack("<I", registry_id)))
        writer.write(_build_request(1, 0, struct.pack("<I", callback_id)))
        await writer.drain()

        buffer = bytearray()
        done = False
        while not done:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout_s)
            except Exception:
                break
            if not chunk:
                break
            buffer.extend(chunk)

            while len(buffer) >= 8:
                object_id, size_opcode = struct.unpack_from("<II", buffer, 0)
                size = size_opcode >> 16
                opcode = size_opcode & 0xFFFF
                if size < 8 or len(buffer) < size:
                    break

                payload = bytes(buffer[8:size])
                del buffer[:size]

                if object_id == registry_id and opcode == 0:
                    try:
                        interface_name = _decode_registry_interface(payload)
                        if interface_name:
                            globals_found.add(interface_name)
                    except Exception:
                        pass
                elif object_id == callback_id and opcode == 0:
                    done = True
                    break
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    return globals_found
