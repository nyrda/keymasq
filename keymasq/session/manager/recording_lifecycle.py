import asyncio
import logging
import re
import secrets
from datetime import datetime
from time import monotonic
from typing import TYPE_CHECKING, cast

from keymasq.common.coercion import coerce_bool, coerce_int, coerce_str
from keymasq.common.ipc import Command, CommandType
from keymasq.common.models import (
    DEFAULT_MACRO_LOOP_STOP_BEHAVIOR,
    MAX_MACRO_RECORDING_SLOTS,
    normalize_macro_loop_stop_behavior,
    normalize_macro_recording_slot,
)
from keymasq.common.security import PeerCredentials

from .common import (
    JsonObject,
    json_list,
    json_object,
)
from .recording_device_selection import recording_device_id
from .state import PendingSave, PendingSlot

if TYPE_CHECKING:
    from .core import SessionManager

log = logging.getLogger("keymasq-session")
MACRO_RECORDING_DISABLED_ERROR_CODE = "macro_recording_disabled"
MACRO_RECORDING_DISABLED_MESSAGE = (
    "Macro recording is disabled. Enable macro recording in Keymasq before using "
    "recording triggers."
)


def _monotonic() -> float:
    return float(monotonic())


def normalize_pending_macro_recording_slot(value: object, *, default: int = 1) -> int:
    slot = normalize_macro_recording_slot(value)
    if slot:
        return slot
    return default if 1 <= default <= MAX_MACRO_RECORDING_SLOTS else 1


def _sync_pending_macro_save(manager: "SessionManager") -> None:
    state = manager.recording_state
    if not state.pending_slots:
        state.pending_save = None
        return

    slot = max(
        state.pending_slots,
        key=lambda current_slot: state.pending_slots[current_slot].created_at,
    )
    pending_slot = state.pending_slots[slot]
    state.pending_save = PendingSave(
        data=pending_slot.data,
        token=pending_slot.token,
        owner_writer_id=pending_slot.owner_writer_id,
        owner_pid=pending_slot.owner_pid,
        owner_uid=pending_slot.owner_uid,
        created_at=pending_slot.created_at,
    )


def _ensure_pending_macro_save_slot(manager: "SessionManager") -> None:
    state = manager.recording_state
    pending_save = state.pending_save
    if state.pending_slots or pending_save is None or not pending_save.token:
        return

    slot = normalize_pending_macro_recording_slot(
        pending_save.data.get("recording_slot"),
        default=1,
    )
    data = dict(pending_save.data)
    data["recording_slot"] = slot
    data["pending_save_token"] = pending_save.token
    state.pending_slots[slot] = PendingSlot(
        data=data,
        token=pending_save.token,
        owner_writer_id=pending_save.owner_writer_id,
        owner_pid=pending_save.owner_pid,
        owner_uid=pending_save.owner_uid,
        created_at=pending_save.created_at or _monotonic(),
    )
    _sync_pending_macro_save(manager)


def pending_macro_save_slot_for_token(
    manager: "SessionManager",
    token: str,
) -> int:
    token = str(token or "").strip()
    if not token:
        return 0
    _ensure_pending_macro_save_slot(manager)
    for slot, pending_slot in manager.recording_state.pending_slots.items():
        if pending_slot.token == token:
            return slot
    return 0


def pending_macro_save_slot(
    manager: "SessionManager",
    *,
    recording_slot: int = 0,
    pending_save_token: str = "",
) -> int:
    _ensure_pending_macro_save_slot(manager)
    token = str(pending_save_token or "").strip()
    token_slot = pending_macro_save_slot_for_token(manager, pending_save_token)
    if token_slot:
        return token_slot
    if token:
        return 0

    slot = normalize_macro_recording_slot(recording_slot)
    if slot and slot in manager.recording_state.pending_slots:
        return slot

    if slot:
        return 0

    if len(manager.recording_state.pending_slots) == 1:
        return next(iter(manager.recording_state.pending_slots))
    return 0


