import asyncio
from typing import TYPE_CHECKING

from keymasq.common.coercion import coerce_str
from keymasq.common.security import PeerCredentials

from .. import device_inspector
from ..common import JsonObject

if TYPE_CHECKING:
    from ..core import SessionManager


async def handle_device_inspector_commands(
    manager: "SessionManager",
    command: str,
    request: JsonObject,
    peer: PeerCredentials,
    writer: asyncio.StreamWriter,
) -> JsonObject | None:
    if command not in {
        "get_device_inspector_snapshot",
        "start_device_inspector",
        "stop_device_inspector",
        "enable_device_inspector_suppression",
        "disable_device_inspector_suppression",
    }:
        return None

    hardware_id = coerce_str(request.get("hardware_id"), "").strip()
    if not hardware_id:
        return {"status": "error", "message": "missing hardware_id"}

    if command == "get_device_inspector_snapshot":
        return device_inspector.build_device_inspector_snapshot(manager, hardware_id)
    if command == "start_device_inspector":
        return await device_inspector.start_device_inspector(
            manager,
            hardware_id,
            peer,
            writer,
        )
    if command == "stop_device_inspector":
        return await device_inspector.stop_device_inspector(
            manager,
            hardware_id,
            writer,
        )
    if command == "enable_device_inspector_suppression":
        return await device_inspector.enable_device_inspector_suppression(
            manager,
            hardware_id,
            writer,
        )
    return await device_inspector.disable_device_inspector_suppression(
        manager,
        hardware_id,
        reason=coerce_str(request.get("reason", "manual"), "manual"),
    )
