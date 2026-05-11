import asyncio
import logging
from typing import TYPE_CHECKING

from keymasq.common.ipc import Command, CommandType

from . import compositor as runtime_compositor
from . import profiles as runtime_profiles
from . import recording as runtime_recording
from .common import JsonObject
from .common import int_value as _int_value
from .common import json_list as _json_list
from .common import str_value as _str_value

if TYPE_CHECKING:
    from .core import SessionManager

log = logging.getLogger("keymasq-session")
GRAB_RETRY_DELAY_S = 5.0
TOPOLOGY_REFRESH_DEBOUNCE_S = 0.5
TOPOLOGY_REFRESH_RETRY_S = 1.0


async def handle_event(
    manager: "SessionManager",
    event_type: CommandType,
    data: JsonObject,
) -> None:
    if manager.verbosity >= 1:
        log.debug("Event: %s -> %s", event_type.value, event_log_view(data))

    if event_type == CommandType.ACTION_TRIGGER:
        exec_ref_raw = data.get("exec_ref")
        exec_ref = _int_value(exec_ref_raw, -1) if exec_ref_raw is not None else None
        if exec_ref is not None:
            if exec_ref >= 10000:
                ref_data = manager.exec_state.superkey_exec_refs.get(exec_ref)
                if ref_data:
                    hardware_id, cmd = ref_data
                    exec_data = dict(data)
                    exec_data["cmd"] = cmd
                    exec_data["hardware_id"] = hardware_id
                    asyncio.create_task(handle_exec_trigger(manager, exec_data))
                else:
                    log.warning("Unknown superkey exec_ref: %s", exec_ref)
            else:
                cmd = manager.exec_state.exec_refs.get(exec_ref)
                if cmd:
                    exec_data = dict(data)
                    exec_data["cmd"] = cmd
                    asyncio.create_task(handle_exec_trigger(manager, exec_data))
                else:
                    log.warning("Unknown exec_ref: %s", exec_ref)

        action_type_str = str(data.get("action_type", "") or "")
        if action_type_str == "start_macro_recording":
            asyncio.create_task(handle_start_macro_trigger(manager))
        elif action_type_str == "stop_macro_recording":
            asyncio.create_task(handle_stop_macro_trigger(manager))
        elif action_type_str == "cancel_macro_playback":
            asyncio.create_task(handle_cancel_macro_trigger(manager))
        elif action_type_str == "emergency_reset":
            asyncio.create_task(handle_emergency_reset_trigger(manager))
        elif action_type_str in {"profile_enable", "profile_disable", "profile_toggle"}:
            asyncio.create_task(handle_profile_trigger(manager, data))
        elif action_type_str == "exec" and exec_ref is None:
            asyncio.create_task(handle_exec_trigger(manager, data))
        elif action_type_str == "compositor_dispatch":
            asyncio.create_task(
                runtime_compositor.handle_compositor_dispatch_trigger(manager, data)
            )
        elif action_type_str == "macro":
            asyncio.create_task(runtime_recording.play_macro_trigger(manager, data))
        return

    if event_type == CommandType.SET_CURSOR_POSITION:
        schedule_cursor_position_request(manager, data)
        return

    if event_type == CommandType.DEVICE_CONNECTED:
        log.info("Device connected: %s", data)
        await on_device_connected(manager, data)
        return

    if event_type == CommandType.DEVICE_DISCONNECTED:
        log.info("Device disconnected: %s", data)
        await on_device_disconnected(manager, data)
        return

    if event_type == CommandType.DEVICE_GRAB_STATUS:
        handle_device_grab_status_event(manager, data)
        return

    if event_type == CommandType.MACRO_PLAYBACK_CANCELLED:
        handle_macro_playback_cancelled_event(manager, data)
        return

    if event_type == CommandType.RUNTIME_RESET:
        asyncio.create_task(handle_runtime_reset_event(manager, data))
        return

    if event_type == CommandType.DIAGNOSTICS_SNAPSHOT:
        manager.broadcast_to_session_clients({"event": "diagnostics_snapshot", **data})
        return

    if event_type == CommandType.RECORDING_STARTED:
        manager.recording_state.active = True
        manager.broadcast_to_session_clients({"event": "recording_started", **data})
        return

    if event_type == CommandType.RECORDING_STOPPED:
        manager.recording_state.active = False
        recording_data = dict(data)
        if manager.recording_state.start_cursor:
            recording_data["start_x"] = int(manager.recording_state.start_cursor[0])
            recording_data["start_y"] = int(manager.recording_state.start_cursor[1])
            recording_data["move_to_start"] = True
        if runtime_recording.has_pending_macro_save(manager):
            manager.recording_state.pending_data = recording_data
            pending_save_token = str(manager.recording_state.pending_save_token or "")
        else:
            pending_save_token = runtime_recording.begin_pending_macro_save(
                manager,
                recording_data,
            )
        manager.recording_state.start_cursor = None
        manager.broadcast_to_session_clients(
            {
                "event": "recording_stopped",
                "pending_save_token": pending_save_token,
                "duration_ms": recording_data.get("duration_ms", 0),
                "event_count": _int_value(recording_data.get("event_count"), 0),
                "device_types": recording_data.get("device_types", []),
                "start_x": recording_data.get("start_x"),
                "start_y": recording_data.get("start_y"),
                "move_to_start": recording_data.get("move_to_start", False),
            }
        )
        return

    if event_type == CommandType.RECORDING_PROGRESS:
        manager.broadcast_to_session_clients({"event": "recording_progress", **data})