def has_pending_macro_save(
    manager: "SessionManager",
    *,
    recording_slot: int = 0,
) -> bool:
    _ensure_pending_macro_save_slot(manager)
    state = manager.recording_state
    slot = normalize_macro_recording_slot(recording_slot)
    if slot:
        return bool(state.pending_slots.get(slot))
    return bool(state.pending_slots) or bool(state.pending_save)


def macro_recording_disabled_response() -> JsonObject:
    return {
        "status": "error",
        "error_code": MACRO_RECORDING_DISABLED_ERROR_CODE,
        "message": MACRO_RECORDING_DISABLED_MESSAGE,
    }


def is_macro_recording_disabled_error(result: JsonObject) -> bool:
    if result.get("error_code") == MACRO_RECORDING_DISABLED_ERROR_CODE:
        return True
    message = str(result.get("message", "") or "").lower()
    return "macro_recording_disabled" in message or "macro recording opt-in" in message


def notify_macro_recording_disabled(manager: "SessionManager") -> None:
    manager.send_notification(
        "Keymasq: Macro Recording Disabled",
        MACRO_RECORDING_DISABLED_MESSAGE,
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
        state.active_owner_writer_id = (
            coerce_int(
                owner.get("writer_id"),
                0,
            )
            or None
        )
        state.active_owner_pid = coerce_int(owner.get("pid"), 0) or None
        state.active_owner_uid = coerce_int(owner.get("uid"), 0) or None
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


def begin_pending_macro_save(
    manager: "SessionManager",
    recording_data: JsonObject,
    *,
    recording_slot: int = 1,
) -> str:
    state = manager.recording_state
    slot = normalize_pending_macro_recording_slot(
        recording_data.get("recording_slot", recording_slot),
        default=recording_slot or 1,
    )
    token = secrets.token_urlsafe(16)
    recording_data["recording_slot"] = slot
    recording_data["pending_save_token"] = token
    state.pending_slots[slot] = PendingSlot(
        data=recording_data,
        token=token,
        # Owner fields are retained for status compatibility, not cleanup.
        owner_writer_id=state.active_owner_writer_id,
        owner_pid=state.active_owner_pid,
        owner_uid=state.active_owner_uid,
        created_at=_monotonic(),
    )
    _sync_pending_macro_save(manager)
    _clear_active_recording_owner(manager)
    return token


def clear_pending_macro_save(
    manager: "SessionManager",
    *,
    recording_slot: int = 0,
    pending_save_token: str = "",
) -> None:
    state = manager.recording_state
    _ensure_pending_macro_save_slot(manager)
    slot = pending_macro_save_slot(
        manager,
        recording_slot=recording_slot,
        pending_save_token=pending_save_token,
    )
    if slot:
        state.pending_slots.pop(slot, None)
        _sync_pending_macro_save(manager)
    else:
        if recording_slot or str(pending_save_token or "").strip():
            return
        state.pending_save = None
        state.pending_slots.clear()


async def delete_pending_macro_slot(
    manager: "SessionManager",
    *,
    recording_slot: int = 0,
    pending_save_token: str = "",
) -> bool:
    _ensure_pending_macro_save_slot(manager)
    slot = pending_macro_save_slot(
        manager,
        recording_slot=recording_slot,
        pending_save_token=pending_save_token,
    )
    if not slot:
        return False

    pending_slot = manager.recording_state.pending_slots.get(slot)
    pending_data = pending_slot.data if pending_slot is not None else {}
    pending_recording_id = coerce_str(pending_data.get("pending_recording_id"), "")
    if pending_recording_id:
        try:
            result = await manager.client.send_command(
                Command(
                    command=CommandType.MACRO_DELETE_RECORDING,
                    data={"pending_recording_id": pending_recording_id},
                )
            )
        except (OSError, TimeoutError, EOFError):
            return False
        if result.status != "ok":
            return False
    clear_pending_macro_save(manager, recording_slot=slot)
    return True


async def store_pending_macro_save(
    manager: "SessionManager",
    recording_data: JsonObject,
    *,
    recording_slot: int,
) -> str:
    slot = normalize_pending_macro_recording_slot(recording_slot, default=1)
    pending_recording_id = coerce_str(recording_data.get("pending_recording_id"), "")
    existing = manager.recording_state.pending_slots.get(slot)
    if existing is not None:
        existing_id = coerce_str(existing.data.get("pending_recording_id"), "")
        if pending_recording_id and existing_id == pending_recording_id:
            token = existing.token
            existing.data.update(recording_data)
            existing.data["recording_slot"] = slot
            if token:
                existing.data["pending_save_token"] = token
            _sync_pending_macro_save(manager)
            return token
        await delete_pending_macro_slot(manager, recording_slot=slot)

    return begin_pending_macro_save(
        manager,
        recording_data,
        recording_slot=slot,
    )


async def sync_pending_macro_slots_from_daemon(manager: "SessionManager") -> None:
    try:
        result = await manager.client.send_command(
            Command(command=CommandType.MACRO_LIST_RECORDINGS)
        )
    except (OSError, TimeoutError, EOFError):
        return
    result_data = json_object(result.data)
    if result.status != "ok" or result_data is None:
        return
    replace_pending_macro_slots_from_daemon(
        manager,
        json_list(result_data.get("recordings")),
    )


def replace_pending_macro_slots_from_daemon(
    manager: "SessionManager",
    recordings: list[object],
) -> None:
    state = manager.recording_state
    old_slots = dict(state.pending_slots)

    state.pending_slots.clear()

    for item in recordings:
        data = json_object(item)
        if data is None:
            continue
        slot = normalize_macro_recording_slot(data.get("recording_slot"))
        pending_recording_id = coerce_str(data.get("pending_recording_id"), "")
        if not slot or not pending_recording_id:
            continue

        existing = old_slots.get(slot)
        existing_data = existing.data if existing is not None else {}
        existing_id = coerce_str(existing_data.get("pending_recording_id"), "")
        same_pending_recording = existing_id == pending_recording_id
        merged: JsonObject = {}
        if same_pending_recording:
            merged.update(existing_data)
        merged.update(data)
        token = ""
        if same_pending_recording and existing is not None:
            token = existing.token or coerce_str(
                merged.get("pending_save_token"),
                "",
            )
        if not token:
            token = secrets.token_urlsafe(16)

        merged["recording_slot"] = int(slot)
        merged["pending_recording_id"] = pending_recording_id
        merged["pending_save_token"] = token
        if same_pending_recording and existing is not None:
            state.pending_slots[slot] = PendingSlot(
                data=merged,
                token=token,
                owner_writer_id=existing.owner_writer_id,
                owner_pid=existing.owner_pid,
                owner_uid=existing.owner_uid,
                created_at=existing.created_at,
            )
        else:
            state.pending_slots[slot] = PendingSlot(
                data=merged,
                token=token,
                created_at=_monotonic(),
            )

    _sync_pending_macro_save(manager)


def pending_macro_save_token_matches(
    manager: "SessionManager",
    token: str,
) -> bool:
    return bool(pending_macro_save_slot_for_token(manager, token))


async def stop_recording(
    manager: "SessionManager",
    *,
    error_if_idle: bool,
    recording_slot: int = 0,
) -> JsonObject:
    if not manager.recording_state.active:
        if error_if_idle:
            return {"status": "error", "message": "No recording in progress"}
        return {"status": "ok"}
    slot = normalize_macro_recording_slot(recording_slot) or manager.recording_state.active_slot
    slot = normalize_pending_macro_recording_slot(slot, default=1)
    try:
        result = await manager.client.send_command(Command(command=CommandType.STOP_RECORDING))
    except (OSError, TimeoutError, EOFError):
        return {"status": "error", "message": "Daemon unavailable"}

    if result.status == "ok":
        result_data = json_object(result.data)
        if result_data is not None:
            recording_data = dict(result_data)
            if manager.recording_state.start_cursor:
                recording_data["start_x"] = int(manager.recording_state.start_cursor[0])
                recording_data["start_y"] = int(manager.recording_state.start_cursor[1])
                recording_data["move_to_start"] = True
            recording_data["recording_slot"] = slot
            pending_save_token = await store_pending_macro_save(
                manager,
                recording_data,
                recording_slot=slot,
            )
            if pending_save_token:
                recording_data["pending_save_token"] = pending_save_token
            manager.recording_state.active = False
            manager.recording_state.active_slot = 0
            manager.recording_state.start_cursor = None
            return {"status": "ok", **recording_data}
        manager.recording_state.active = False
        manager.recording_state.active_slot = 0
        manager.recording_state.start_cursor = None
        _clear_active_recording_owner(manager)
        return {"status": "ok"}
    return {"status": "error", "message": result.error or "Failed to stop recording"}


async def play_macro_by_name(manager: "SessionManager", name: str) -> JsonObject:
    return await play_macro_trigger(manager, {"macro_name": name})


async def play_macro_slot_trigger(manager: "SessionManager", data: JsonObject) -> JsonObject:
    slot = normalize_macro_recording_slot(data.get("recording_slot"))
    if not slot:
        return {
            "status": "error",
            "error_code": "macro_recording_slot_required",
            "message": (
                f"Macro slot playback requires a slot from 1 to {MAX_MACRO_RECORDING_SLOTS}."
            ),
        }

    active_slot = normalize_macro_recording_slot(manager.recording_state.active_slot)
    if manager.recording_state.active and active_slot == slot:
        message = f"Slot {slot} is currently recording. Stop recording before playing it."
        manager.send_notification("Keymasq: Macro Recording Active", message)
        return {
            "status": "error",
            "error_code": "macro_recording_slot_active",
            "message": message,
            "recording_slot": slot,
        }

    pending_slot = pending_macro_save_slot(manager, recording_slot=slot)
    if not pending_slot:
        await sync_pending_macro_slots_from_daemon(manager)
        pending_slot = pending_macro_save_slot(manager, recording_slot=slot)

    if not pending_slot:
        return {
            "status": "error",
            "error_code": "macro_recording_slot_empty",
            "message": f"Recording slot {slot} is empty.",
        }

    pending_slot_state = manager.recording_state.pending_slots.get(pending_slot)
    pending_data = pending_slot_state.data if pending_slot_state is not None else {}
    pending_recording_id = coerce_str(pending_data.get("pending_recording_id"), "")
    if not pending_recording_id:
        return {
            "status": "error",
            "error_code": "macro_recording_slot_empty",
            "message": f"Recording slot {slot} is empty.",
        }

    payload: JsonObject = {
        "pending_recording_id": pending_recording_id,
        "macro_name": f"recording-slot-{slot}",
        "replay_mouse_movement": True,
        "replay_mouse_clicks": True,
        "speed": 1.0,
        "loop_mode": "none",
        "loop_count": 1,
        "loop_stop_behavior": DEFAULT_MACRO_LOOP_STOP_BEHAVIOR,
        "move_to_start": coerce_bool(pending_data.get("move_to_start"), False),
        "start_x": coerce_int(pending_data.get("start_x"), 0),
        "start_y": coerce_int(pending_data.get("start_y"), 0),
        "block_mouse_movement": coerce_bool(pending_data.get("block_mouse_movement"), False),
        "source_device": str(data.get("source_device", "") or ""),
        "source_button": str(data.get("source_button", "") or ""),
        "trigger_value": coerce_int(data.get("trigger_value"), 1),
    }
    try:
        result = await manager.client.send_command(
            Command(command=CommandType.MACRO_PLAY_RECORDING, data=payload)
        )
    except (OSError, TimeoutError, EOFError):
        return {"status": "error", "message": "Daemon unavailable"}
    if result.status == "ok":
        response_data = json_object(result.data)
        return response_data if response_data is not None else {"status": "ok"}
    return {"status": "error", "message": result.error or "playback failed"}


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
            "loop_count": coerce_int(
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
            "move_to_start": coerce_bool(
                data.get(
                    "macro_move_to_start",
                    data.get("move_to_start", (macro or {}).get("move_to_start", False)),
                ),
                False,
            ),
            "start_x": coerce_int(
                data.get("macro_start_x", data.get("start_x", (macro or {}).get("start_x"))),
                0,
            ),
            "start_y": coerce_int(
                data.get("macro_start_y", data.get("start_y", (macro or {}).get("start_y"))),
                0,
            ),
            "block_mouse_movement": coerce_bool(
                data.get(
                    "macro_block_mouse_movement",
                    data.get(
                        "block_mouse_movement",
                        (macro or {}).get("block_mouse_movement", False),
                    ),
                ),
                False,
            ),
            "source_device": str(data.get("source_device", "") or ""),
            "source_button": str(data.get("source_button", "") or ""),
            "trigger_value": coerce_int(data.get("trigger_value"), 1),
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
        action = coerce_str(item.get("macro_action"), "").lower()
        if action == "exec_sync":
            timeout_ms = coerce_int(item.get("timeout_ms"), max_timeout)
            item["timeout_ms"] = max(1, min(timeout_ms, max_timeout))
        sanitized.append(item)
    cloned["events"] = sanitized
    return cloned


async def start_recording(
    manager: "SessionManager",
    reset_if_active: bool = False,
    *,
    recording_slot: int = 1,
    owner_peer: PeerCredentials | None = None,
    owner_writer: asyncio.StreamWriter | None = None,
) -> JsonObject:
    slot = normalize_macro_recording_slot(recording_slot)
    if not slot:
        return {
            "status": "error",
            "error_code": "macro_recording_slot_required",
            "message": (
                f"Macro recording requires an explicit slot from 1 to {MAX_MACRO_RECORDING_SLOTS}."
            ),
        }

    if manager.recording_state.active:
        active_slot = normalize_macro_recording_slot(manager.recording_state.active_slot)
        if active_slot and active_slot != slot:
            return {
                "status": "error",
                "error_code": "recording_already_active",
                "message": f"Recording already in progress in slot {active_slot}",
                "recording_slot": active_slot,
            }
        if not reset_if_active:
            return {"status": "error", "message": "Recording already in progress"}
        try:
            result = await manager.client.send_command(Command(command=CommandType.STOP_RECORDING))
            result_data = json_object(result.data)
            if result.status == "ok" and result_data is not None:
                recording_data = dict(result_data)
                recording_data["recording_slot"] = slot
                await store_pending_macro_save(
                    manager,
                    recording_data,
                    recording_slot=slot,
                )
        except (OSError, TimeoutError, EOFError):
            log.debug("Failed to stop active recording before starting a new one", exc_info=True)
        manager.recording_state.active = False
        manager.recording_state.active_slot = 0
        _clear_active_recording_owner(manager)

    replace_pending_slot = has_pending_macro_save(manager, recording_slot=slot)

    settings = manager.recording_state.settings
    include_mouse_movement = settings.get("include_mouse_movement", False)
    include_mouse_clicks = settings.get("include_mouse_clicks", False)
    record_start_position = settings.get("record_start_position", False)
    device_types = ["keyboard", "gamepad", "mouse"]

    if not manager.recording_state.devices_cache_ready:
        log.debug("Recording start using empty/uninitialized recording device cache")
    devices = list(manager.recording_state.selected_devices_cache)
    recording_ids = list(
        dict.fromkeys(current_id for d in devices if (current_id := recording_device_id(d)))
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
            except (OSError, TimeoutError, EOFError) as e:
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
                    "recording_slot": slot,
                    "devices": devices,
                    "include_mouse_movement": include_mouse_movement,
                    "include_mouse_clicks": include_mouse_clicks,
                    "start_x": start_x,
                    "start_y": start_y,
                },
            )
        )
    except (OSError, TimeoutError, EOFError):
        return {"status": "error", "message": "Daemon unavailable"}

    if result.status == "ok":
        if replace_pending_slot:
            await delete_pending_macro_slot(manager, recording_slot=slot)
        manager.recording_state.active = True
        manager.recording_state.active_slot = slot
        _set_active_recording_owner(
            manager,
            peer=owner_peer,
            writer=owner_writer,
        )
        response_data = json_object(result.data)
        if response_data:
            response = dict(response_data)
            response["recording_slot"] = slot
            return response
        return {"status": "ok", "recording_slot": slot}

    message = str(result.error or "Daemon unavailable")
    response: JsonObject = {"status": "error", "message": message}
    if "recording_locked" in message.lower():
        response["error_code"] = "recording_locked"
    if is_macro_recording_disabled_error(response):
        response["error_code"] = MACRO_RECORDING_DISABLED_ERROR_CODE
    return response


