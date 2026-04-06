# pyright: reportPrivateUsage=false

import asyncio
import inspect
import logging
from typing import TYPE_CHECKING

from keyforge.common.ipc import Command, CommandType
from keyforge.common.security import PeerCredentials, command_allowed
from keyforge.session import manager_recording as runtime_recording
from keyforge.session.compositor import (
    get_compositor_capabilities,
    get_compositor_name,
    get_compositor_support_details,
)
from keyforge.session.manager_common import (
    JsonObject,
    float_value,
    int_value,
    json_object,
    merge_support_details,
    str_value,
)

if TYPE_CHECKING:
    from keyforge.session.manager import SessionManager

log = logging.getLogger("keyforge-session")


async def handle_session_request(
    manager: "SessionManager",
    request: JsonObject,
    client_class: str,
    peer: PeerCredentials,
    writer: asyncio.StreamWriter,
) -> JsonObject:
    command = str_value(request.get("command"), "")
    policy = manager._security_policy

    if not command_allowed(command, policy.session_command_acl, client_class):
        return {
            "status": "error",
            "message": f"{client_class} is not allowed to call '{command}'",
        }

    if runtime_recording.is_sensitive_session_command(
        manager,
        command, policy
    ) and not runtime_recording.is_refresh_owner_request(manager, peer, writer):
        return {
            "status": "error",
            "error_code": "sensitive_command_denied",
            "message": "Sensitive command denied: caller is not active GUI owner",
        }

    result = await _handle_profile_commands(manager, command, request)
    if result is not None:
        return result

    result = await _handle_compositor_commands(manager, command, request, peer, writer)
    if result is not None:
        return result

    result = await _handle_recording_commands(manager, command, request, peer, writer)
    if result is not None:
        return result

    result = await _handle_macro_commands(manager, command, request)
    if result is not None:
        return result

    result = await _handle_capture_commands(manager, command, request)
    if result is not None:
        return result

    if command == "set_diagnostics":
        return await _handle_set_diagnostics(manager, request)

    return {"error": f"Unknown command: {command}"}


async def _handle_profile_commands(
    manager: "SessionManager",
    command: str,
    request: JsonObject,
) -> JsonObject | None:
    if command == "get_active_profiles":
        return manager._build_active_profiles_payload()

    if command == "list_profiles":
        return manager._build_profile_overview()

    if command in {"enable_profile", "disable_profile", "toggle_profile"}:
        profile_name = str_value(request.get("profile_name"), "")
        if not profile_name:
            return {"status": "error", "message": "missing profile_name"}

        enabled: bool | None = None
        if command == "enable_profile":
            enabled = True
        elif command == "disable_profile":
            enabled = False

        return await manager._set_profile_enabled(profile_name, enabled)

    if command == "reload":
        await manager._reload_profiles()
        return {"status": "ok"}

    if command in {"reevaluate_profiles", "reevaluate_hardware"}:
        log.info("Global profile reevaluate requested")
        await asyncio.to_thread(manager._reload_config_from_disk)
        await manager._reevaluate_profiles()
        return {"status": "ok"}

    if command == "ping":
        return {"status": "ok"}

    return None