def schedule_cursor_position_request(manager: "SessionManager", data: JsonObject) -> None:
    task = asyncio.create_task(handle_set_cursor_position_request(manager, data))
    manager.compositor_state.cursor_position_tasks.add(task)

    def _discard(done: asyncio.Task[None]) -> None:
        manager.compositor_state.cursor_position_tasks.discard(done)
        try:
            exc = done.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            log.debug("Cursor position request failed: %s", exc)

    task.add_done_callback(_discard)


async def handle_set_cursor_position_request(
    manager: "SessionManager",
    data: JsonObject,
) -> None:
    request_id = _str_value(data.get("request_id"), "")
    x = _int_value(data.get("x"), 0)
    y = _int_value(data.get("y"), 0)
    listener = manager.compositor_state.window_listener
    ok = False
    message = "No native cursor position listener available"

    if listener is not None and bool(
        getattr(listener, "supports_native_cursor_position_set", False)
    ):
        if manager.verbosity >= 1:
            log.debug(
                "Setting cursor position through %s listener: request_id=%s x=%s y=%s",
                type(listener).__name__,
                request_id or "<none>",
                x,
                y,
            )
        try:
            ok, message = await listener.set_cursor_position(x, y)
        except Exception as exc:
            ok = False
            message = str(exc)

    if not request_id:
        log.debug("Handled cursor position request without request_id: %s", message)
        return

    await send_cursor_position_result(
        manager,
        request_id=request_id,
        ok=ok,
        message=message,
    )


async def send_cursor_position_result(
    manager: "SessionManager",
    *,
    request_id: str,
    ok: bool,
    message: str,
) -> None:
    try:
        await manager.client.send_command(
            Command(
                command=CommandType.SET_CURSOR_POSITION_RESULT,
                data={
                    "request_id": request_id,
                    "ok": bool(ok),
                    "message": str(message or ""),
                },
            ),
            timeout=1.0,
        )
    except Exception as exc:
        log.debug("Failed to send cursor position result to keymasqd: %s", exc)


