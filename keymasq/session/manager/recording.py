import asyncio
import logging
import re
import secrets
import tomllib
from datetime import datetime
from time import monotonic
from typing import TYPE_CHECKING, cast

import tomli_w

from keymasq.common.devices import normalize_input_classes
from keymasq.common.ipc import Command, CommandType
from keymasq.common.models import normalize_macro_loop_stop_behavior
from keymasq.common.recording_guard import resolve_unlock_status
from keymasq.common.security import PeerCredentials, SecurityPolicy
from keymasq.session.profiles import ResolvedDeviceProfile

from . import profiles as runtime_profiles
from .common import (
    JsonObject,
    int_value,
    json_list,
    json_object,
    str_value,
)

if TYPE_CHECKING:
    from .core import SessionManager

log = logging.getLogger("keymasq-session")
MACRO_SAVE_PENDING_ERROR_CODE = "macro_save_pending"
MACRO_SAVE_PENDING_MESSAGE = (
    "Save or discard the current recording before starting another recording."
)
MACRO_SAVE_PENDING_NOTIFICATION_COOLDOWN_S = 5.0


def is_sensitive_session_command(
    manager: "SessionManager",
    command: str,
    policy: SecurityPolicy | None = None,
) -> bool:
    if policy is None:
        policy = manager.security_policy

    if command == "lock_recording_unlock":
        return True

    if policy.recording_unlock_required and command in {
        "start_recording",
        "begin_capture",
        "capture_read",
        "end_capture",
        "capture_combo",
        "start_device_inspector",
        "enable_device_inspector_suppression",
    }:
        return True

    if policy.macro_edit_requires_unlock and command in {
        "get_macro",
        "create_macro",
        "update_macro",
    }:
        return True

    return False


def has_active_gui_recording_owner(manager: "SessionManager") -> bool:
    owner = manager.unlock_state.refresh_owner
    if owner is None:
        return False
    return bool(str(owner.get("lease_id", "") or "").strip())


def has_pending_macro_save(manager: "SessionManager") -> bool:
    state = manager.recording_state
    return state.pending_data is not None and bool(state.pending_save_token)


def macro_save_pending_response(manager: "SessionManager") -> JsonObject:
    state = manager.recording_state
    response: JsonObject = {
        "status": "error",
        "error_code": MACRO_SAVE_PENDING_ERROR_CODE,
        "message": MACRO_SAVE_PENDING_MESSAGE,
    }
    if state.pending_save_token:
        response["pending_save_token"] = state.pending_save_token
    return response


def notify_macro_save_pending(manager: "SessionManager") -> None:
    token = manager.recording_state.pending_save_token or ""
    now = monotonic()
    if (
        token
        and manager.recording_state.pending_save_notification_token == token
        and now - manager.recording_state.pending_save_notification_at
        < MACRO_SAVE_PENDING_NOTIFICATION_COOLDOWN_S
    ):
        return

    manager.recording_state.pending_save_notification_token = token
    manager.recording_state.pending_save_notification_at = now
    manager.send_notification(
        "Keymasq: Macro Save Pending",
        MACRO_SAVE_PENDING_MESSAGE,
    )


def _set_active_recording_owner(
    manager: "SessionManager",
    *,
    peer: PeerCredentials | None = None,
    writer: asyncio.StreamWriter | None = None,
) -> None:
    state = manager.recording_state
    if peer is not None and writer is not None:
        state.active_owner_writer_id = id(writer)
        state.active_owner_pid = int(peer.pid)
        state.active_owner_uid = int(peer.uid)
        return

    owner = manager.unlock_state.refresh_owner
    if owner is not None:
        state.active_owner_writer_id = int_value(owner.get("writer_id"), 0) or None
        state.active_owner_pid = int_value(owner.get("pid"), 0) or None
        state.active_owner_uid = int_value(owner.get("uid"), 0) or None
        return

    state.active_owner_writer_id = None
    state.active_owner_pid = None
    state.active_owner_uid = None


def _clear_active_recording_owner(manager: "SessionManager") -> None:
    state = manager.recording_state
    state.active_owner_writer_id = None
    state.active_owner_pid = None
    state.active_owner_uid = None


def clear_active_recording_owner_if_writer(
    manager: "SessionManager",
    writer: asyncio.StreamWriter,
) -> None:
    state = manager.recording_state
    if not state.active:
        return
    if state.active_owner_writer_id == id(writer):
        _clear_active_recording_owner(manager)


def begin_pending_macro_save(manager: "SessionManager", recording_data: JsonObject) -> str:
    state = manager.recording_state
    token = secrets.token_urlsafe(16)
    state.pending_data = recording_data
    state.pending_save_token = token
    # Owner fields are lifecycle affinity for cleanup, not a same-UID auth boundary.
    state.pending_save_owner_writer_id = state.active_owner_writer_id
    state.pending_save_owner_pid = state.active_owner_pid
    state.pending_save_owner_uid = state.active_owner_uid
    state.pending_save_created_at = monotonic()
    state.pending_save_notification_token = None
    state.pending_save_notification_at = 0.0
    _clear_active_recording_owner(manager)
    return token


def clear_pending_macro_save(manager: "SessionManager") -> None:
    state = manager.recording_state
    state.pending_data = None
    state.pending_save_token = None
    state.pending_save_owner_writer_id = None
    state.pending_save_owner_pid = None
    state.pending_save_owner_uid = None
    state.pending_save_created_at = 0.0
    state.pending_save_notification_token = None
    state.pending_save_notification_at = 0.0


