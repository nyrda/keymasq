import asyncio
from typing import TYPE_CHECKING

from keymasq.common.coercion import coerce_int, coerce_str
from keymasq.common.model.actions import (
    MAX_MACRO_RECORDING_SLOTS,
    normalize_macro_recording_slot,
)
from keymasq.common.security import PeerCredentials

from .. import recording_device_selection, recording_lifecycle
from ..common import JsonObject

if TYPE_CHECKING:
    from ..core import SessionManager


async def handle_recording_commands(
    manager: "SessionManager",
    command: str,
    request: JsonObject,
    peer: PeerCredentials,
    writer: asyncio.StreamWriter,
) -> JsonObject | None:
    if command == "start_recording":
        recording_slot = coerce_int(request.get("recording_slot"), 0)
        if not normalize_macro_recording_slot(recording_slot):
            return {
                "status": "error",
                "error_code": "macro_recording_slot_required",
                "message": (
                    "Macro recording requires an explicit slot from 1 to "
                    f"{MAX_MACRO_RECORDING_SLOTS}."
                ),
            }
        recording_device_selection.update_recording_settings(manager, request)
        return await recording_lifecycle.start_recording(
            manager,
            reset_if_active=False,
            recording_slot=recording_slot,
            owner_peer=peer,
            owner_writer=writer,
        )

    if command == "set_recording_settings":
        recording_device_selection.update_recording_settings(manager, request)
        return {"status": "ok", **manager.recording_state.settings}

    if command == "get_recording_settings":
        return {
            "status": "ok",
            "macro_recording_allowed": bool(manager.security_policy.macro_recording_allowed),
            **manager.recording_state.settings,
        }

    if command == "stop_recording":
        return await recording_lifecycle.stop_recording(
            manager,
            error_if_idle=True,
            recording_slot=coerce_int(request.get("recording_slot"), 0),
        )

    if command == "save_recording":
        name = coerce_str(request.get("name"), "").strip()
        if not name:
            return {"status": "error", "message": "Name required"}
        pending_save_token = coerce_str(request.get("pending_save_token"), "").strip()
        if pending_save_token and not recording_lifecycle.pending_macro_save_token_matches(
            manager,
            pending_save_token,
        ):
            return {
                "status": "error",
                "error_code": "stale_pending_macro_save",
                "message": "Pending recording has already changed.",
            }
        save_result = await recording_lifecycle.save_recording(
            manager,
            name,
            block_mouse_movement=bool(request.get("block_mouse_movement", False)),
            block_keyboard=bool(request.get("block_keyboard", False)),
            recording_slot=coerce_int(request.get("recording_slot"), 0),
            pending_save_token=pending_save_token,
        )
        if save_result.get("status") != "ok":
            return save_result
        return {"status": "ok", "name": save_result.get("name", name)}

    if command == "delete_recording_slot":
        pending_save_token = coerce_str(request.get("pending_save_token"), "").strip()
        if pending_save_token and not recording_lifecycle.pending_macro_save_token_matches(
            manager,
            pending_save_token,
        ):
            return {
                "status": "error",
                "error_code": "stale_pending_macro_save",
                "message": "Pending recording has already changed.",
            }
        deleted = await recording_lifecycle.delete_pending_macro_slot(
            manager,
            recording_slot=coerce_int(request.get("recording_slot"), 0),
            pending_save_token=pending_save_token,
        )
        if not deleted:
            return {"status": "error", "message": "No pending recording"}
        return {"status": "ok"}

    return None
