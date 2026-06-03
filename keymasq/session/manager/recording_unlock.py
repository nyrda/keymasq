import asyncio
import logging
import secrets
import sys
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast

from keymasq.common.ipc import Command, CommandType
from keymasq.common.recording_guard import (
    is_unlock_value_active,
)
from keymasq.common.recording_guard import (
    resolve_macro_recording_status as _resolve_macro_recording_status,
)
from keymasq.common.recording_guard import (
    resolve_unlock_status as _resolve_unlock_status,
)
from keymasq.common.security import PeerCredentials, SecurityPolicy

from .common import JsonObject, int_value

if TYPE_CHECKING:
    from .core import SessionManager

log = logging.getLogger("keymasq-session")
RecordingStatus = dict[str, bool | int | str]
resolve_unlock_status = _resolve_unlock_status
resolve_macro_recording_status = _resolve_macro_recording_status


def _facade_status_resolver(
    name: str,
    fallback: Callable[[int], RecordingStatus],
) -> Callable[[int], RecordingStatus]:
    facade = sys.modules.get("keymasq.session.manager.recording")
    resolver = getattr(facade, name, fallback) if facade is not None else fallback
    if callable(resolver):
        return cast(Callable[[int], RecordingStatus], resolver)
    return fallback


async def _call_facade_resolve_unlock_status_async(
    manager: "SessionManager",
    uid: int,
) -> RecordingStatus:
    facade = sys.modules.get("keymasq.session.manager.recording")
    resolver = (
        getattr(facade, "resolve_unlock_status_async", None)
        if facade is not None
        else None
    )
    if resolver is not None and resolver is not resolve_unlock_status_async:
        return await cast(
            Callable[["SessionManager", int], Awaitable[RecordingStatus]],
            resolver,
        )(manager, uid)
    return await _resolve_unlock_status_async_impl(manager, uid)


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
        "begin_capture",
        "capture_read",
        "end_capture",
        "capture_combo",
        "save_recording",
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


def _status_is_active(status: RecordingStatus | None) -> bool:
    if status is None or not bool(status.get("unlocked", False)):
        return False
    return is_unlock_value_active(int_value(status.get("expires_at"), 0))


def _cache_or_fallback_status(
    cache: dict[int, RecordingStatus],
    uid: int,
    status: RecordingStatus,
) -> RecordingStatus:
    uid = int(uid)
    if _status_is_active(status):
        cache[uid] = dict(status)
        return status

    if bool(status.get("unreadable", False)):
        cached = cache.get(uid)
        if cached is not None and _status_is_active(cached):
            return dict(cached)

    cache.pop(uid, None)
    return status


async def _resolve_unlock_status_async_impl(
    manager: "SessionManager",
    uid: int,
) -> RecordingStatus:
    if manager.connected:
        try:
            response = await manager.client.send_command(
                Command(CommandType.RECORDING_UNLOCK_STATUS, data={"uid": int(uid)}),
                timeout=3.0,
            )
        except Exception as exc:
            log.warning("Failed to query daemon recording unlock status: %s", exc)
        else:
            if response.status == "ok" and isinstance(response.data, dict):
                status = cast(RecordingStatus, response.data)
                return _cache_or_fallback_status(
                    manager.unlock_state.unlock_status_cache,
                    uid,
                    status,
                )
            log.warning(
                "Daemon recording unlock status query failed: status=%s error=%s",
                response.status,
                response.error,
            )

    resolver = _facade_status_resolver("resolve_unlock_status", resolve_unlock_status)
    status = await asyncio.to_thread(resolver, uid)
    return _cache_or_fallback_status(manager.unlock_state.unlock_status_cache, uid, status)


async def resolve_unlock_status_async(
    manager: "SessionManager",
    uid: int,
) -> RecordingStatus:
    return await _resolve_unlock_status_async_impl(manager, uid)


async def _resolve_macro_recording_status_async_impl(
    manager: "SessionManager",
    uid: int,
) -> RecordingStatus:
    if manager.connected:
        try:
            response = await manager.client.send_command(
                Command(CommandType.MACRO_RECORDING_STATUS, data={"uid": int(uid)}),
                timeout=3.0,
            )
        except Exception as exc:
            log.warning("Failed to query daemon macro recording status: %s", exc)
        else:
            if response.status == "ok" and isinstance(response.data, dict):
                status = cast(RecordingStatus, response.data)
                return _cache_or_fallback_status(
                    manager.unlock_state.macro_recording_status_cache,
                    uid,
                    status,
                )
            log.warning(
                "Daemon macro recording status query failed: status=%s error=%s",
                response.status,
                response.error,
            )

    resolver = _facade_status_resolver(
        "resolve_macro_recording_status",
        resolve_macro_recording_status,
    )
    status = await asyncio.to_thread(resolver, uid)
    return _cache_or_fallback_status(
        manager.unlock_state.macro_recording_status_cache,
        uid,
        status,
    )