async def _handle_compositor_commands(
    manager: "SessionManager",
    command: str,
    request: JsonObject,
    peer: PeerCredentials,
    writer: asyncio.StreamWriter,
) -> JsonObject | None:
    _ = peer, writer
    if command == "get_compositor":
        details = merge_support_details(
            await get_compositor_support_details(manager._compositor_id, manager.dbus),
            manager._window_listener,
        )
        return {
            "compositor_id": manager._compositor_id,
            "compositor_name": get_compositor_name(manager._compositor_id),
            "supported": bool(details.get("supported", False)),
            "capabilities": get_compositor_capabilities(manager._compositor_id),
            "details": details,
            "listener_active": manager._window_listener is not None,
            "listener_name": (
                getattr(manager._window_listener, "name", "")
                if manager._window_listener is not None
                else ""
            ),
            "compositor_dispatch_available": manager._compositor_dispatch_available(),
        }

    if command == "get_active_window":
        return await manager._get_active_window_payload()

    if command == "activate_title":
        title = str_value(request.get("title"), "").strip()
        if not title:
            return {"status": "error", "message": "title parameter required"}
        listener = manager._window_listener
        activate_window_by_title = (
            getattr(listener, "activate_window_by_title", None) if listener is not None else None
        )
        if not callable(activate_window_by_title):
            return {
                "status": "error",
                "message": "Window activation not supported on this compositor",
            }
        try:
            result_obj = activate_window_by_title(title)
            if not inspect.isawaitable(result_obj):
                return {
                    "status": "error",
                    "message": "Window activation not supported on this compositor",
                }
            result = await result_obj
            if result and result.get("found"):
                return {"status": "ok", "title": title, "found": True}
            return {
                "status": "error",
                "message": f"Window with title {title!r} not found",
                "details": result,
            }
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    if command == "get_cursor_position":
        pos = None

        if manager._window_listener:
            try:
                pos = await manager._window_listener.get_cursor_position()
            except Exception as e:
                log.debug(
                    "Cursor query failed (compositor_id=%s listener=%s): %s",
                    manager._compositor_id,
                    getattr(manager._window_listener, "name", "unknown"),
                    e,
                )

        if pos is None:
            return {
                "status": "error",
                "message": "Cursor position is unavailable on this compositor",
            }
        return {"status": "ok", "x": int(pos[0]), "y": int(pos[1])}

    if command == "get_status":
        unlock_status = await runtime_recording.resolve_unlock_status_async(manager, peer.uid)
        compositor_details = merge_support_details(
            await get_compositor_support_details(manager._compositor_id, manager.dbus),
            manager._window_listener,
        )
        policy = manager._security_policy
        return {
            "status": "ok",
            "keyforged_connected": manager._connected,
            "compositor_id": manager._compositor_id,
            "compositor_name": get_compositor_name(manager._compositor_id),
            "compositor_supported": bool(compositor_details.get("supported", False)),
            "compositor_details": compositor_details,
            "listener_active": manager._window_listener is not None,
            "listener_name": (
                getattr(manager._window_listener, "name", "")
                if manager._window_listener is not None
                else ""
            ),
            "compositor_dispatch_available": manager._compositor_dispatch_available(),
            "active_profiles": list(manager._active_profile_names),
            "recording_active": manager.recording_state.active,
            "macro_exec_timeout_max_ms": int(policy.macro_exec_timeout_max_ms),
            "gui_allow_left_right_click_remap": bool(policy.gui_allow_left_right_click_remap),
            **runtime_recording.serialize_recording_unlock_state(
                manager,
                unlock_status,
                refresh_owner=runtime_recording.is_refresh_owner_request(manager, peer, writer),
            ),
        }

    return None


async def _handle_recording_commands(
    manager: "SessionManager",
    command: str,
    request: JsonObject,
    peer: PeerCredentials,
    writer: asyncio.StreamWriter,
) -> JsonObject | None:
    if command == "start_recording":
        runtime_recording.update_recording_settings(manager, request)
        start_result = await runtime_recording.start_recording(manager, reset_if_active=False)
        runtime_recording.notify_recording_unlock_required(manager, start_result)
        return start_result

    if command == "set_recording_settings":
        runtime_recording.update_recording_settings(manager, request)
        return {"status": "ok", **manager.recording_state.settings}

    if command == "get_recording_settings":
        unlock_status = await runtime_recording.resolve_unlock_status_async(manager, peer.uid)
        return {
            "status": "ok",
            **runtime_recording.serialize_recording_unlock_state(
                manager,
                unlock_status,
                refresh_owner=runtime_recording.is_refresh_owner_request(manager, peer, writer),
            ),
            **manager.recording_state.settings,
        }

    if command == "claim_recording_unlock_refresh":
        return await runtime_recording.claim_recording_unlock_refresh(manager, peer, writer)

    if command == "refresh_recording_unlock":
        lease_id = str_value(request.get("lease_id"), "").strip()
        return await runtime_recording.refresh_recording_unlock(manager, peer, writer, lease_id)

    if command == "lock_recording_unlock":
        lease_id = str_value(request.get("lease_id"), "").strip()
        return await runtime_recording.lock_recording_unlock(manager, peer, writer, lease_id)

    if command == "stop_recording":
        return await runtime_recording.stop_recording(manager, error_if_idle=True)

    if command == "save_recording":
        name = str_value(request.get("name"), "").strip()
        if not name:
            return {"status": "error", "message": "Name required"}
        if not manager.recording_state.pending_data:
            return {"status": "error", "message": "No pending recording"}
        save_result = await runtime_recording.save_recording(
            manager,
            name,
            move_to_start=bool(request.get("move_to_start", False)),
            start_x=int_value(request.get("start_x"), 0),
            start_y=int_value(request.get("start_y"), 0),
            block_mouse_movement=bool(request.get("block_mouse_movement", False)),
        )
        if save_result.get("status") != "ok":
            return save_result
        return {"status": "ok", "name": save_result.get("name", name)}

    if command == "discard_recording":
        manager.recording_state.pending_data = None
        return {"status": "ok"}

    return None


