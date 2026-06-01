import asyncio
from pathlib import Path

from keymasq.session.wayland_protocols import client_transport as _transport


class _RegistryProbeTransport(_transport.WaylandClientTransport):
    def __init__(self, socket_path: Path) -> None:
        super().__init__(str(socket_path))
        self._globals_found: set[str] = set()

    async def collect(self, timeout_s: float) -> set[str]:
        try:
            await self._connect_and_request_registry()
            sync_id = await self._request_sync()
            await self._pump_until_sync(sync_id, timeout=timeout_s)
        finally:
            self._close_socket()
        return set(self._globals_found)

    def _check_required_globals(self) -> None:
        pass

    async def _handle_registry_event(self, object_id: int, opcode: int, payload: bytes) -> None:
        try:
            await super()._handle_registry_event(object_id, opcode, payload)
        except Exception:
            return

    async def _handle_registry_global(
        self,
        registry_id: int,
        global_name: int,
        interface_name: str,
        version: int,
    ) -> None:
        del registry_id, global_name, version
        if interface_name:
            self._globals_found.add(interface_name)

    async def _dispatch_protocol_event(
        self,
        interface: str,
        object_id: int,
        opcode: int,
        payload: bytes,
    ) -> None:
        del interface, object_id, opcode, payload


def list_registry_globals_sync(socket_path: Path, timeout_s: float = 0.6) -> set[str]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(list_registry_globals(socket_path, timeout_s=timeout_s))
    else:
        return set()


async def list_registry_globals(socket_path: Path, timeout_s: float = 0.6) -> set[str]:
    transport = _RegistryProbeTransport(socket_path)
    try:
        return await asyncio.wait_for(transport.collect(timeout_s), timeout=timeout_s)
    except Exception:
        return set()