async def handle_start_macro_trigger(manager: "SessionManager") -> None:
    if manager.recording_state.active:
        await handle_stop_macro_trigger(manager)
        return

    if not runtime_recording.has_active_gui_recording_owner(manager):
        if manager.session_clients:
            log.info("Ignored start_macro_recording trigger: GUI recording is locked")
            manager.send_notification(
                "Keymasq: Recording Locked",
                "Unlock macro recording in Keymasq GUI before using recording triggers.",
            )
        else:
            log.info("Ignored start_macro_recording trigger: no active GUI recording owner")
            manager.send_notification(
                "Keymasq: Recording Unavailable",
                "Macro recording from triggers requires Keymasq GUI to be open.",
            )
        manager.broadcast_to_session_clients({"event": "recording_auth_requested"})
        return

    result = await runtime_recording.start_recording(manager, reset_if_active=False)
    if result.get("status") != "ok":
        if result.get("error_code") == runtime_recording.MACRO_SAVE_PENDING_ERROR_CODE:
            manager.broadcast_to_session_clients(
                {
                    "event": "macro_save_pending",
                    "message": result.get("message", ""),
                    "pending_save_token": result.get("pending_save_token", ""),
                }
            )
            return
        runtime_recording.notify_recording_unlock_required(manager, result)
        manager.broadcast_to_session_clients({"event": "recording_auth_requested"})


async def handle_stop_macro_trigger(manager: "SessionManager") -> None:
    if not manager.recording_state.active:
        return
    try:
        await runtime_recording.stop_recording(manager, error_if_idle=False)
    except Exception:
        pass


async def handle_cancel_macro_trigger(manager: "SessionManager") -> None:
    try:
        await manager.client.send_command(Command(command=CommandType.CANCEL_MACRO_PLAYBACK))
    except Exception:
        pass


async def handle_emergency_reset_trigger(manager: "SessionManager") -> None:
    try:
        await manager.client.send_command(Command(command=CommandType.EMERGENCY_RESET))
    except Exception:
        pass


async def handle_profile_trigger(manager: "SessionManager", data: JsonObject) -> None:
    action_type = str(data.get("action_type", "") or "").strip().lower()
    profile_name = str(data.get("profile_name", "") or "").strip()
    if not profile_name:
        return

    enabled: bool | None
    if action_type == "profile_enable":
        enabled = True
    elif action_type == "profile_disable":
        enabled = False
    else:
        enabled = None

    result = await runtime_profiles.set_profile_enabled(manager, profile_name, enabled)
    if result.get("status") != "ok":
        log.warning(
            "Profile trigger failed action=%s profile=%s message=%s",
            action_type,
            profile_name,
            result.get("message", "unknown error"),
        )


async def handle_exec_trigger(manager: "SessionManager", data: JsonObject) -> None:
    cmd = str(data.get("cmd", "") or "").strip()
    if not cmd:
        return

    wait_id = str(data.get("macro_exec_wait_id", "") or "").strip()
    is_async = bool(data.get("macro_exec_async", False))

    action_handler = manager.action_handler
    if action_handler is None:
        return

    if wait_id:
        returncode = await action_handler.execute_command(cmd)
        try:
            await manager.client.send_command(
                Command(
                    command=CommandType.MACRO_EXEC_COMPLETE,
                    data={"wait_id": wait_id, "returncode": int(returncode)},
                )
            )
        except Exception:
            pass
        return

    if is_async:
        action_handler.execute_command_sync(cmd)
        return

    await action_handler.execute_command(cmd)


def handle_device_grab_status_event(manager: "SessionManager", data: JsonObject) -> None:
    hardware_id = str(data.get("hardware_id", "") or "")
    state = str(data.get("state", "") or "").strip().lower()
    active_keys = [str(key) for key in _json_list(data.get("active_keys")) if str(key)]
    summary = ", ".join(active_keys) if active_keys else "unknown keys"

    manager.broadcast_to_session_clients({"event": "device_grab_status", **data})

    if not hardware_id:
        return

    device_name = device_name_for_hardware(manager, hardware_id)
    if state == "waiting":
        if hardware_id in manager.profile_state.grab_waiting_devices:
            return
        manager.profile_state.grab_waiting_devices.add(hardware_id)
        manager.send_notification(
            "Keymasq: Grab Pending",
            f"{device_name}: waiting for keys to be released ({summary}).",
        )
        return

    if state == "ready":
        manager.profile_state.grab_waiting_devices.discard(hardware_id)
        return

    if state == "timed_out":
        manager.profile_state.grab_waiting_devices.discard(hardware_id)
        manager.send_notification(
            "Keymasq: Grab Timed Out",
            f"{device_name}: keys stayed down too long ({summary}). Retrying automatically.",
        )
        runtime_profiles.schedule_grab_retry(manager, hardware_id, GRAB_RETRY_DELAY_S)