def clear_pending_macro_save_if_writer(
    manager: "SessionManager",
    writer: asyncio.StreamWriter,
) -> None:
    state = manager.recording_state
    if not has_pending_macro_save(manager):
        return
    owner_writer_id = state.pending_save_owner_writer_id
    if owner_writer_id == id(writer):
        clear_pending_macro_save(manager)


async def discard_pending_macro_save_if_writer(
    manager: "SessionManager",
    writer: asyncio.StreamWriter,
) -> None:
    state = manager.recording_state
    if not has_pending_macro_save(manager):
        return
    owner_writer_id = state.pending_save_owner_writer_id
    if owner_writer_id != id(writer):
        return

    pending_data = state.pending_data or {}
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
    clear_pending_macro_save(manager)


def pending_macro_save_token_matches(
    manager: "SessionManager",
    token: str,
) -> bool:
    current = manager.recording_state.pending_save_token
    return bool(current) and token == current


async def resolve_unlock_status_async(
    _manager: "SessionManager",
    uid: int,
) -> dict[str, bool | int | str]:
    return await asyncio.to_thread(resolve_unlock_status, uid)


def serialize_recording_unlock_state(
    manager: "SessionManager",
    unlock_status: dict[str, bool | int | str],
    *,
    refresh_owner: bool,
) -> dict[str, bool | int | str]:
    unlock_required = bool(manager.security_policy.recording_unlock_required)
    raw_unlocked = bool(unlock_status.get("unlocked", False))
    return {
        "recording_unlock_required": unlock_required,
        "recording_unlocked": raw_unlocked,
        "recording_unlock_source": str(unlock_status.get("source", "none") or "none"),
        "recording_unlock_expires_at": int(unlock_status.get("expires_at", 0) or 0),
        "recording_refresh_owner": bool(refresh_owner),
    }


def is_refresh_owner_request(
    manager: "SessionManager",
    peer: PeerCredentials,
    writer: asyncio.StreamWriter,
) -> bool:
    owner = manager.unlock_state.refresh_owner
    if owner is None:
        return False
    return (
        owner.get("uid") == int(peer.uid)
        and owner.get("pid") == int(peer.pid)
        and owner.get("writer_id") == id(writer)
    )


def _has_other_session_client_for_uid(
    manager: "SessionManager",
    uid: int,
    *,
    excluding: asyncio.StreamWriter | None = None,
) -> bool:
    for current_writer, peer in manager.session_client_peers.items():
        if current_writer is excluding:
            continue
        if int(peer.uid) == int(uid):
            return True
    return False


async def _cleanup_runtime_unlock_for_uid(
    manager: "SessionManager",
    uid: int,
    *,
    reason: str,
) -> None:
    try:
        result = await manager.client.send_command(
            Command(
                command=CommandType.LOCK_RECORDING_UNLOCK,
                data={"uid": int(uid), "cleanup": True},
            )
        )
        if result.status == "ok":
            log.info("Runtime unlock cleaned up uid=%s reason=%s", uid, reason)
        else:
            log.debug(
                "Runtime unlock cleanup failed uid=%s reason=%s error=%s",
                uid,
                reason,
                result.error,
            )
    except Exception as e:
        log.debug(
            "Runtime unlock cleanup failed uid=%s reason=%s error=%s",
            uid,
            reason,
            e,
        )


async def clear_recording_refresh_owner_if_writer(
    manager: "SessionManager",
    peer: PeerCredentials,
    writer: asyncio.StreamWriter,
) -> None:
    owner = manager.unlock_state.refresh_owner
    uid = int(peer.uid)

    if owner is not None and owner.get("writer_id") == id(writer):
        manager.unlock_state.refresh_owner = None
        manager.unlock_state.runtime_refresh_claim_consumed_until.pop(uid, None)
        await _cleanup_runtime_unlock_for_uid(manager, uid, reason="refresh_owner_disconnect")
        return

    if owner is not None and owner.get("uid") == uid:
        return

    if _has_other_session_client_for_uid(manager, uid, excluding=writer):
        return

    unlock_status = await resolve_unlock_status_async(manager, uid)
    if not bool(unlock_status.get("unlocked", False)):
        return

    if str(unlock_status.get("source", "none") or "none") != "runtime":
        return

    await _cleanup_runtime_unlock_for_uid(manager, uid, reason="last_client_disconnect")


