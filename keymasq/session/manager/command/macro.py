import asyncio
from typing import TYPE_CHECKING

from keymasq.common.coercion import coerce_float, coerce_int, coerce_str
from keymasq.common.ipc import Command, CommandType
from keymasq.common.macro_compile import (
    DEFAULT_TYPE_MACRO_DOWN_MS,
    DEFAULT_TYPE_MACRO_PAUSE_MS,
)

from .. import recording_lifecycle
from ..common import JsonObject, json_list, json_object
from ..profile import coordinator
from .common import daemon_unavailable_response, send_daemon_request

if TYPE_CHECKING:
    from ..core import SessionManager


async def handle_macro_commands(
    manager: "SessionManager",
    command: str,
    request: JsonObject,
    writer: asyncio.StreamWriter | None = None,
) -> JsonObject | None:
    if writer is not None:
        request = dict(request)
        if command in {"play_macro", "type_text"}:
            request.pop("playback_id", None)
        if command in {"get_macro_playback", "cancel_macro_request"}:
            playback_id = coerce_str(request.get("playback_id"), "")
            if command == "get_macro_playback":
                return manager.playback_requests.status(playback_id, writer)
            return await manager.playback_requests.cancel(playback_id, writer)
        if command == "type_text" or (
            command == "play_macro"
            and (request.get("track") is True or request.get("ordered") is True)
        ):
            return manager.playback_requests.submit(request, writer)
    if command == "list_macros":
        result = await send_daemon_request(manager, Command(command=CommandType.MACRO_LIST_META))
        if result is None:
            return daemon_unavailable_response()
        result_data = json_object(result.data)
        if result.status == "ok" and result_data is not None:
            macros = list(json_list(result_data.get("macros")))
            if bool(request.get("include_slots", False)):
                await recording_lifecycle.sync_pending_macro_slots_from_daemon(manager)
                macros.extend(recording_lifecycle.build_pending_macro_slot_meta(manager))
            return {"status": "ok", "macros": macros}
        return {"status": "error", "message": result.error or "Failed to list macros"}

    if command == "get_macro":
        name = coerce_str(request.get("name"), "")
        result = await send_daemon_request(
            manager,
            Command(command=CommandType.MACRO_GET, data={"name": name}),
        )
        if result is None:
            return daemon_unavailable_response()
        result_data = json_object(result.data)
        if result.status == "ok" and result_data is not None:
            return {"status": "ok", "macro": result_data.get("macro")}
        return {"status": "error", "message": result.error or "Macro not found"}

    if command == "create_macro":
        macro = json_object(request.get("macro"))
        if macro is None:
            return {"status": "error", "message": "macro payload required"}
        result = await send_daemon_request(
            manager,
            Command(command=CommandType.MACRO_CREATE, data={"macro": macro}),
        )
        if result is None:
            return daemon_unavailable_response()
        result_data = json_object(result.data)
        if result.status == "ok" and result_data is not None:
            created = json_object(result_data.get("macro")) or {}
            await coordinator.refresh_macro_bindings(manager)
            manager.broadcast_to_session_clients(
                {"event": "macro_saved", "name": coerce_str(created.get("name"), "")}
            )
            return {"status": "ok", "macro": created}
        return {"status": "error", "message": result.error or "Failed to create macro"}

    if command == "update_macro":
        name = coerce_str(request.get("name"), "")
        macro = json_object(request.get("macro"))
        if macro is None:
            return {"status": "error", "message": "macro payload required"}
        update_payload: JsonObject = {"name": name, "macro": macro}
        if "expected_revision" in request:
            update_payload["expected_revision"] = request.get("expected_revision")
        result = await send_daemon_request(
            manager,
            Command(command=CommandType.MACRO_UPDATE, data=update_payload),
        )
        if result is None:
            return daemon_unavailable_response()
        result_data = json_object(result.data)
        if result.status == "ok" and result_data is not None:
            updated = json_object(result_data.get("macro")) or {}
            await coordinator.refresh_macro_bindings(manager)
            manager.broadcast_to_session_clients(
                {"event": "macro_saved", "name": coerce_str(updated.get("name"), name)}
            )
            return {"status": "ok", "macro": updated}
        return {"status": "error", "message": result.error or "Failed to update macro"}

    if command == "delete_macro":
        name = coerce_str(request.get("name"), "")
        delete_payload: JsonObject = {"name": name}
        if "expected_revision" in request:
            delete_payload["expected_revision"] = request.get("expected_revision")
        result = await send_daemon_request(
            manager,
            Command(command=CommandType.MACRO_DELETE, data=delete_payload),
        )
        if result is None:
            return daemon_unavailable_response()
        if result.status != "ok":
            return {"status": "error", "message": result.error or "Failed to delete macro"}
        await coordinator.refresh_macro_bindings(manager)
        manager.broadcast_to_session_clients({"event": "macro_deleted", "name": name})
        return {"status": "ok"}

    if command == "rename_macro":
        rename_payload: JsonObject = {
            "old_name": coerce_str(request.get("old"), ""),
            "new_name": coerce_str(request.get("new"), ""),
        }
        if "expected_revision" in request:
            rename_payload["expected_revision"] = request.get("expected_revision")
        result = await send_daemon_request(
            manager,
            Command(command=CommandType.MACRO_RENAME, data=rename_payload),
        )
        if result is None:
            return daemon_unavailable_response()
        if result.status != "ok":
            return {"status": "error", "message": result.error or "Failed to rename macro"}
        await coordinator.refresh_macro_bindings(manager)
        result_data = json_object(result.data)
        if result_data is not None:
            renamed = json_object(result_data.get("macro")) or {}
            manager.broadcast_to_session_clients(
                {"event": "macro_deleted", "name": coerce_str(request.get("old"), "")}
            )
            manager.broadcast_to_session_clients(
                {"event": "macro_saved", "name": coerce_str(renamed.get("name"), "")}
            )
            return {"status": "ok", "macro": renamed}
        return {"status": "ok"}

    if command == "play_macro":
        name = coerce_str(request.get("name"), "")
        result = await send_daemon_request(
            manager,
            Command(
                command=CommandType.MACRO_PLAY_BY_NAME,
                data={
                    "name": name,
                    **(
                        {"playback_id": request["playback_id"]}
                        if request.get("playback_id")
                        else {}
                    ),
                    "replay_mouse_movement": request.get("replay_mouse_movement", True),
                    "replay_mouse_clicks": request.get("replay_mouse_clicks", True),
                    "speed": coerce_float(request.get("speed"), 1.0),
                },
            ),
        )
        if result is None:
            return daemon_unavailable_response()
        if result.status == "ok":
            response_data = json_object(result.data)
            return response_data if response_data else {"status": "ok"}
        return {"status": "error", "message": result.error or "playback failed"}

    if command == "type_text":
        text = coerce_str(request.get("text"), "")
        speed = coerce_float(request.get("speed"), 1.0)
        try:
            events = await asyncio.to_thread(
                _compile_type_text_macro,
                text,
                max(0, coerce_int(request.get("down_ms"), DEFAULT_TYPE_MACRO_DOWN_MS)),
                max(0, coerce_int(request.get("pause_ms"), DEFAULT_TYPE_MACRO_PAUSE_MS)),
                bool(request.get("use_unicode_input", True)),
            )
        except (TypeError, ValueError) as exc:
            return {"status": "error", "message": str(exc)}

        result = await _play_type_macro(
            manager, events, speed, coerce_str(request.get("playback_id"), "")
        )
        if result.get("status") == "ok":
            result.setdefault("char_count", len(text))
            result.setdefault("event_count", len(events))
        return result

    if command == "cancel_macro_playback":
        if hasattr(manager, "playback_requests"):
            manager.playback_requests.cancel_pending()
        result = await send_daemon_request(
            manager,
            Command(command=CommandType.CANCEL_MACRO_PLAYBACK),
        )
        if result is None:
            return daemon_unavailable_response()
        if result.status == "ok":
            response_data = json_object(result.data)
            return response_data if response_data else {"status": "ok", "cancelled": True}
        return {"status": "error", "message": result.error or "cancel failed"}

    return None


