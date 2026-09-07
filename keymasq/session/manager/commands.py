import asyncio
from typing import TYPE_CHECKING

from keymasq.common.coercion import coerce_str
from keymasq.common.security import PeerCredentials

from . import recording_unlock
from .command.capture import handle_capture_commands
from .command.compositor import handle_compositor_commands
from .command.diagnostics import handle_set_diagnostics
from .command.inspector import handle_device_inspector_commands
from .command.macro import handle_macro_commands
from .command.profile import handle_profile_commands
from .command.recording import handle_recording_commands
from .command.settings import handle_settings_commands, handle_virtual_gamepad_commands
from .common import JsonObject

if TYPE_CHECKING:
    from .core import SessionManager


async def handle_session_request(
    manager: "SessionManager",
    request: JsonObject,
    peer: PeerCredentials,
    writer: asyncio.StreamWriter,
) -> JsonObject:
    command = coerce_str(request.get("command"), "")
    policy = manager.security_policy

    if recording_unlock.is_sensitive_session_command(
        manager, command, policy
    ) and not await recording_unlock.authorize_sensitive_session_command(
        manager,
        command,
        peer,
        writer,
    ):
        return {
            "status": "error",
            "error_code": "sensitive_command_denied",
            "message": "Sensitive command denied: caller is not active GUI owner",
        }

    result = await handle_profile_commands(manager, command, request)
    if result is not None:
        return result

    result = await handle_compositor_commands(
        manager,
        command,
        request,
        peer,
        writer,
    )
    if result is not None:
        return result

    result = await handle_recording_commands(manager, command, request, peer, writer)
    if result is not None:
        return result

    result = await handle_macro_commands(manager, command, request, writer)
    if result is not None:
        return result

    result = await handle_capture_commands(manager, command, request, writer)
    if result is not None:
        return result

    result = await handle_virtual_gamepad_commands(manager, command, request)
    if result is not None:
        return result

    result = await handle_settings_commands(manager, command, request)
    if result is not None:
        return result

    if command == "set_diagnostics":
        return await handle_set_diagnostics(manager, request)

    result = await handle_device_inspector_commands(manager, command, request, peer, writer)
    if result is not None:
        return result

    return {"error": f"Unknown command: {command}"}
