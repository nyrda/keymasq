import asyncio
import logging
from typing import TYPE_CHECKING

from keymasq.common.coercion import coerce_str
from keymasq.common.ipc import Command, CommandType

from .. import combo_inspector
from ..common import JsonObject, json_object
from ..profile import coordinator, runtime_state, runtime_status
from .common import (
    config_reload_failed_response,
    daemon_unavailable_response,
    send_daemon_request,
    suppress_or_join_config_watcher_reload,
)

if TYPE_CHECKING:
    from ..core import SessionManager

log = logging.getLogger("keymasq-session")


async def handle_profile_commands(
    manager: "SessionManager",
    command: str,
    request: JsonObject,
) -> JsonObject | None:
    if command == "get_active_profiles":
        await runtime_status.refresh_device_runtime_status(manager)
        return runtime_status.build_active_profiles_payload(manager)

    if command == "get_combo_inspector_snapshot":
        return combo_inspector.build_combo_inspector_snapshot(manager)

    if command == "list_profiles":
        return runtime_status.build_profile_overview(manager)

    if command in {"enable_profile", "disable_profile", "toggle_profile"}:
        profile_name = coerce_str(request.get("profile_name"), "")
        if not profile_name:
            return {"status": "error", "message": "missing profile_name"}

        enabled: bool | None = None
        if command == "enable_profile":
            enabled = True
        elif command == "disable_profile":
            enabled = False

        return await coordinator.set_profile_enabled(manager, profile_name, enabled)

    if command == "reload":
        if response := await suppress_or_join_config_watcher_reload(manager):
            return response
        if await manager.reload_profiles():
            manager.suppress_config_watcher_reload()
            return {"status": "ok"}
        return config_reload_failed_response()

    if command == "release_device":
        hardware_id = coerce_str(request.get("hardware_id"), "").strip()
        if not hardware_id:
            return {"status": "error", "message": "missing hardware_id"}
        immediate = bool(request.get("immediate", True))
        result = await send_daemon_request(
            manager,
            Command(
                command=CommandType.RELEASE_DEVICE,
                data={"hardware_id": hardware_id, "immediate": immediate},
            ),
        )
        if result is None:
            return daemon_unavailable_response()
        if result.status != "ok":
            return {
                "status": "error",
                "message": result.error or f"Failed to release {hardware_id}",
            }
        runtime_state.clear_hardware_runtime_state(manager, hardware_id)
        response_data = json_object(result.data)
        response = response_data if response_data else {}
        response["status"] = "ok"
        return response

    if command in {"reevaluate_profiles", "reevaluate_hardware"}:
        log.info("Global profile reevaluate requested")
        if response := await suppress_or_join_config_watcher_reload(manager):
            return response
        try:
            await asyncio.to_thread(manager.reload_config_from_disk)
        except Exception as exc:
            log.exception("Failed to reload user config from disk for reevaluate request")
            manager.send_notification(
                "Keymasq Config Error",
                "Failed to reload config; keeping the previous active config. See logs.",
            )
            return {"status": "error", "message": str(exc)}
        manager.suppress_config_watcher_reload()
        runtime_state.invalidate_runtime_payload_signatures(manager)
        await coordinator.reevaluate_profiles(manager, reason="session command reevaluate")
        return {"status": "ok"}

    if command == "ping":
        return {"status": "ok"}

    return None
