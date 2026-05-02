import asyncio
import logging
from typing import TYPE_CHECKING, cast

from keymasq.common.ipc import Command, CommandType
from keymasq.common.models import normalize_macro_loop_stop_behavior
from keymasq.common.security import PeerCredentials, command_allowed

from . import compositor as runtime_compositor
from . import profiles as runtime_profiles
from . import recording as runtime_recording
from .common import (
    JsonObject,
    float_value,
    int_value,
    json_list,
    json_object,
    str_value,
)

if TYPE_CHECKING:
    from .core import SessionManager

log = logging.getLogger("keymasq-session")


async def handle_session_request(
    manager: "SessionManager",
    request: JsonObject,
    client_class: str,
    peer: PeerCredentials,
    writer: asyncio.StreamWriter,
) -> JsonObject:
    command = str_value(request.get("command"), "")
    policy = manager.security_policy

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

    result = await _handle_compositor_commands(
        manager,
        command,
        request,
        client_class,
        peer,
        writer,
    )
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
        return runtime_profiles.build_active_profiles_payload(manager)

    if command == "list_profiles":
        return runtime_profiles.build_profile_overview(manager)

    if command in {"enable_profile", "disable_profile", "toggle_profile"}:
        profile_name = str_value(request.get("profile_name"), "")
        if not profile_name:
            return {"status": "error", "message": "missing profile_name"}

        enabled: bool | None = None
        if command == "enable_profile":
            enabled = True
        elif command == "disable_profile":
            enabled = False

        return await runtime_profiles.set_profile_enabled(manager, profile_name, enabled)

    if command == "reload":
        await manager.reload_profiles()
        return {"status": "ok"}

    if command in {"reevaluate_profiles", "reevaluate_hardware"}:
        log.info("Global profile reevaluate requested")
        await asyncio.to_thread(manager.reload_config_from_disk)
        await runtime_profiles.reevaluate_profiles(manager)
        return {"status": "ok"}

    if command == "ping":
        return {"status": "ok"}

    return None


async def _handle_compositor_commands(
    manager: "SessionManager",
    command: str,
    request: JsonObject,
    client_class: str,
    peer: PeerCredentials,
    writer: asyncio.StreamWriter,
) -> JsonObject | None:
    _ = peer, writer
    if command == "get_compositor":
        return await runtime_compositor.build_compositor_payload(manager)

    if command == "refresh_compositor":
        return await runtime_compositor.refresh_compositor_binding(manager)

    if command == "run_compositor_setup_action":
        return await runtime_compositor.run_compositor_setup_action(
            manager,
            str_value(request.get("compositor"), "").strip(),
            str_value(request.get("action"), "").strip(),
        )

    if command == "get_active_window":
        return await runtime_compositor.get_active_window_payload(manager)

    if command == "activate_title":
        return await runtime_compositor.activate_title(
            manager,
            str_value(request.get("title"), "").strip(),
        )

    if command == "dispatch_compositor":
        ok, message = await runtime_compositor.run_compositor_dispatch(
            manager,
            str_value(request.get("compositor"), "").strip(),
            str_value(request.get("dispatcher"), "").strip(),
            str_value(request.get("args"), "").strip(),
        )
        return {"status": "ok" if ok else "error", "message": message}

    if command == "get_cursor_position":
        return await runtime_compositor.get_cursor_position_payload(manager)

    if command == "get_status":
        unlock_status = await runtime_recording.resolve_unlock_status_async(manager, peer.uid)
        compositor_status = await runtime_compositor.build_compositor_payload(manager)
        compositor_details = cast(dict[str, object], compositor_status["details"])
        policy = manager.security_policy
        profile_payload = runtime_profiles.build_active_profiles_payload(manager)
        status_payload: JsonObject = {
            "status": "ok",
            "keymasqd_connected": manager.connected,
            "compositor_id": compositor_status["compositor_id"],
            "compositor_name": compositor_status["compositor_name"],
            "compositor_supported": bool(compositor_status["supported"]),
            "compositor_details": compositor_details,
            "listener_active": compositor_status["listener_active"],
            "listener_name": compositor_status["listener_name"],
            "compositor_dispatch_available": compositor_status["compositor_dispatch_available"],
            "recording_active": manager.recording_state.active,
            "macro_exec_timeout_max_ms": int(policy.macro_exec_timeout_max_ms),
            "emergency_cancel_combo_enabled": bool(policy.emergency_cancel_combo_enabled),
            **runtime_recording.serialize_recording_unlock_state(
                manager,
                unlock_status,
                refresh_owner=runtime_recording.is_refresh_owner_request(manager, peer, writer),
            ),
        }
        if command_allowed("get_active_profiles", policy.session_command_acl, client_class):
            status_payload["active_profiles"] = profile_payload["active_profiles"]
            status_payload["devices"] = profile_payload["devices"]
        if command_allowed("get_active_window", policy.session_command_acl, client_class):
            status_payload["window"] = manager.compositor_state.current_window
        return status_payload

    return None


