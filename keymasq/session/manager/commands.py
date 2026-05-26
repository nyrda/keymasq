import asyncio
import logging
from typing import TYPE_CHECKING, cast

from keymasq.common.ipc import Command, CommandType
from keymasq.common.models import normalize_macro_loop_stop_behavior
from keymasq.common.security import PeerCredentials, SecurityPolicy, command_allowed
from keymasq.common.settings import GlobalSettings
from keymasq.common.virtual_devices import MAX_VIRTUAL_GAMEPADS, MIN_VIRTUAL_GAMEPADS
from keymasq.session.settings import save_global_settings, save_virtual_gamepad_count

from . import combo_inspector as runtime_combo_inspector
from . import compositor as runtime_compositor
from . import device_inspector as runtime_device_inspector
from . import output_stream as runtime_output_stream
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
        command,
        request,
        policy,
    ) and not runtime_recording.is_refresh_owner_request(manager, peer, writer):
        return {
            "status": "error",
            "error_code": "sensitive_command_denied",
            "message": "Sensitive command denied: caller is not active GUI owner",
        }

    result = await _handle_profile_commands(manager, command, request, client_class, policy)
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

    result = await _handle_virtual_gamepad_commands(manager, command, request)
    if result is not None:
        return result

    result = await _handle_settings_commands(manager, command, request)
    if result is not None:
        return result

    if command == "set_diagnostics":
        result = await _handle_set_diagnostics(manager, request)
        runtime_recording.notify_recording_unlock_required(manager, result)
        return result

    if command == "set_diagnostics_output_stream":
        result = await runtime_output_stream.set_diagnostics_output_stream(
            manager,
            bool(request.get("enabled", False)),
            request.get("filters"),
            writer,
        )
        runtime_recording.notify_recording_unlock_required(manager, result)
        return result

    result = await _handle_device_inspector_commands(manager, command, request, peer, writer)
    if result is not None:
        return result

    return {"error": f"Unknown command: {command}"}


async def _handle_profile_commands(
    manager: "SessionManager",
    command: str,
    request: JsonObject,
    client_class: str,
    policy: SecurityPolicy,
) -> JsonObject | None:
    if command == "get_active_profiles":
        return runtime_profiles.build_active_profiles_payload(manager)

    if command == "get_combo_inspector_snapshot":
        if not command_allowed(
            "get_active_profiles",
            policy.session_command_acl,
            client_class,
        ):
            return {
                "status": "error",
                "message": (
                    f"{client_class} is not allowed to call "
                    "'get_combo_inspector_snapshot' while 'get_active_profiles' is denied"
                ),
            }
        return runtime_combo_inspector.build_combo_inspector_snapshot(manager)

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
        if await manager.reload_profiles():
            return {"status": "ok"}
        return {
            "status": "error",
            "message": "Failed to reload config; keeping previous active config",
        }

    if command == "release_device":
        hardware_id = str_value(request.get("hardware_id"), "").strip()
        if not hardware_id:
            return {"status": "error", "message": "missing hardware_id"}
        immediate = bool(request.get("immediate", True))
        try:
            result = await manager.client.send_command(
                Command(
                    command=CommandType.RELEASE_DEVICE,
                    data={"hardware_id": hardware_id, "immediate": immediate},
                )
            )
        except Exception:
            return {"status": "error", "message": "Daemon unavailable"}
        if result.status != "ok":
            return {
                "status": "error",
                "message": result.error or f"Failed to release {hardware_id}",
            }
        runtime_profiles.clear_hardware_runtime_state(manager, hardware_id)
        response_data = json_object(result.data)
        response = response_data if response_data else {}
        response["status"] = "ok"
        return response

    if command in {"reevaluate_profiles", "reevaluate_hardware"}:
        log.info("Global profile reevaluate requested")
        try:
            await asyncio.to_thread(manager.reload_config_from_disk)
        except Exception as exc:
            log.exception("Failed to reload user config from disk for reevaluate request")
            manager.send_notification(
                "Keymasq Config Error",
                "Failed to reload config; keeping the previous active config. See logs.",
            )
            return {"status": "error", "message": str(exc)}
        runtime_profiles.invalidate_runtime_payload_signatures(manager)
        await runtime_profiles.reevaluate_profiles(manager, reason="session command reevaluate")
        return {"status": "ok"}

    if command == "ping":
        return {"status": "ok"}

    return None