async def save_recording(
    manager: "SessionManager",
    name: str,
    move_to_start: bool = False,
    start_x: int = 0,
    start_y: int = 0,
    block_mouse_movement: bool = False,
    recording_slot: int = 0,
    pending_save_token: str = "",
) -> JsonObject:
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", name).strip("._")
    if not safe_name:
        return {
            "status": "error",
            "error_code": "invalid_macro_name",
            "message": "Macro name is invalid or empty",
        }

    slot = pending_macro_save_slot(
        manager,
        recording_slot=recording_slot,
        pending_save_token=pending_save_token,
    )
    if not slot:
        return {"status": "error", "message": "No pending recording"}

    pending_slot = manager.recording_state.pending_slots.get(slot)
    data = pending_slot.data if pending_slot is not None else {}
    pending_recording_id = coerce_str(data.get("pending_recording_id"), "")
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
    except (OSError, TimeoutError, EOFError):
        return {"status": "error", "message": "Daemon unavailable"}

    if result.status != "ok":
        return {"status": "error", "message": result.error or "Failed to save recording"}

    created_name = safe_name
    result_data = json_object(result.data)
    if result_data is not None:
        created = json_object(result_data.get("macro"))
        if created is not None:
            created_name = str(created.get("name", safe_name))

    data["move_to_start"] = bool(move_to_start)
    data["start_x"] = int(start_x)
    data["start_y"] = int(start_y)
    data["block_mouse_movement"] = bool(block_mouse_movement)
    _sync_pending_macro_save(manager)
    manager.broadcast_to_session_clients({"event": "macro_saved", "name": created_name})
    return {"status": "ok", "name": created_name}