def _compile_type_text_macro(
    text: str,
    down_ms: int,
    pause_ms: int,
    use_unicode_input: bool,
) -> list[JsonObject]:
    from keymasq.common.macro_compile import build_type_macro_events

    return build_type_macro_events(
        text,
        down_ms,
        pause_ms,
        use_unicode_input=use_unicode_input,
    )


async def _play_type_macro(
    manager: "SessionManager",
    events: list[JsonObject],
    speed: float,
    playback_id: str = "",
) -> JsonObject:
    if playback_id:
        job = manager.playback_requests.requests.get(playback_id)
        if job is not None and job.cancel_requested:
            return {"status": "error", "message": "Playback cancelled"}
    if not events:
        return {"status": "error", "message": "type macro produced no events"}

    macro = recording_lifecycle.sanitize_macro_for_policy(manager, {"events": events})
    sanitized_events = json_list(macro.get("events"))

    result = await send_daemon_request(
        manager,
        Command(
            command=CommandType.PLAY_MACRO,
            data={
                "macro_events": sanitized_events,
                **({"playback_id": playback_id} if playback_id else {}),
                "speed": speed,
            },
        ),
    )
    if result is None:
        return daemon_unavailable_response()
    if result.status == "ok":
        response_data = json_object(result.data)
        return response_data if response_data else {"status": "ok"}
    return {"status": "error", "message": result.error or "playback failed"}
