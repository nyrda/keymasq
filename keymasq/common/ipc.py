import json
import struct
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CommandType(Enum):
    GRAB_DEVICE = "grab_device"
    RELEASE_DEVICE = "release_device"
    SET_MAPPING = "set_mapping"
    SET_COMBOS = "set_combos"
    SET_CURSOR_POSITION = "set_cursor_position"
    SET_CURSOR_POSITION_BACKEND = "set_cursor_position_backend"
    SET_CURSOR_POSITION_RESULT = "set_cursor_position_result"
    LIST_DEVICES = "list_devices"
    PING = "ping"
    DEVICE_EVENT = "device_event"
    ACTION_TRIGGER = "action_trigger"
    DEVICE_CONNECTED = "device_connected"
    DEVICE_DISCONNECTED = "device_disconnected"
    DEVICE_GRAB_STATUS = "device_grab_status"
    GET_ACTIVE_PROFILES = "get_active_profiles"
    PROFILE_CHANGED = "profile_changed"
    PROFILES_CHANGED = "profiles_changed"
    GET_COMPOSITOR = "get_compositor"
    KEYMASQD_STATUS = "keymasqd_status"
    START_RECORDING = "start_recording"
    STOP_RECORDING = "stop_recording"
    RECORDING_STARTED = "recording_started"
    RECORDING_STOPPED = "recording_stopped"
    RECORDING_PROGRESS = "recording_progress"
    MACRO_PLAYBACK_CANCELLED = "macro_playback_cancelled"
    RUNTIME_RESET = "runtime_reset"
    PLAY_MACRO = "play_macro"
    MACRO_LIST_META = "macro_list_meta"
    MACRO_GET = "macro_get"
    MACRO_CREATE = "macro_create"
    MACRO_UPDATE = "macro_update"
    MACRO_RENAME = "macro_rename"
    MACRO_DELETE = "macro_delete"
    MACRO_SAVE_RECORDING = "macro_save_recording"
    MACRO_DISCARD_RECORDING = "macro_discard_recording"
    MACRO_PLAY_BY_NAME = "macro_play_by_name"
    CANCEL_MACRO_PLAYBACK = "cancel_macro_playback"
    EMERGENCY_RESET = "emergency_reset"
    MACRO_EXEC_COMPLETE = "macro_exec_complete"
    CAPTURE_BEGIN = "capture_begin"
    CAPTURE_READ = "capture_read"
    CAPTURE_END = "capture_end"
    CAPTURE_COMBO = "capture_combo"
    SET_DIAGNOSTICS = "set_diagnostics"
    REFRESH_RECORDING_UNLOCK = "refresh_recording_unlock"
    LOCK_RECORDING_UNLOCK = "lock_recording_unlock"


@dataclass
class Command:
    command: CommandType
    data: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None


@dataclass
class Response:
    status: str
    data: Any = None
    error: str | None = None
    request_id: str | None = None


HEADER_FORMAT = "!I"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
MAX_PAYLOAD_SIZE = 16 * 1024 * 1024  # 16 MiB


def encode_command(cmd: Command) -> bytes:
    payload = {
        "command": cmd.command.value,
        "data": cmd.data,
        "request_id": cmd.request_id,
    }
    json_bytes = json.dumps(payload).encode("utf-8")
    header = struct.pack(HEADER_FORMAT, len(json_bytes))
    return header + json_bytes


def decode_command(data: bytes) -> tuple[Command | None, bytes]:
    if len(data) < HEADER_SIZE:
        return None, data

    payload_len = struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])[0]
    if payload_len > MAX_PAYLOAD_SIZE:
        return None, data[HEADER_SIZE:]

    total_len = HEADER_SIZE + payload_len

    if len(data) < total_len:
        return None, data

    json_bytes = data[HEADER_SIZE:total_len]
    remaining = data[total_len:]

    try:
        payload = json.loads(json_bytes.decode("utf-8"))
        cmd = Command(
            command=CommandType(payload["command"]),
            data=payload.get("data", {}),
            request_id=payload.get("request_id"),
        )
        return cmd, remaining
    except (json.JSONDecodeError, KeyError, ValueError):
        return None, remaining


def encode_response(resp: Response) -> bytes:
    payload = {
        "status": resp.status,
        "data": resp.data,
        "error": resp.error,
        "request_id": resp.request_id,
    }
    json_bytes = json.dumps(payload).encode("utf-8")
    header = struct.pack(HEADER_FORMAT, len(json_bytes))
    return header + json_bytes


def decode_response(data: bytes) -> tuple[Response | None, bytes]:
    if len(data) < HEADER_SIZE:
        return None, data

    payload_len = struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])[0]
    if payload_len > MAX_PAYLOAD_SIZE:
        return None, data[HEADER_SIZE:]

    total_len = HEADER_SIZE + payload_len

    if len(data) < total_len:
        return None, data

    json_bytes = data[HEADER_SIZE:total_len]
    remaining = data[total_len:]

    try:
        payload = json.loads(json_bytes.decode("utf-8"))
        resp = Response(
            status=payload["status"],
            data=payload.get("data"),
            error=payload.get("error"),
            request_id=payload.get("request_id"),
        )
        return resp, remaining
    except (json.JSONDecodeError, KeyError, ValueError):
        return None, remaining
