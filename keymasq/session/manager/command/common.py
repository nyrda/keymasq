import logging
from typing import TYPE_CHECKING

from keymasq.common.ipc import Command, Response

from ..common import JsonObject

if TYPE_CHECKING:
    from ..core import SessionManager

log = logging.getLogger("keymasq-session")


def daemon_unavailable_response() -> JsonObject:
    return {"status": "error", "message": "Daemon unavailable"}


def config_reload_failed_response() -> JsonObject:
    return {
        "status": "error",
        "message": "Failed to reload config; keeping previous active config",
    }


async def suppress_or_join_config_watcher_reload(
    manager: "SessionManager",
) -> JsonObject | None:
    manager.suppress_config_watcher_reload()
    running_reload_result = await manager.wait_for_running_config_reload()
    if running_reload_result is not None:
        manager.suppress_config_watcher_reload()
    return None


async def send_daemon_request(
    manager: "SessionManager",
    command: Command,
) -> Response | None:
    try:
        return await manager.client.send_command(command)
    except OSError as exc:
        log.debug("Daemon request %s failed: %s", command.command.value, exc, exc_info=True)
        return None