async def _handle_macro_commands(
    manager: "SessionManager",
    command: str,
    request: JsonObject,
) -> JsonObject | None:
    if command == "list_macros":
        try:
            result = await manager.client.send_command(Command(command=CommandType.MACRO_LIST_META))
        except Exception:
            return {"status": "error", "message": "Daemon unavailable"}
        result_data = json_object(result.data)
        if result.status == "ok" and result_data is not None:
            return {"status": "ok", "macros": result_data.get("macros", [])}
        return {"status": "error", "message": result.error or "Failed to list macros"}

    if command == "get_macro":
        name = str_value(request.get("name"), "")
        try:
            result = await manager.client.send_command(
                Command(command=CommandType.MACRO_GET, data={"name": name})
            )
        except Exception:
            return {"status": "error", "message": "Daemon unavailable"}
        result_data = json_object(result.data)
        if result.status == "ok" and result_data is not None:
            return {"status": "ok", "macro": result_data.get("macro")}
        return {"status": "error", "message": result.error or "Macro not found"}

    if command == "create_macro":
        macro = json_object(request.get("macro"))
        if macro is None:
            return {"status": "error", "message": "macro payload required"}
        try:
            result = await manager.client.send_command(
                Command(command=CommandType.MACRO_CREATE, data={"macro": macro})
            )
        except Exception:
            return {"status": "error", "message": "Daemon unavailable"}
        result_data = json_object(result.data)
        if result.status == "ok" and result_data is not None:
            created = json_object(result_data.get("macro")) or {}
            manager._broadcast_to_session_clients(
                {"event": "macro_saved", "name": str_value(created.get("name"), "")}
            )
            return {"status": "ok", "macro": created}
        return {"status": "error", "message": result.error or "Failed to create macro"}

    if command == "update_macro":
        name = str_value(request.get("name"), "")
        macro = json_object(request.get("macro"))
        if macro is None:
            return {"status": "error", "message": "macro payload required"}
        update_payload: JsonObject = {"name": name, "macro": macro}
        if "expected_revision" in request:
            update_payload["expected_revision"] = request.get("expected_revision")
        try:
            result = await manager.client.send_command(
                Command(command=CommandType.MACRO_UPDATE, data=update_payload)
            )
        except Exception:
            return {"status": "error", "message": "Daemon unavailable"}
        result_data = json_object(result.data)
        if result.status == "ok" and result_data is not None:
            updated = json_object(result_data.get("macro")) or {}
            manager._broadcast_to_session_clients(
                {"event": "macro_saved", "name": str_value(updated.get("name"), name)}
            )
            return {"status": "ok", "macro": updated}
        return {"status": "error", "message": result.error or "Failed to update macro"}

    if command == "delete_macro":
        name = str_value(request.get("name"), "")
        delete_payload: JsonObject = {"name": name}
        if "expected_revision" in request:
            delete_payload["expected_revision"] = request.get("expected_revision")
        try:
            result = await manager.client.send_command(
                Command(command=CommandType.MACRO_DELETE, data=delete_payload)
            )
        except Exception:
            return {"status": "error", "message": "Daemon unavailable"}
        if result.status != "ok":
            return {"status": "error", "message": result.error or "Failed to delete macro"}
        await manager._reload_profiles()
        return {"status": "ok"}

    if command == "rename_macro":
        rename_payload: JsonObject = {
            "old_name": str_value(request.get("old"), ""),
            "new_name": str_value(request.get("new"), ""),
        }
        if "expected_revision" in request:
            rename_payload["expected_revision"] = request.get("expected_revision")
        try:
            result = await manager.client.send_command(
                Command(command=CommandType.MACRO_RENAME, data=rename_payload)
            )
        except Exception:
            return {"status": "error", "message": "Daemon unavailable"}
        if result.status != "ok":
            return {"status": "error", "message": result.error or "Failed to rename macro"}
        await manager._reload_profiles()
        result_data = json_object(result.data)
        if result_data is not None:
            return {"status": "ok", "macro": result_data.get("macro")}
        return {"status": "ok"}

    if command == "play_macro":
        name = str_value(request.get("name"), "")
        try:
            get_result = await manager.client.send_command(
                Command(command=CommandType.MACRO_GET, data={"name": name})
            )
        except Exception:
            return {"status": "error", "message": "Daemon unavailable"}

        get_result_data = json_object(get_result.data)
        if get_result.status != "ok" or get_result_data is None:
            return {"status": "error", "message": get_result.error or "Macro not found"}

        macro = json_object(get_result_data.get("macro"))
        if macro is None:
            return {"status": "error", "message": "Macro not found"}

        macro = runtime_recording.sanitize_macro_for_policy(manager, macro)
        payload: JsonObject = {
            "macro_name": str(macro.get("name", name) or name),
            "macro_events": macro.get("events", []),
            "replay_mouse_movement": request.get("replay_mouse_movement", True),
            "replay_mouse_clicks": request.get("replay_mouse_clicks", True),
            "speed": float_value(request.get("speed"), 1.0),
            "loop_mode": str(macro.get("loop_mode", "none") or "none"),
            "loop_count": int_value(macro.get("loop_count"), 1),
            "move_to_start": bool(macro.get("move_to_start", False)),
            "start_x": int_value(macro.get("start_x"), 0),
            "start_y": int_value(macro.get("start_y"), 0),
            "block_mouse_movement": bool(macro.get("block_mouse_movement", False)),
        }

        try:
            result = await manager.client.send_command(
                Command(command=CommandType.PLAY_MACRO, data=payload)
            )
        except Exception:
            return {"status": "error", "message": "Daemon unavailable"}
        if result.status == "ok":
            response_data = json_object(result.data)
            return response_data if response_data else {"status": "ok"}
        return {"status": "error", "message": result.error or "playback failed"}

    if command == "cancel_macro_playback":
        try:
            result = await manager.client.send_command(
                Command(command=CommandType.CANCEL_MACRO_PLAYBACK)
            )
        except Exception:
            return {"status": "error", "message": "Daemon unavailable"}
        if result.status == "ok":
            response_data = json_object(result.data)
            return response_data if response_data else {"status": "ok", "cancelled": True}
        return {"status": "error", "message": result.error or "cancel failed"}

    return None