async def claim_recording_unlock_refresh(
    manager: "SessionManager",
    peer: PeerCredentials,
    writer: asyncio.StreamWriter,
) -> JsonObject:
    unlock_status = await resolve_unlock_status_async(manager, peer.uid)
    if not bool(unlock_status.get("unlocked", False)):
        return {
            "status": "error",
            "error_code": "recording_locked",
            "message": "recording_locked: unlock required before claiming refresh",
        }

    source = str(unlock_status.get("source", "none") or "none")
    expires_at = int(unlock_status.get("expires_at", 0) or 0)

    if source == "runtime":
        consumed_until = int(
            manager.unlock_state.runtime_refresh_claim_consumed_until.get(int(peer.uid), 0) or 0
        )
        if expires_at <= consumed_until:
            return {
                "status": "error",
                "error_code": "recording_refresh_reclaim_denied",
                "message": (
                    "recording_refresh_denied: runtime lease already claimed; "
                    "unlock again to re-establish owner"
                ),
            }

    lease_id = secrets.token_urlsafe(24)
    manager.unlock_state.refresh_owner = {
        "uid": int(peer.uid),
        "pid": int(peer.pid),
        "writer_id": id(writer),
        "lease_id": lease_id,
    }
    if source == "runtime":
        manager.unlock_state.runtime_refresh_claim_consumed_until[int(peer.uid)] = expires_at

    if source == "runtime":
        try:
            refresh_result = await manager.client.send_command(
                Command(
                    command=CommandType.REFRESH_RECORDING_UNLOCK,
                    data={
                        "uid": int(peer.uid),
                        "ttl": int(manager.unlock_state.refresh_ttl_s),
                    },
                )
            )
        except Exception:
            manager.unlock_state.refresh_owner = None
            return {"status": "error", "message": "Daemon unavailable"}

        if refresh_result.status != "ok":
            manager.unlock_state.refresh_owner = None
            return {
                "status": "error",
                "error_code": "recording_refresh_denied",
                "message": refresh_result.error or "Failed to establish recording refresh lease",
            }

    unlock_status = await resolve_unlock_status_async(manager, peer.uid)
    return {
        "status": "ok",
        "lease_id": lease_id,
        **serialize_recording_unlock_state(
            manager,
            {
                **unlock_status,
                "source": source,
                "expires_at": int(unlock_status.get("expires_at", expires_at) or expires_at),
            },
            refresh_owner=True,
        ),
    }


async def refresh_recording_unlock(
    manager: "SessionManager",
    peer: PeerCredentials,
    writer: asyncio.StreamWriter,
    lease_id: str,
) -> JsonObject:
    if not lease_id:
        return {
            "status": "error",
            "error_code": "recording_refresh_denied",
            "message": "recording_refresh_denied: missing lease id",
        }

    owner = manager.unlock_state.refresh_owner
    if owner is None:
        return {
            "status": "error",
            "error_code": "recording_refresh_denied",
            "message": "recording_refresh_denied: no active refresh owner",
        }

    if (
        owner.get("uid") != int(peer.uid)
        or owner.get("pid") != int(peer.pid)
        or owner.get("writer_id") != id(writer)
        or owner.get("lease_id") != lease_id
    ):
        return {
            "status": "error",
            "error_code": "recording_refresh_owner_mismatch",
            "message": "recording_refresh_denied: caller is not active refresh owner",
        }

    try:
        result = await manager.client.send_command(
            Command(
                command=CommandType.REFRESH_RECORDING_UNLOCK,
                data={
                    "uid": int(peer.uid),
                    "ttl": int(manager.unlock_state.refresh_ttl_s),
                },
            )
        )
    except Exception:
        return {"status": "error", "message": "Daemon unavailable"}

    if result.status != "ok":
        return {
            "status": "error",
            "error_code": "recording_refresh_denied",
            "message": result.error or "Failed to refresh recording unlock",
        }

    unlock_status = await resolve_unlock_status_async(manager, peer.uid)
    if str(unlock_status.get("source", "none") or "none") == "runtime":
        expires_at = int(unlock_status.get("expires_at", 0) or 0)
        consumed_until = int(
            manager.unlock_state.runtime_refresh_claim_consumed_until.get(int(peer.uid), 0) or 0
        )
        if expires_at > consumed_until:
            manager.unlock_state.runtime_refresh_claim_consumed_until[int(peer.uid)] = expires_at
    if not bool(unlock_status.get("unlocked", False)):
        manager.unlock_state.refresh_owner = None

    return {
        "status": "ok",
        **serialize_recording_unlock_state(
            manager,
            unlock_status,
            refresh_owner=is_refresh_owner_request(manager, peer, writer),
        ),
    }


async def lock_recording_unlock(
    manager: "SessionManager",
    peer: PeerCredentials,
    writer: asyncio.StreamWriter,
    lease_id: str,
) -> JsonObject:
    if not lease_id:
        return {
            "status": "error",
            "error_code": "recording_lock_denied",
            "message": "recording_lock_denied: missing lease id",
        }

    owner = manager.unlock_state.refresh_owner
    if owner is None:
        return {
            "status": "error",
            "error_code": "recording_lock_denied",
            "message": "recording_lock_denied: no active refresh owner",
        }

    if (
        owner.get("uid") != int(peer.uid)
        or owner.get("pid") != int(peer.pid)
        or owner.get("writer_id") != id(writer)
        or owner.get("lease_id") != lease_id
    ):
        return {
            "status": "error",
            "error_code": "recording_lock_owner_mismatch",
            "message": "recording_lock_denied: caller is not active refresh owner",
        }

    try:
        result = await manager.client.send_command(
            Command(
                command=CommandType.LOCK_RECORDING_UNLOCK,
                data={"uid": int(peer.uid)},
            )
        )
    except Exception:
        return {"status": "error", "message": "Daemon unavailable"}

    if result.status != "ok":
        return {
            "status": "error",
            "error_code": "recording_lock_denied",
            "message": result.error or "Failed to lock recording unlock",
        }

    manager.unlock_state.refresh_owner = None
    manager.unlock_state.runtime_refresh_claim_consumed_until.pop(int(peer.uid), None)
    return {
        "status": "ok",
        **serialize_recording_unlock_state(
            manager,
            {"unlocked": False, "source": "none", "expires_at": 0},
            refresh_owner=False,
        ),
    }


