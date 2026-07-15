import asyncio
from typing import TYPE_CHECKING, cast

from keymasq.common.coercion import coerce_str
from keymasq.common.model.actions import parse_mpris_command
from keymasq.common.security import PeerCredentials
from keymasq.session.mpris import MprisDBusError

from .. import compositor, recording_unlock
from ..common import JsonObject
from ..profile import runtime_status

if TYPE_CHECKING:
    from ..core import SessionManager


async def handle_compositor_commands(
    manager: "SessionManager",
    command: str,
    request: JsonObject,
    peer: PeerCredentials,
    writer: asyncio.StreamWriter,
) -> JsonObject | None:
    _ = peer, writer
    if command == "get_compositor":
        return await compositor.build_compositor_payload(manager)

    if command == "refresh_compositor":
        return await compositor.refresh_compositor_binding(manager)

    if command == "run_compositor_setup_action":
        return await compositor.run_compositor_setup_action(
            manager,
            coerce_str(request.get("compositor"), "").strip(),
            coerce_str(request.get("action"), "").strip(),
        )

    if command == "get_active_window":
        return await compositor.get_active_window_payload(manager)

    if command == "activate_title":
        return await compositor.activate_title(
            manager,
            coerce_str(request.get("title"), "").strip(),
        )

    if command == "dispatch_compositor":
        ok, message = await compositor.run_compositor_dispatch(
            manager,
            coerce_str(request.get("compositor"), "").strip(),
            coerce_str(request.get("dispatcher"), "").strip(),
            coerce_str(request.get("args"), "").strip(),
        )
        return {"status": "ok" if ok else "error", "message": message}

    if command == "get_cursor_position":
        return await compositor.get_cursor_position_payload(manager)

    if command == "mpris":
        requested_command = request.get("mpris_command", request.get("action"))
        raw_command = coerce_str(requested_command, "").strip()
        if raw_command.lower().replace("-", "_") == "status":
            return {
                "status": "ok",
                "command": "status",
                "mpris": manager.mpris_controller.status_snapshot(),
            }
        mpris_command = parse_mpris_command(requested_command)
        if mpris_command is None:
            message = (
                f"unknown MPRIS command: {raw_command}" if raw_command else "missing mpris_command"
            )
            return {"status": "error", "message": message}
        try:
            await manager.mpris_controller.handle_command(mpris_command, raise_on_error=True)
        except MprisDBusError as exc:
            return {
                "status": "error",
                "command": mpris_command,
                "message": str(exc),
                "mpris": manager.mpris_controller.status_snapshot(),
            }
        return {
            "status": "ok",
            "command": mpris_command,
            "mpris": manager.mpris_controller.status_snapshot(),
        }

    if command == "get_status":
        unlock_status = await recording_unlock.resolve_unlock_status_async(manager, peer.uid)
        macro_recording_status = await recording_unlock.resolve_macro_recording_status_async(
            manager,
            peer.uid,
        )
        compositor_status = await compositor.build_compositor_payload(manager)
        compositor_details = cast(dict[str, object], compositor_status["details"])
        policy = manager.security_policy
        await runtime_status.refresh_device_runtime_status(manager)
        profile_payload = runtime_status.build_active_profiles_payload(manager)
        status_payload: JsonObject = {
            "status": "ok",
            "keymasqd_connected": manager.connected,
            "compositor_id": compositor_status["compositor_id"],
            "compositor_name": compositor_status["compositor_name"],
            "compositor_supported": bool(compositor_status["supported"]),
            "compositor_details": compositor_details,
            "compositor_capabilities": compositor_status["capabilities"],
            "listener_active": compositor_status["listener_active"],
            "listener_name": compositor_status["listener_name"],
            "compositor_dispatch_available": compositor_status["compositor_dispatch_available"],
            "recording_active": manager.recording_state.active,
            "recording_slot": int(manager.recording_state.active_slot),
            "macro_exec_timeout_max_ms": int(policy.macro_exec_timeout_max_ms),
            "emergency_cancel_combo_enabled": bool(policy.emergency_cancel_combo_enabled),
            **recording_unlock.serialize_recording_unlock_state(
                manager,
                unlock_status,
                refresh_owner=recording_unlock.is_active_refresh_owner_request(
                    manager,
                    peer,
                    writer,
                    unlock_status,
                ),
            ),
            **recording_unlock.serialize_macro_recording_state(macro_recording_status),
            "mpris": manager.mpris_controller.status_snapshot(),
            "active_profiles": profile_payload["active_profiles"],
            "devices": profile_payload["devices"],
            "window": manager.compositor_state.current_window,
        }
        return status_payload

    return None