def build_pending_macro_slot_meta(manager: "SessionManager") -> list[JsonObject]:
    _ensure_pending_macro_save_slot(manager)
    out: list[JsonObject] = []
    for slot in sorted(manager.recording_state.pending_slots):
        pending_slot = manager.recording_state.pending_slots[slot]
        data = pending_slot.data
        token = pending_slot.token
        duration_ms = coerce_int(data.get("duration_ms"), 0)
        duration_us = coerce_int(data.get("duration_us"), duration_ms * 1000)
        device_types = [str(value) for value in json_list(data.get("device_types"))]
        event_count = coerce_int(data.get("event_count"), 0)
        pending = True
        playable = True
        out.append(
            {
                "kind": "recording_slot",
                "name": f"__recording_slot_{slot}",
                "display_name": f"Slot {slot}",
                "recording_slot": int(slot),
                "pending_save_token": token,
                "pending": pending,
                "editable": False,
                "playable": playable,
                "duration_us": duration_us,
                "duration_ms": duration_ms,
                "device_types": device_types,
                "event_count": event_count,
                "move_to_start": coerce_bool(data.get("move_to_start"), False),
                "start_x": coerce_int(data.get("start_x"), 0),
                "start_y": coerce_int(data.get("start_y"), 0),
                "block_mouse_movement": coerce_bool(data.get("block_mouse_movement"), False),
            }
        )
    return out