async def _begin_capture(
    manager: "SessionManager",
    hardware_id: str,
) -> JsonObject:
    manager.capture_state.locks.add(hardware_id)

    current_profiles = list(
        manager.profile_state.resolved_devices.get(
            hardware_id,
            ResolvedDeviceProfile(hardware_id),
        ).active_profile_names
    )
    manager.capture_state.resume_profiles[hardware_id] = current_profiles

    released = False
    if hardware_id in manager.profile_state.grabbed_devices:
        await runtime_profiles.deactivate_profile(manager, hardware_id, immediate=True)
        released = True

    return {
        "status": "ok",
        "hardware_id": hardware_id,
        "released": released,
        "profiles": current_profiles,
    }


async def capture_begin(manager: "SessionManager", hardware_id: str) -> JsonObject:
    return await capture_begin_for_paths(manager, hardware_id, [])


async def capture_begin_for_paths(
    manager: "SessionManager",
    hardware_id: str,
    evdev_paths: list[str],
    *,
    evdev_interfaces: list[JsonObject] | None = None,
    mode: str = "button",
) -> JsonObject:
    explicit_evdev_paths = bool(evdev_paths)
    if not explicit_evdev_paths:
        evdev_paths = _hardware_evdev_paths(manager, hardware_id)
    if evdev_interfaces is None and explicit_evdev_paths:
        evdev_interfaces = _evdev_interfaces_for_paths(manager, hardware_id, evdev_paths)
    elif evdev_interfaces is None:
        evdev_interfaces = _hardware_evdev_interfaces(manager, hardware_id)
    if not evdev_paths and _requires_explicit_evdev_paths(hardware_id):
        return {
            "status": "error",
            "message": f"Hardware config for {hardware_id} has no evdev paths",
        }
    lock_result = await _begin_capture(manager, hardware_id)
    try:
        result = await manager.client.send_command(
            Command(
                command=CommandType.CAPTURE_BEGIN,
                data={
                    "hardware_id": hardware_id,
                    **({"mode": mode} if mode != "button" else {}),
                    **({"evdev_paths": evdev_paths} if evdev_paths else {}),
                    **(
                        {"evdev_interfaces": evdev_interfaces}
                        if evdev_interfaces
                        else {}
                    ),
                },
            )
        )
    except Exception:
        await _end_capture(manager, hardware_id)
        return {"status": "error", "message": "Daemon unavailable"}

    result_data = json_object(result.data)
    if result.status != "ok" or result_data is None:
        await _end_capture(manager, hardware_id)
        return {"status": "error", "message": result.error or "Failed to begin capture"}

    token = str_value(result_data.get("token"), "")
    if not token:
        await _end_capture(manager, hardware_id)
        return {"status": "error", "message": "Missing capture token"}

    manager.capture_state.tokens[hardware_id] = token
    response = {
        "status": "ok",
        "hardware_id": hardware_id,
        "token": token,
        "warnings": result_data.get("warnings", []),
    }
    response.update(lock_result)
    return response


async def capture_read(manager: "SessionManager", hardware_id: str) -> JsonObject:
    token = manager.capture_state.tokens.get(hardware_id, "")
    if not token:
        return {"status": "error", "message": "capture not active"}

    try:
        result = await manager.client.send_command(
            Command(command=CommandType.CAPTURE_READ, data={"token": token})
        )
    except Exception:
        return {"status": "error", "message": "Daemon unavailable"}

    result_data = json_object(result.data)
    if result.status == "ok" and result_data is not None:
        return {"status": "ok", "captured": result_data.get("captured")}
    return {"status": "error", "message": result.error or "Failed to read capture"}


async def capture_end(manager: "SessionManager", hardware_id: str) -> JsonObject:
    token = manager.capture_state.tokens.pop(hardware_id, "")
    if token:
        try:
            await manager.client.send_command(
                Command(command=CommandType.CAPTURE_END, data={"token": token})
            )
        except Exception:
            pass
    return await _end_capture(manager, hardware_id)


async def _end_capture(manager: "SessionManager", hardware_id: str) -> JsonObject:
    was_locked = hardware_id in manager.capture_state.locks
    manager.capture_state.locks.discard(hardware_id)

    previous_profile_names = manager.capture_state.resume_profiles.pop(hardware_id, [])
    if not was_locked:
        return {"status": "ok", "hardware_id": hardware_id, "resumed": False}

    await runtime_profiles.reevaluate_profiles(manager, reason=f"capture ended for {hardware_id}")
    active_names = list(
        manager.profile_state.resolved_devices.get(
            hardware_id,
            ResolvedDeviceProfile(hardware_id),
        ).active_profile_names
    )
    return {
        "status": "ok",
        "hardware_id": hardware_id,
        "resumed": bool(active_names),
        "profiles": active_names or previous_profile_names,
    }


