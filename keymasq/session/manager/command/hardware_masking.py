"""Hardware administration is independent of hardware mapping files."""

from typing import TYPE_CHECKING, cast

from keymasq.common.ipc import Command, CommandType
from keymasq.common.masking import HARDWARE_COMMAND_TIMEOUT
from keymasq.common.types import JsonObject

if TYPE_CHECKING:
    from keymasq.session.manager.core import SessionManager

COMMANDS = {
    "hardware_inventory": CommandType.HARDWARE_INVENTORY,
    "mask_hardware": CommandType.MASK_HARDWARE,
    "keep_hardware_mask": CommandType.KEEP_HARDWARE_MASK,
    "restore_hardware": CommandType.RESTORE_HARDWARE,
    "resume_hardware": CommandType.RESUME_HARDWARE,
    "set_hardware_mask_persistence": CommandType.SET_HARDWARE_MASK_PERSISTENCE,
}


async def handle_hardware_masking_commands(
    manager: "SessionManager",
    command: str,
    request: JsonObject,
) -> JsonObject | None:
    operation = COMMANDS.get(command)
    if operation is None:
        return None
    if not manager.connected:
        return {"status": "error", "message": "The input daemon is disconnected"}
    data = {key: request[key] for key in ("id", "generation", "token", "persist") if key in request}
    result = await manager.client.send_command(
        Command(operation, data=data), timeout=HARDWARE_COMMAND_TIMEOUT
    )
    if result.status != "ok":
        return {"status": "error", "message": result.error or "Hardware masking failed"}
    if command == "resume_hardware":
        from ..profile import coordinator

        await coordinator.reevaluate_profiles(manager, reason="hardware recovery resumed")
    return {"status": "ok", **cast(JsonObject, result.data or {})}