def handle_macro_playback_cancelled_event(
    manager: "SessionManager",
    data: JsonObject,
) -> None:
    manager.broadcast_to_session_clients({"event": "macro_playback_cancelled", **data})
    manager.send_notification(
        "Keymasq: Macro Playback Cancelled",
        "Stopped all running macro playback.",
    )


async def handle_runtime_reset_event(manager: "SessionManager", data: JsonObject) -> None:
    manager.broadcast_to_session_clients({"event": "runtime_reset", **data})
    manager.send_notification(
        "Keymasq: Emergency Reset",
        "Released all grabbed devices. Reapplying active profiles.",
    )
    runtime_profiles.invalidate_grabbed_state(manager)
    try:
        await runtime_profiles.reevaluate_profiles(manager, reason="runtime reset")
    except Exception as exc:
        log.warning("Failed to reapply profiles after runtime reset: %s", exc)
        manager.send_notification(
            "Keymasq: Reapply Failed",
            "Emergency reset completed, but active profiles could not be reapplied.",
        )


async def on_device_connected(manager: "SessionManager", device_info: JsonObject) -> None:
    hardware_id = (
        f"{_str_value(device_info.get('vendor_id'), '')}:"
        f"{_str_value(device_info.get('product_id'), '')}"
    )
    if not hardware_id or ":" not in hardware_id:
        return
    if (
        manager.hardware.get_hardware(hardware_id) is None
        and hardware_id not in manager.profile_state.resolved_devices
    ):
        return
    runtime_profiles.schedule_topology_refresh(
        manager,
        TOPOLOGY_REFRESH_DEBOUNCE_S,
        TOPOLOGY_REFRESH_RETRY_S,
    )
    asyncio.create_task(_refresh_recording_devices_cache_after_topology(manager))


async def on_device_disconnected(manager: "SessionManager", device_info: JsonObject) -> None:
    hardware_id = _str_value(device_info.get("hardware_id"), "")
    if not hardware_id or ":" not in hardware_id:
        hardware_id = (
            f"{_str_value(device_info.get('vendor_id'), '')}:"
            f"{_str_value(device_info.get('product_id'), '')}"
        )
    if not hardware_id or ":" not in hardware_id:
        return
    if (
        manager.hardware.get_hardware(hardware_id) is None
        and hardware_id not in manager.profile_state.resolved_devices
    ):
        return
    runtime_profiles.schedule_topology_refresh(
        manager,
        TOPOLOGY_REFRESH_DEBOUNCE_S,
        TOPOLOGY_REFRESH_RETRY_S,
    )
    asyncio.create_task(_refresh_recording_devices_cache_after_topology(manager))


async def _refresh_recording_devices_cache_after_topology(manager: "SessionManager") -> None:
    await asyncio.sleep(TOPOLOGY_REFRESH_DEBOUNCE_S + 0.1)
    await runtime_recording.refresh_recording_devices_cache(manager)


def device_name_for_hardware(manager: "SessionManager", hardware_id: str) -> str:
    hardware = manager.hardware.get_hardware(hardware_id)
    if hardware is None:
        return hardware_id
    return str(getattr(hardware, "name", "") or hardware_id)


def event_log_view(data: JsonObject) -> JsonObject:
    view = dict(data)
    events = _json_list(view.get("events"))
    if events:
        view["events"] = f"<{len(events)} events>"
        if "event_count" not in view:
            view["event_count"] = len(events)
    return view