async def capture_combo(
    manager: "SessionManager",
    profile_name: str,
    timeout_s: float,
) -> JsonObject:
    profile = manager.profiles.get_profile(profile_name)
    if profile is None:
        return {"status": "error", "message": f"Unknown profile '{profile_name}'"}

    hardware_ids = sorted(
        {
            *manager.hardware.list_hardware_ids(),
            *profile.config.device_layers.keys(),
            *(
                event.hardware_id
                for combo in getattr(profile.config, "combos", [])
                for step in combo.steps
                for event in step.events
                if event.hardware_id
            ),
        }
    )
    if not hardware_ids:
        return {
            "status": "error",
            "message": "No known devices available for combo capture",
        }

    try:
        hardware_paths = {
            hardware_id: paths
            for hardware_id in hardware_ids
            if (paths := _hardware_evdev_paths(manager, hardware_id))
        }
        hardware_interfaces = {
            hardware_id: interfaces
            for hardware_id in hardware_ids
            if (interfaces := _hardware_evdev_interfaces(manager, hardware_id))
        }
        result = await manager.client.send_command(
            Command(
                command=CommandType.CAPTURE_COMBO,
                data={
                    "hardware_ids": hardware_ids,
                    "hardware_paths": hardware_paths,
                    "hardware_interfaces": hardware_interfaces,
                    "timeout_s": float(timeout_s),
                },
            )
        )
    except Exception:
        return {"status": "error", "message": "Daemon unavailable"}

    result_data = json_object(result.data)
    if result.status != "ok" or result_data is None:
        return {"status": "error", "message": result.error or "Combo capture failed"}

    events_raw = result_data.get("events")
    if not isinstance(events_raw, list):
        return {"status": "error", "message": "Combo capture returned no events"}
    event_items = cast(list[object], events_raw)

    events: list[JsonObject] = []
    for raw_event in event_items:
        event = json_object(raw_event)
        if event is None:
            continue
        events.append(
            {
                "evdev": str_value(event.get("evdev"), ""),
                "hardware_id": str_value(event.get("hardware_id"), ""),
                "source": str_value(event.get("source"), ""),
            }
        )

    return {
        "status": "ok",
        "events": events,
        "warnings": json_list(result_data.get("warnings")),
    }


def _hardware_evdev_paths(manager: "SessionManager", hardware_id: str) -> list[str]:
    hardware = manager.hardware.get_hardware(hardware_id)
    if hardware is None:
        return []
    return [
        path
        for device in getattr(hardware, "evdev_devices", [])
        if (path := str_value(getattr(device, "path", ""), ""))
    ]


def _hardware_evdev_interfaces(
    manager: "SessionManager", hardware_id: str
) -> list[JsonObject]:
    hardware = manager.hardware.get_hardware(hardware_id)
    if hardware is None:
        return []
    interfaces: list[JsonObject] = []
    for device in getattr(hardware, "evdev_devices", []):
        device_id = str_value(getattr(device, "id", ""), "")
        if not device_id:
            continue
        path = str_value(getattr(device, "path", ""), "")
        if not path:
            continue
        interfaces.append(
            {
                "id": device_id,
                "path": path,
                "type": getattr(getattr(device, "device_type", None), "value", "other"),
                "phys": str_value(getattr(device, "phys", ""), ""),
                "capabilities": list(getattr(device, "capabilities", []) or []),
            }
        )
    return interfaces


def _evdev_interfaces_for_paths(
    manager: "SessionManager",
    hardware_id: str,
    evdev_paths: list[str],
) -> list[JsonObject]:
    configured_by_path: dict[str, list[JsonObject]] = {}
    for interface in _hardware_evdev_interfaces(manager, hardware_id):
        path = str_value(interface.get("path"), "")
        if not path:
            continue
        configured_by_path.setdefault(path, []).append(interface)

    interfaces: list[JsonObject] = []
    for path in evdev_paths:
        normalized_path = str_value(path, "")
        if not normalized_path:
            continue
        configured = configured_by_path.get(normalized_path, [])
        if configured:
            interfaces.append(configured.pop(0))
    return interfaces


def _requires_explicit_evdev_paths(hardware_id: str) -> bool:
    return "@" in str(hardware_id or "")


async def stop_recording(
    manager: "SessionManager",
    *,
    error_if_idle: bool,
) -> JsonObject:
    if not manager.recording_state.active:
        if error_if_idle:
            return {"status": "error", "message": "No recording in progress"}
        return {"status": "ok"}
    try:
        result = await manager.client.send_command(Command(command=CommandType.STOP_RECORDING))
    except Exception:
        return {"status": "error", "message": "Daemon unavailable"}

    if result.status == "ok":
        result_data = json_object(result.data)
        if result_data is not None:
            recording_data = dict(result_data)
            if manager.recording_state.start_cursor:
                recording_data["start_x"] = int(manager.recording_state.start_cursor[0])
                recording_data["start_y"] = int(manager.recording_state.start_cursor[1])
                recording_data["move_to_start"] = True
            if has_pending_macro_save(manager):
                manager.recording_state.pending_data = recording_data
                pending_save_token = str(manager.recording_state.pending_save_token or "")
            else:
                pending_save_token = begin_pending_macro_save(manager, recording_data)
            if pending_save_token:
                recording_data["pending_save_token"] = pending_save_token
            manager.recording_state.active = False
            manager.recording_state.start_cursor = None
            return {"status": "ok", **recording_data}
        manager.recording_state.active = False
        manager.recording_state.start_cursor = None
        _clear_active_recording_owner(manager)
        return {"status": "ok"}
    return {"status": "error", "message": result.error or "Failed to stop recording"}


async def play_macro_by_name(manager: "SessionManager", name: str) -> JsonObject:
    return await play_macro_trigger(manager, {"macro_name": name})