async def _handle_recording_commands(
    manager: "SessionManager",
    command: str,
    request: JsonObject,
    peer: PeerCredentials,
    writer: asyncio.StreamWriter,
) -> JsonObject | None:
    if command == "start_recording":
        if runtime_recording.has_pending_macro_save(manager):
            return await runtime_recording.start_recording(
                manager,
                reset_if_active=False,
                owner_peer=peer,
                owner_writer=writer,
            )
        runtime_recording.update_recording_settings(manager, request)
        start_result = await runtime_recording.start_recording(
            manager,
            reset_if_active=False,
            owner_peer=peer,
            owner_writer=writer,
        )
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
        pending_save_token = str_value(request.get("pending_save_token"), "").strip()
        if pending_save_token and not runtime_recording.pending_macro_save_token_matches(
            manager,
            pending_save_token,
        ):
            return {
                "status": "error",
                "error_code": "stale_pending_macro_save",
                "message": "Pending recording has already changed.",
            }
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
        pending_save_token = str_value(request.get("pending_save_token"), "").strip()
        if pending_save_token and not runtime_recording.pending_macro_save_token_matches(
            manager,
            pending_save_token,
        ):
            return {
                "status": "error",
                "error_code": "stale_pending_macro_save",
                "message": "Pending recording has already changed.",
            }
        pending_data = manager.recording_state.pending_data or {}
        pending_recording_id = str_value(pending_data.get("pending_recording_id"), "")
        if pending_recording_id:
            try:
                await manager.client.send_command(
                    Command(
                        command=CommandType.MACRO_DISCARD_RECORDING,
                        data={"pending_recording_id": pending_recording_id},
                    )
                )
            except Exception:
                pass
        runtime_recording.clear_pending_macro_save(manager)
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
            await runtime_profiles.refresh_macro_bindings(manager)
            manager.broadcast_to_session_clients(
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
            await runtime_profiles.refresh_macro_bindings(manager)
            manager.broadcast_to_session_clients(
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
        await runtime_profiles.refresh_macro_bindings(manager)
        manager.broadcast_to_session_clients({"event": "macro_deleted", "name": name})
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
        await runtime_profiles.refresh_macro_bindings(manager)
        result_data = json_object(result.data)
        if result_data is not None:
            renamed = json_object(result_data.get("macro")) or {}
            manager.broadcast_to_session_clients(
                {"event": "macro_deleted", "name": str_value(request.get("old"), "")}
            )
            manager.broadcast_to_session_clients(
                {"event": "macro_saved", "name": str_value(renamed.get("name"), "")}
            )
            return {"status": "ok", "macro": renamed}
        return {"status": "ok"}

    if command == "play_macro":
        name = str_value(request.get("name"), "")
        try:
            result = await manager.client.send_command(
                Command(
                    command=CommandType.MACRO_PLAY_BY_NAME,
                    data={
                        "name": name,
                        "replay_mouse_movement": request.get("replay_mouse_movement", True),
                        "replay_mouse_clicks": request.get("replay_mouse_clicks", True),
                        "speed": float_value(request.get("speed"), 1.0),
                    },
                )
            )
        except Exception:
            return {"status": "error", "message": "Daemon unavailable"}
        if result.status == "ok":
            response_data = json_object(result.data)
            return response_data if response_data else {"status": "ok"}
        return {"status": "error", "message": result.error or "playback failed"}

    if command == "play_macro_payload":
        macro_events = [
            event
            for raw_event in json_list(request.get("macro_events"))
            if (event := json_object(raw_event)) is not None
        ]
        if not macro_events:
            return {"status": "error", "message": "macro_events required"}

        macro = runtime_recording.sanitize_macro_for_policy(manager, {"events": macro_events})
        sanitized_events = json_list(macro.get("events"))
        adhoc_payload: JsonObject = {
            "macro_name": str_value(request.get("macro_name"), ""),
            "macro_events": sanitized_events,
            "replay_mouse_movement": bool(request.get("replay_mouse_movement", True)),
            "replay_mouse_clicks": bool(request.get("replay_mouse_clicks", True)),
            "speed": float_value(request.get("speed"), 1.0),
            "loop_mode": str_value(request.get("loop_mode", "none"), "none") or "none",
            "loop_count": int_value(request.get("loop_count"), 1),
            "loop_stop_behavior": normalize_macro_loop_stop_behavior(
                request.get("loop_stop_behavior")
            ),
            "move_to_start": bool(request.get("move_to_start", False)),
            "start_x": int_value(request.get("start_x"), 0),
            "start_y": int_value(request.get("start_y"), 0),
            "block_mouse_movement": bool(request.get("block_mouse_movement", False)),
        }

        try:
            result = await manager.client.send_command(
                Command(command=CommandType.PLAY_MACRO, data=adhoc_payload)
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
        manager.recording_state.devices_cache = devices
        manager.recording_state.devices_cache_ready = True
        runtime_recording.update_selected_recording_devices_cache(manager)
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
