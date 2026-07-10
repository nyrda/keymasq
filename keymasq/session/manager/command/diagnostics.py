from typing import TYPE_CHECKING

from keymasq.common.coercion import coerce_float, coerce_str
from keymasq.common.ipc import Command, CommandType

from ..common import JsonObject, json_list
from .common import daemon_unavailable_response, send_daemon_request

if TYPE_CHECKING:
    from ..core import SessionManager


async def handle_set_diagnostics(
    manager: "SessionManager",
    request: JsonObject,
) -> JsonObject:
    enabled = bool(request.get("enabled", False))
    interval = coerce_float(request.get("interval"), 5.0)
    categories = [
        coerce_str(category, "")
        for category in json_list(request.get("categories"))
        if coerce_str(category, "")
    ]
    result = await send_daemon_request(
        manager,
        Command(
            command=CommandType.SET_DIAGNOSTICS,
            data={"enabled": enabled, "interval": interval, "categories": categories},
        ),
    )
    if result is None:
        return daemon_unavailable_response()

    if result.status == "ok":
        return {"status": "ok", "data": result.data or {}}
    return {"status": "error", "message": result.error or "Failed to update diagnostics"}