async def play_macro_trigger(manager: "SessionManager", data: JsonObject) -> JsonObject:
    try:
        macro_name = str(data.get("macro_name", data.get("name", "")) or "").strip()
        macro_events = json_list(data.get("macro_events"))

        macro: JsonObject | None = None
        if macro_events:
            macro = sanitize_macro_for_policy(
                manager,
                {"events": macro_events},
            )
            macro_events = json_list(macro.get("events"))

        if not macro_name and not macro_events:
            return {"status": "ok"}

        macro_speed_raw = data.get("macro_speed", data.get("speed"))
        payload = {
            "macro_name": macro_name,
            "macro_events": macro_events,
            "replay_mouse_movement": bool(
                data.get("macro_replay_mouse_movement", data.get("replay_mouse_movement", True))
            ),
            "replay_mouse_clicks": bool(
                data.get("macro_replay_mouse_clicks", data.get("replay_mouse_clicks", True))
            ),
            "speed": 1.0
            if macro_speed_raw is None
            else float(cast(int | float | str | bytes, macro_speed_raw)),
            "loop_mode": str(
                data.get(
                    "macro_loop_mode",
                    data.get("loop_mode", (macro or {}).get("loop_mode", "none")),
                )
                or "none"
            ),
            "loop_count": int_value(
                data.get(
                    "macro_loop_count",
                    data.get("loop_count", (macro or {}).get("loop_count")),
                ),
                1,
            ),
            "loop_stop_behavior": normalize_macro_loop_stop_behavior(
                data.get(
                    "macro_loop_stop_behavior",
                    data.get(
                        "loop_stop_behavior",
                        (macro or {}).get("loop_stop_behavior"),
                    ),
                )
            ),
            "move_to_start": bool(
                data.get(
                    "macro_move_to_start",
                    data.get("move_to_start", (macro or {}).get("move_to_start", False)),
                )
            ),
            "start_x": int_value(
                data.get("macro_start_x", data.get("start_x", (macro or {}).get("start_x"))),
                0,
            ),
            "start_y": int_value(
                data.get("macro_start_y", data.get("start_y", (macro or {}).get("start_y"))),
                0,
            ),
            "block_mouse_movement": bool(
                data.get(
                    "macro_block_mouse_movement",
                    data.get(
                        "block_mouse_movement",
                        (macro or {}).get("block_mouse_movement", False),
                    ),
                )
            ),
            "source_device": str(data.get("source_device", "") or ""),
            "source_button": str(data.get("source_button", "") or ""),
            "trigger_value": int_value(data.get("trigger_value"), 1),
        }

        result = await manager.client.send_command(
            Command(command=CommandType.PLAY_MACRO, data=payload)
        )
        if result.status == "ok":
            result_data = json_object(result.data)
            return result_data if result_data is not None else {"status": "ok"}

        message = result.error or "playback failed"
        log.warning("Macro trigger playback failed for %r: %s", macro_name, message)
        return {"status": "error", "message": message}
    except Exception as exc:
        log.exception(
            "Failed to play macro trigger macro=%r source_device=%r source_button=%r",
            data.get("macro_name", data.get("name", "")),
            data.get("source_device", ""),
            data.get("source_button", ""),
        )
        return {"status": "error", "message": f"Failed to play macro trigger: {exc}"}


def sanitize_macro_for_policy(manager: "SessionManager", macro: JsonObject) -> JsonObject:
    cloned = dict(macro)
    events = json_list(cloned.get("events"))
    if not events:
        return cloned

    max_timeout = max(1, int(manager.security_policy.macro_exec_timeout_max_ms))
    sanitized: list[JsonObject] = []
    for ev in events:
        event_data = json_object(ev)
        if event_data is None:
            continue
        item = dict(event_data)
        action = str_value(item.get("macro_action"), "").lower()
        if action == "exec_sync":
            timeout_ms = int_value(item.get("timeout_ms"), max_timeout)
            item["timeout_ms"] = max(1, min(timeout_ms, max_timeout))
        sanitized.append(item)
    cloned["events"] = sanitized
    return cloned


def update_recording_settings(manager: "SessionManager", request: JsonObject) -> None:
    settings = manager.recording_state.settings
    if "include_mouse_movement" in request:
        settings["include_mouse_movement"] = bool(request.get("include_mouse_movement"))
    if "include_mouse_clicks" in request:
        settings["include_mouse_clicks"] = bool(request.get("include_mouse_clicks"))
    if "record_start_position" in request:
        settings["record_start_position"] = bool(request.get("record_start_position"))
    if "record_keyboard" in request:
        settings["record_keyboard"] = bool(request.get("record_keyboard"))
    if "record_mouse" in request:
        settings["record_mouse"] = bool(request.get("record_mouse"))
    if "record_gamepad" in request:
        settings["record_gamepad"] = bool(request.get("record_gamepad"))
    if "device_overrides" in request:
        overrides = json_object(request.get("device_overrides"))
        if overrides is not None:
            settings["device_overrides"] = {
                str(recording_id): bool(enabled) for recording_id, enabled in overrides.items()
            }
    update_selected_recording_devices_cache(manager)
    queue_recording_settings_save(manager, dict(settings))


def queue_recording_settings_save(
    manager: "SessionManager",
    settings: JsonObject,
) -> None:
    manager.recording_state.settings_pending_save = settings
    save_task = cast(asyncio.Task[None] | None, manager.recording_state.settings_save_task)
    if save_task is not None and not save_task.done():
        return
    manager.recording_state.settings_save_task = asyncio.create_task(
        flush_recording_settings_saves(manager)
    )


async def flush_recording_settings_saves(manager: "SessionManager") -> None:
    try:
        while manager.recording_state.settings_pending_save is not None:
            pending = manager.recording_state.settings_pending_save
            manager.recording_state.settings_pending_save = None
            await asyncio.to_thread(save_recording_settings_to_disk, manager, pending)
    finally:
        manager.recording_state.settings_save_task = None