async def _handle_virtual_gamepad_commands(
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

    requested_count = int_value(request.get("count"), manager.virtual_gamepad_count)
    count = save_virtual_gamepad_count(requested_count)
    manager.virtual_gamepad_count = count
    if manager.connected:
        response = await manager.client.send_command(
            Command(command=CommandType.SET_VIRTUAL_GAMEPADS, data={"count": count})
        )
        if response.status != "ok":
            return {"status": "error", "message": response.error or "daemon rejected count"}
        if isinstance(response.data, dict):
            data = cast(JsonObject, response.data)
            count = int_value(data.get("count"), count)
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


async def _handle_device_inspector_commands(
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

    hardware_id = str_value(request.get("hardware_id"), "").strip()
    if not hardware_id:
        return {"status": "error", "message": "missing hardware_id"}

    if command == "get_device_inspector_snapshot":
        return runtime_device_inspector.build_device_inspector_snapshot(manager, hardware_id)
    if command == "start_device_inspector":
        return await runtime_device_inspector.start_device_inspector(
            manager,
            hardware_id,
            peer,
            writer,
        )
    if command == "stop_device_inspector":
        return await runtime_device_inspector.stop_device_inspector(
            manager,
            hardware_id,
            writer,
        )
    if command == "enable_device_inspector_suppression":
        return await runtime_device_inspector.enable_device_inspector_suppression(
            manager,
            hardware_id,
            writer,
        )
    return await runtime_device_inspector.disable_device_inspector_suppression(
        manager,
        hardware_id,
        reason=str_value(request.get("reason", "manual"), "manual"),
    )


async def _handle_settings_commands(
    manager: "SessionManager",
    command: str,
    request: JsonObject,
) -> JsonObject | None:
    if command == "get_settings":
        return _settings_payload(manager)
    if command != "set_settings":
        return None

    requested_count = int_value(request.get("virtual_gamepad_count"), manager.virtual_gamepad_count)
    saved = save_global_settings(
        GlobalSettings(
            virtual_gamepad_count=requested_count,
        )
    )
    manager.virtual_gamepad_count = saved.virtual_gamepad_count
    gamepad_error = ""

    if manager.connected:
        response = await manager.client.send_command(
            Command(
                command=CommandType.SET_VIRTUAL_GAMEPADS,
                data={"count": int(manager.virtual_gamepad_count)},
            )
        )
        if response.status != "ok":
            gamepad_error = response.error or "daemon rejected virtual gamepad count"
        elif isinstance(response.data, dict):
            data = cast(JsonObject, response.data)
            manager.virtual_gamepad_count = int_value(
                data.get("count"),
                manager.virtual_gamepad_count,
            )
            saved = save_global_settings(
                GlobalSettings(
                    virtual_gamepad_count=manager.virtual_gamepad_count,
                )
            )
            manager.virtual_gamepad_count = saved.virtual_gamepad_count

    payload = _settings_payload(manager)
    if gamepad_error:
        payload["status"] = "error"
        payload["message"] = gamepad_error
        return payload
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

    if command == "type_text":
        text = str_value(request.get("text"), "")
        try:
            events, payload = await asyncio.to_thread(
                _compile_type_text_macro,
                text,
                max(0, int_value(request.get("down_ms"), 10)),
                max(0, int_value(request.get("pause_ms"), 20)),
                bool(request.get("use_unicode_input", True)),
                float_value(request.get("speed"), 1.0),
            )
        except (TypeError, ValueError) as exc:
            return {"status": "error", "message": str(exc)}

        result = await _send_adhoc_macro_payload(manager, payload)
        if result.get("status") == "ok":
            result.setdefault("char_count", len(text))
            result.setdefault("event_count", len(events))
        return result

    if command == "play_compact_macro":
        tokens = [str(token) for token in json_list(request.get("tokens")) if str(token)]
        if not tokens:
            return {"status": "error", "message": "tokens required"}

        try:
            events, payload = await asyncio.to_thread(
                _compile_compact_macro,
                tokens,
                float_value(request.get("speed"), 1.0),
            )
        except (TypeError, ValueError) as exc:
            return {"status": "error", "message": str(exc)}

        result = await _send_adhoc_macro_payload(manager, payload)
        if result.get("status") == "ok":
            result.setdefault("event_count", len(events))
        return result

    if command == "play_macro_payload":
        return await _send_adhoc_macro_payload(manager, request)

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


def _compile_type_text_macro(
    text: str,
    down_ms: int,
    pause_ms: int,
    use_unicode_input: bool,
    speed: float,
) -> tuple[list[JsonObject], JsonObject]:
    from keymasq.common.macro_compile import build_macro_payload, build_type_macro_events

    events = build_type_macro_events(
        text,
        down_ms,
        pause_ms,
        use_unicode_input=use_unicode_input,
    )
    return events, build_macro_payload(events, speed=speed)


def _compile_compact_macro(tokens: list[str], speed: float) -> tuple[list[JsonObject], JsonObject]:
    from keymasq.common.macro_compile import build_compact_macro_events, build_macro_payload

    events = build_compact_macro_events(tokens)
    return events, build_macro_payload(events, speed=speed)


async def _send_adhoc_macro_payload(
    manager: "SessionManager",
    payload: JsonObject,
) -> JsonObject:
    macro_events = [
        event
        for raw_event in json_list(payload.get("macro_events"))
        if (event := json_object(raw_event)) is not None
    ]
    if not macro_events:
        return {"status": "error", "message": "macro_events required"}

    macro = runtime_recording.sanitize_macro_for_policy(manager, {"events": macro_events})
    sanitized_events = json_list(macro.get("events"))
    adhoc_payload: JsonObject = {
        "macro_name": str_value(payload.get("macro_name"), ""),
        "macro_events": sanitized_events,
        "replay_mouse_movement": bool(payload.get("replay_mouse_movement", True)),
        "replay_mouse_clicks": bool(payload.get("replay_mouse_clicks", True)),
        "speed": float_value(payload.get("speed"), 1.0),
        "loop_mode": str_value(payload.get("loop_mode", "none"), "none") or "none",
        "loop_count": int_value(payload.get("loop_count"), 1),
        "loop_stop_behavior": normalize_macro_loop_stop_behavior(
            payload.get("loop_stop_behavior")
        ),
        "move_to_start": bool(payload.get("move_to_start", False)),
        "start_x": int_value(payload.get("start_x"), 0),
        "start_y": int_value(payload.get("start_y"), 0),
        "block_mouse_movement": bool(payload.get("block_mouse_movement", False)),
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


async def _handle_capture_commands(
    manager: "SessionManager",
    command: str,
    request: JsonObject,
) -> JsonObject | None:
    if command == "list_devices_for_recording":
        device_types = ["keyboard", "gamepad", "mouse"]
        if bool(request.get("include_other", False)):
            device_types = ["keyboard", "gamepad", "mouse", "touchpad", "pointstick", "other"]
        devices = await runtime_recording.get_devices_for_recording(
            manager,
            device_types,
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
        evdev_paths = [
            str_value(path, "")
            for path in json_list(request.get("evdev_paths"))
            if str_value(path, "")
        ]
        evdev_interfaces_raw = request.get("evdev_interfaces")
        evdev_interfaces = (
            [
                cast(JsonObject, item)
                for item in json_list(evdev_interfaces_raw)
                if isinstance(item, dict)
            ]
            if evdev_interfaces_raw is not None
            else None
        )
        mode = str_value(request.get("mode"), "button")
        return await runtime_recording.capture_begin_for_paths(
            manager,
            hardware_id,
            evdev_paths,
            evdev_interfaces=evdev_interfaces,
            mode=mode,
        )

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
    categories = [
        str_value(category, "")
        for category in json_list(request.get("categories"))
        if str_value(category, "")
    ]
    try:
        result = await manager.client.send_command(
            Command(
                command=CommandType.SET_DIAGNOSTICS,
                data={"enabled": enabled, "interval": interval, "categories": categories},
            )
        )
    except Exception:
        return {"status": "error", "message": "Daemon unavailable"}

    if result.status == "ok":
        return {"status": "ok", "data": result.data or {}}
    message = result.error or "Failed to update diagnostics"
    response: JsonObject = {"status": "error", "message": message}
    if "recording_locked" in message.lower():
        response["error_code"] = "recording_locked"
    return response
