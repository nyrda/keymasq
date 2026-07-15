from typing import TYPE_CHECKING, cast

from keymasq.common.coercion import coerce_int
from keymasq.common.ipc import Command, CommandType
from keymasq.common.settings import GlobalSettings
from keymasq.common.virtual_devices import (
    MAX_VIRTUAL_GAMEPADS,
    MIN_VIRTUAL_GAMEPADS,
    clamp_virtual_gamepad_count,
)
from keymasq.session.settings import save_global_settings, save_virtual_gamepad_count

from ..common import JsonObject
from .common import daemon_unavailable_response, send_daemon_request

if TYPE_CHECKING:
    from ..core import SessionManager


async def handle_virtual_gamepad_commands(
    manager: "SessionManager",
    command: str,
    request: JsonObject,
) -> JsonObject | None:
    if command == "get_virtual_gamepads":
        return {
            "status": "ok",
            "count": int(manager.virtual_gamepad_count),
            "min_count": MIN_VIRTUAL_GAMEPADS,
            "max_count": MAX_VIRTUAL_GAMEPADS,
        }
    if command != "set_virtual_gamepads":
        return None

    count = clamp_virtual_gamepad_count(
        coerce_int(request.get("count"), manager.virtual_gamepad_count)
    )
    if manager.connected:
        response = await send_daemon_request(
            manager, Command(command=CommandType.SET_VIRTUAL_GAMEPADS, data={"count": count})
        )
        if response is None:
            return daemon_unavailable_response()
        if response.status != "ok":
            return {"status": "error", "message": response.error or "daemon rejected count"}
        if isinstance(response.data, dict):
            data = cast(JsonObject, response.data)
            count = coerce_int(data.get("count"), count)
    count = save_virtual_gamepad_count(count)
    manager.virtual_gamepad_count = count
    manager.broadcast_to_session_clients(
        {"event": "virtual_gamepads_changed", "count": int(manager.virtual_gamepad_count)}
    )
    return {
        "status": "ok",
        "count": int(manager.virtual_gamepad_count),
        "min_count": MIN_VIRTUAL_GAMEPADS,
        "max_count": MAX_VIRTUAL_GAMEPADS,
    }


async def handle_settings_commands(
    manager: "SessionManager",
    command: str,
    request: JsonObject,
) -> JsonObject | None:
    if command == "get_settings":
        return _settings_payload(manager)
    if command != "set_settings":
        return None

    count = clamp_virtual_gamepad_count(
        coerce_int(
            request.get("virtual_gamepad_count"),
            manager.virtual_gamepad_count,
        )
    )

    if manager.connected:
        response = await send_daemon_request(
            manager,
            Command(
                command=CommandType.SET_VIRTUAL_GAMEPADS,
                data={"count": count},
            ),
        )
        if response is None:
            payload = _settings_payload(manager)
            payload["status"] = "error"
            payload["message"] = "Daemon unavailable"
            return payload
        if response.status != "ok":
            payload = _settings_payload(manager)
            payload["status"] = "error"
            payload["message"] = response.error or "daemon rejected virtual gamepad count"
            return payload
        if isinstance(response.data, dict):
            data = cast(JsonObject, response.data)
            count = coerce_int(data.get("count"), count)

    saved = save_global_settings(
        GlobalSettings(
            virtual_gamepad_count=count,
        )
    )
    manager.virtual_gamepad_count = saved.virtual_gamepad_count
    payload = _settings_payload(manager)
    manager.broadcast_to_session_clients(
        {
            "event": "settings_changed",
            "virtual_gamepad_count": int(manager.virtual_gamepad_count),
        }
    )
    return payload


def _settings_payload(manager: "SessionManager") -> JsonObject:
    return {
        "status": "ok",
        "virtual_gamepad_count": int(manager.virtual_gamepad_count),
        "min_virtual_gamepad_count": MIN_VIRTUAL_GAMEPADS,
        "max_virtual_gamepad_count": MAX_VIRTUAL_GAMEPADS,
    }