def load_recording_settings_from_disk(manager: "SessionManager") -> None:
    try:
        if not manager.RECORDING_SETTINGS_PATH.exists():
            return
        with manager.RECORDING_SETTINGS_PATH.open("rb") as f:
            data = cast(JsonObject, tomllib.load(f))
        settings = manager.recording_state.settings
        settings["include_mouse_movement"] = bool(data.get("include_mouse_movement", False))
        settings["include_mouse_clicks"] = bool(data.get("include_mouse_clicks", False))
        settings["record_start_position"] = bool(data.get("record_start_position", False))
        settings["record_keyboard"] = bool(data.get("record_keyboard", True))
        settings["record_mouse"] = bool(data.get("record_mouse", False))
        settings["record_gamepad"] = bool(data.get("record_gamepad", True))
        overrides = json_object(data.get("device_overrides"))
        if overrides is not None:
            settings["device_overrides"] = {
                str(recording_id): bool(enabled) for recording_id, enabled in overrides.items()
            }
    except Exception:
        log.exception(
            "Failed to load recording settings from %s",
            manager.RECORDING_SETTINGS_PATH,
        )


def save_recording_settings_to_disk(
    manager: "SessionManager",
    settings: JsonObject | None = None,
) -> None:
    settings = settings or manager.recording_state.settings
    try:
        data: JsonObject = {
            "include_mouse_movement": bool(settings.get("include_mouse_movement", False)),
            "include_mouse_clicks": bool(settings.get("include_mouse_clicks", False)),
            "record_start_position": bool(settings.get("record_start_position", False)),
            "record_keyboard": bool(settings.get("record_keyboard", True)),
            "record_mouse": bool(settings.get("record_mouse", False)),
            "record_gamepad": bool(settings.get("record_gamepad", True)),
            "device_overrides": {
                str(recording_id): bool(enabled)
                for recording_id, enabled in (
                    json_object(settings.get("device_overrides")) or {}
                ).items()
            },
        }

        manager.RECORDING_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with manager.RECORDING_SETTINGS_PATH.open("wb") as f:
            tomli_w.dump(data, f)
    except Exception:
        log.exception(
            "Failed to save recording settings to %s",
            manager.RECORDING_SETTINGS_PATH,
        )


async def start_recording(
    manager: "SessionManager",
    reset_if_active: bool = False,
    *,
    owner_peer: PeerCredentials | None = None,
    owner_writer: asyncio.StreamWriter | None = None,
) -> JsonObject:
    if has_pending_macro_save(manager):
        notify_macro_save_pending(manager)
        return macro_save_pending_response(manager)

    if manager.recording_state.active:
        if not reset_if_active:
            return {"status": "error", "message": "Recording already in progress"}
        try:
            result = await manager.client.send_command(Command(command=CommandType.STOP_RECORDING))
            result_data = json_object(result.data)
            if result.status == "ok" and result_data is not None:
                manager.recording_state.pending_data = result_data
        except Exception:
            pass
        manager.recording_state.active = False
        _clear_active_recording_owner(manager)

    settings = manager.recording_state.settings
    include_mouse_movement = settings.get("include_mouse_movement", False)
    include_mouse_clicks = settings.get("include_mouse_clicks", False)
    record_start_position = settings.get("record_start_position", False)
    device_types = ["keyboard", "gamepad", "mouse"]

    if not manager.recording_state.devices_cache_ready:
        log.debug("Recording start using empty/uninitialized recording device cache")
    devices = list(manager.recording_state.selected_devices_cache)
    recording_ids = list(
        dict.fromkeys(
            recording_id
            for d in devices
            if (recording_id := _recording_device_id(d))
        )
    )
    log.debug(
        "recording start device selection: types=%s overrides=%r recording_ids=%s devices=%s",
        device_types,
        json_object(settings.get("device_overrides")) or {},
        recording_ids,
        [str(d.get("path", "")) for d in devices],
    )

    start_x, start_y = 0, 0
    manager.recording_state.start_cursor = None
    if record_start_position:
        if manager.compositor_state.window_listener:
            try:
                pos = await manager.compositor_state.window_listener.get_cursor_position()
                if pos:
                    start_x, start_y = int(pos[0]), int(pos[1])
                    manager.recording_state.start_cursor = (start_x, start_y)
                    log.debug(
                        "Recording start cursor position captured: x=%s, y=%s",
                        start_x,
                        start_y,
                    )
                else:
                    log.debug("Recording start: get_cursor_position returned None")
            except Exception as e:
                log.debug("Failed to get cursor position for recording start: %s", e)
        else:
            log.debug("Recording start: no window listener available")
    else:
        log.debug("Recording start: record_start_position is disabled")

    try:
        result = await manager.client.send_command(
            Command(
                command=CommandType.START_RECORDING,
                data={
                    "devices": devices,
                    "include_mouse_movement": include_mouse_movement,
                    "include_mouse_clicks": include_mouse_clicks,
                    "start_x": start_x,
                    "start_y": start_y,
                },
            )
        )
    except Exception:
        return {"status": "error", "message": "Daemon unavailable"}

    if result.status == "ok":
        manager.recording_state.active = True
        _set_active_recording_owner(
            manager,
            peer=owner_peer,
            writer=owner_writer,
        )
        response_data = json_object(result.data)
        return response_data if response_data else {"status": "ok"}

    message = str(result.error or "Daemon unavailable")
    response: JsonObject = {"status": "error", "message": message}
    if "recording_locked" in message.lower():
        response["error_code"] = "recording_locked"
    return response


async def refresh_recording_devices_cache(manager: "SessionManager") -> None:
    try:
        devices = await get_devices_for_recording(
            manager,
            ["keyboard", "gamepad", "mouse"],
            include_grabbed=True,
        )
        manager.recording_state.devices_cache = devices
        manager.recording_state.devices_cache_ready = True
        update_selected_recording_devices_cache(manager)
    except Exception:
        pass


