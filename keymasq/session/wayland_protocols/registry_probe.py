import asyncio
import logging
import struct
from pathlib import Path

from keymasq.session.wayland_protocols.client_transport import (
    WaylandClientTransport,
    WaylandDisplayError,
)

log = logging.getLogger("keymasq-session.wayland.registry_probe")


class _RegistryProbeTransport(WaylandClientTransport):
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
        except struct.error as exc:
            log.debug("Ignoring malformed Wayland registry global: %s", exc)
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
    raise RuntimeError(
        "list_registry_globals_sync cannot be called from a running event loop; "
        "use await list_registry_globals(...)"
    )


async def list_registry_globals(socket_path: Path, timeout_s: float = 0.6) -> set[str]:
    transport = _RegistryProbeTransport(socket_path)
    try:
        return await asyncio.wait_for(transport.collect(timeout_s), timeout=timeout_s)
    except TimeoutError as exc:
        log.debug("Wayland registry probe timed out for %s: %s", socket_path, exc)
        return set()
    except (OSError, WaylandDisplayError) as exc:
        log.debug("Wayland registry probe failed for %s: %s", socket_path, exc)
        return set()
    except Exception:
        log.exception("Unexpected Wayland registry probe failure for %s", socket_path)
        return set()