async def _handle_capture_commands(
    manager: "SessionManager",
    command: str,
    request: JsonObject,
) -> JsonObject | None:
    if command == "list_devices_for_recording":
        devices = await runtime_recording.get_devices_for_recording(
            manager,
            ["keyboard", "gamepad", "mouse"],
            include_grabbed=True,
        )
        manager.recording_state.devices_cache = [
            d for d in devices if not d.get("grabbed_by_keyforge")
        ]
        return {"status": "ok", "devices": devices}

    if command == "begin_capture":
        hardware_id = str_value(request.get("hardware_id"), "")
        if not hardware_id:
            return {"error": "missing hardware_id"}
        return await runtime_recording.capture_begin(manager, hardware_id)

    if command == "capture_read":
        hardware_id = str_value(request.get("hardware_id"), "")
        if not hardware_id:
            return {"error": "missing hardware_id"}
        return await runtime_recording.capture_read(manager, hardware_id)

    if command == "end_capture":
        hardware_id = str_value(request.get("hardware_id"), "")
        if not hardware_id:
            return {"error": "missing hardware_id"}
        return await runtime_recording.capture_end(manager, hardware_id)

    if command == "capture_combo":
        profile_name = str_value(request.get("profile_name"), "")
        if not profile_name:
            return {"error": "missing profile_name"}
        timeout_s = float_value(request.get("timeout_s"), 15.0)
        return await runtime_recording.capture_combo(manager, profile_name, timeout_s)

    return None


async def _handle_set_diagnostics(
    manager: "SessionManager",
    request: JsonObject,
) -> JsonObject:
    enabled = bool(request.get("enabled", False))
    interval = float_value(request.get("interval"), 5.0)
    try:
        result = await manager.client.send_command(
            Command(
                command=CommandType.SET_DIAGNOSTICS,
                data={"enabled": enabled, "interval": interval},
            )
        )
    except Exception:
        return {"status": "error", "message": "Daemon unavailable"}

    if result.status == "ok":
        return {"status": "ok", "data": result.data or {}}
    return {"status": "error", "message": result.error or "Failed to update diagnostics"}