def update_selected_recording_devices_cache(manager: "SessionManager") -> None:
    overrides = json_object(manager.recording_state.settings.get("device_overrides")) or {}
    manager.recording_state.selected_devices_cache = [
        d
        for d in manager.recording_state.devices_cache
        if _recording_device_enabled(d, overrides)
    ]


def _recording_device_types(device: JsonObject) -> list[str]:
    return normalize_input_classes(
        cast(list[str] | None, json_list(device.get("device_types")) or None),
        str_value(device.get("device_type"), "other"),
    )

def _recording_device_id(device: JsonObject) -> str:
    recording_id = str_value(device.get("recording_id"), "")
    if recording_id:
        return recording_id
    stable_path = str_value(device.get("stable_path"), "")
    if stable_path:
        return f"physical:{stable_path}"
    path = str_value(device.get("path"), "")
    return f"physical:{path}" if path else ""


def _recording_device_selected_by_default(device: JsonObject) -> bool:
    return str_value(device.get("recording_kind"), "physical") in {
        "keymasq_output",
        "keymasq_passthrough",
    }


def _recording_device_enabled(
    device: JsonObject,
    overrides: JsonObject,
) -> bool:
    recording_id = _recording_device_id(device)
    if recording_id in overrides:
        return bool(overrides.get(recording_id))
    return _recording_device_selected_by_default(device)


async def get_devices_for_recording(
    manager: "SessionManager",
    device_types: list[str],
    include_grabbed: bool = False,
) -> list[JsonObject]:
    try:
        result = await manager.client.send_command(Command(command=CommandType.LIST_DEVICES))
    except Exception:
        return []
    result_data = json_object(result.data)
    if result.status != "ok" or result_data is None:
        return []

    devices: list[JsonObject] = []
    for raw_device in json_list(result_data.get("devices")):
        d = json_object(raw_device)
        if d is None:
            continue
        path = str_value(d.get("path"), "")
        stable_path = str_value(d.get("stable_path"), path)
        dtype = str_value(d.get("device_type"), "other")
        resolved_types = _recording_device_types(d)
        if not path or not set(device_types).intersection(resolved_types):
            continue

        is_grabbed = bool(d.get("grabbed_by_keymasq", False))
        if is_grabbed and not include_grabbed:
            continue

        devices.append(
            {
                "path": path,
                "open_path": str_value(d.get("open_path"), path),
                "stable_path": stable_path,
                "interface_id": str_value(d.get("interface_id"), ""),
                "name": str_value(d.get("name"), path),
                "phys": str_value(d.get("phys"), ""),
                "uniq": str_value(d.get("uniq"), ""),
                "vendor_id": str(d.get("vendor_id", "") or ""),
                "product_id": str(d.get("product_id", "") or ""),
                "device_type": dtype,
                "device_types": resolved_types,
                "recording_id": str_value(d.get("recording_id"), f"physical:{stable_path}"),
                "recording_kind": str_value(d.get("recording_kind"), "physical"),
                "grabbed_by_keymasq": is_grabbed,
                "source_hardware_id": str_value(d.get("source_hardware_id"), ""),
                "source_interface_id": str_value(d.get("source_interface_id"), ""),
                "source_stable_path": str_value(d.get("source_stable_path"), ""),
                "source_path": str_value(d.get("source_path"), ""),
                "keymasq_output": str_value(d.get("keymasq_output"), ""),
            }
        )

    return devices


async def save_recording(
    manager: "SessionManager",
    name: str,
    move_to_start: bool = False,
    start_x: int = 0,
    start_y: int = 0,
    block_mouse_movement: bool = False,
) -> JsonObject:
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", name).strip("._")
    if not safe_name:
        raise ValueError("Invalid macro name")

    data: JsonObject = manager.recording_state.pending_data or {}
    pending_recording_id = str_value(data.get("pending_recording_id"), "")
    if not pending_recording_id:
        return {"status": "error", "message": "No pending recording"}

    macro: JsonObject = {
        "name": safe_name,
        "created_at": datetime.now().isoformat(),
        "pending_recording_id": pending_recording_id,
        "move_to_start": bool(move_to_start),
        "start_x": int(start_x),
        "start_y": int(start_y),
        "block_mouse_movement": bool(block_mouse_movement),
    }
    try:
        result = await manager.client.send_command(
            Command(command=CommandType.MACRO_SAVE_RECORDING, data=macro)
        )
    except Exception:
        return {"status": "error", "message": "Daemon unavailable"}

    if result.status != "ok":
        return {"status": "error", "message": result.error or "Failed to save recording"}

    created_name = safe_name
    result_data = json_object(result.data)
    if result_data is not None:
        created = json_object(result_data.get("macro"))
        if created is not None:
            created_name = str(created.get("name", safe_name))

    clear_pending_macro_save(manager)
    manager.broadcast_to_session_clients({"event": "macro_saved", "name": created_name})
    return {"status": "ok", "name": created_name}


def is_recording_locked_error(result: JsonObject) -> bool:
    if result.get("error_code") == "recording_locked":
        return True

    message = str(result.get("message", "") or "").lower()
    return "recording_locked" in message


def notify_recording_unlock_required(
    manager: "SessionManager",
    result: JsonObject,
) -> None:
    if not is_recording_locked_error(result):
        return

    manager.send_notification(
        "Keymasq: Recording Locked",
        "Recording/capture requires unlock in Keymasq GUI.",
    )