async def resolve_macro_recording_status_async(
    manager: "SessionManager",
    uid: int,
) -> RecordingStatus:
    return await _resolve_macro_recording_status_async_impl(manager, uid)


def serialize_recording_unlock_state(
    manager: "SessionManager",
    unlock_status: RecordingStatus,
    *,
    refresh_owner: bool,
) -> RecordingStatus:
    unlock_required = bool(manager.security_policy.recording_unlock_required)
    raw_unlocked = bool(unlock_status.get("unlocked", False))
    return {
        "recording_unlock_required": unlock_required,
        "recording_unlocked": raw_unlocked,
        "recording_unlock_source": str(unlock_status.get("source", "none") or "none"),
        "recording_unlock_expires_at": int(unlock_status.get("expires_at", 0) or 0),
        "recording_refresh_owner": bool(refresh_owner),
    }


def serialize_macro_recording_state(
    macro_recording_status: RecordingStatus,
) -> RecordingStatus:
    enabled = bool(macro_recording_status.get("unlocked", False))
    return {
        "macro_recording_enabled": enabled,
        "macro_recording_source": str(
            macro_recording_status.get("source", "none") or "none"
        ),
        "macro_recording_expires_at": int(
            macro_recording_status.get("expires_at", 0) or 0
        ),
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


def _clear_refresh_owner_for_uid(manager: "SessionManager", uid: int) -> None:
    owner = manager.unlock_state.refresh_owner
    if owner is None or owner.get("uid") != int(uid):
        return
    manager.unlock_state.refresh_owner = None
    manager.unlock_state.runtime_refresh_claim_consumed_until.pop(int(uid), None)


def is_active_refresh_owner_request(
    manager: "SessionManager",
    peer: PeerCredentials,
    writer: asyncio.StreamWriter,
    unlock_status: RecordingStatus,
) -> bool:
    if not is_refresh_owner_request(manager, peer, writer):
        return False
    if bool(unlock_status.get("unlocked", False)):
        return True
    _clear_refresh_owner_for_uid(manager, int(peer.uid))
    return False


async def authorize_sensitive_session_command(
    manager: "SessionManager",
    command: str,
    peer: PeerCredentials,
    writer: asyncio.StreamWriter,
) -> bool:
    if not is_refresh_owner_request(manager, peer, writer):
        return False
    if command == "end_capture":
        return True
    unlock_status = await _call_facade_resolve_unlock_status_async(manager, peer.uid)
    return is_active_refresh_owner_request(manager, peer, writer, unlock_status)


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

    unlock_status = await _call_facade_resolve_unlock_status_async(manager, uid)
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
    unlock_status = await _call_facade_resolve_unlock_status_async(manager, peer.uid)
    if not bool(unlock_status.get("unlocked", False)):
        return {
            "status": "error",
            "error_code": "recording_locked",
            "message": "recording_locked: capture unlock required before claiming refresh",
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
                "message": refresh_result.error or "Failed to establish capture unlock lease",
            }

    unlock_status = await _call_facade_resolve_unlock_status_async(manager, peer.uid)
    refresh_owner = is_active_refresh_owner_request(manager, peer, writer, unlock_status)
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
            refresh_owner=refresh_owner,
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
        _clear_refresh_owner_for_uid(manager, int(peer.uid))
        return {
            "status": "error",
            "error_code": "recording_refresh_denied",
            "message": result.error or "Failed to refresh capture unlock",
        }

    unlock_status = await _call_facade_resolve_unlock_status_async(manager, peer.uid)
    if str(unlock_status.get("source", "none") or "none") == "runtime":
        expires_at = int(unlock_status.get("expires_at", 0) or 0)
        consumed_until = int(
            manager.unlock_state.runtime_refresh_claim_consumed_until.get(int(peer.uid), 0) or 0
        )
        if expires_at > consumed_until:
            manager.unlock_state.runtime_refresh_claim_consumed_until[int(peer.uid)] = expires_at
    return {
        "status": "ok",
        **serialize_recording_unlock_state(
            manager,
            unlock_status,
            refresh_owner=is_active_refresh_owner_request(
                manager,
                peer,
                writer,
                unlock_status,
            ),
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
            "message": result.error or "Failed to lock capture unlock",
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


def is_recording_locked_error(result: JsonObject) -> bool:
    if result.get("error_code") == "recording_locked":
        return True

    message = str(result.get("message", "") or "").lower()
    return "recording_locked" in message


def is_recording_unlock_required_error(result: JsonObject) -> bool:
    if is_recording_locked_error(result):
        return True
    return result.get("error_code") == "sensitive_command_denied"


def notify_recording_unlock_required(
    manager: "SessionManager",
    result: JsonObject,
) -> None:
    if not is_recording_unlock_required_error(result):
        return

    manager.send_notification(
        "Keymasq: Capture Unlock Required",
        "Capture unlock is required in Keymasq GUI.",
    )
