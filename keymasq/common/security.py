import socket
import struct
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast


@dataclass
class PeerCredentials:
    pid: int
    uid: int
    gid: int


@dataclass
class SecurityPolicy:
    session_command_acl: dict[str, list[str]] = field(default_factory=dict)
    daemon_command_acl: dict[str, list[str]] = field(default_factory=dict)
    daemon_allowed_uids: list[int] = field(default_factory=list)
    session_allowed_uids: list[int] = field(default_factory=list)
    macro_exec_timeout_max_ms: int = 30000
    recording_unlock_required: bool = True
    macro_edit_requires_unlock: bool = False
    gui_allow_left_right_click_remap: bool = False
    emergency_cancel_combo_enabled: bool = True


def _to_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items = cast(list[object], value)
    out: list[str] = []
    for item in items:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _to_acl_map(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    raw_acl = cast(dict[object, object], value)
    out: dict[str, list[str]] = {}
    for client_class, commands in raw_acl.items():
        if not isinstance(client_class, str):
            continue
        out[client_class] = _to_str_list(commands)
    return out


def _to_int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    items = cast(list[object], value)
    out: list[int] = []
    for item in items:
        if not isinstance(item, int | str):
            continue
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def load_security_policy(config_path: Path) -> SecurityPolicy:
    policy = SecurityPolicy(
        session_command_acl={"client": []},
        daemon_command_acl={"session": []},
    )

    if not config_path.exists():
        return policy

    raw: dict[str, Any] = tomllib.loads(config_path.read_text())

    session_acl = _to_acl_map(raw.get("session_command_acl"))
    if session_acl:
        policy.session_command_acl = session_acl

    daemon_acl = _to_acl_map(raw.get("daemon_command_acl"))
    if daemon_acl:
        policy.daemon_command_acl = daemon_acl

    policy.daemon_allowed_uids = _to_int_list(raw.get("daemon_allowed_uids"))
    policy.session_allowed_uids = _to_int_list(raw.get("session_allowed_uids"))

    macro_cfg = raw.get("macro")
    if isinstance(macro_cfg, dict):
        macro_settings = cast(dict[str, Any], macro_cfg)
        timeout_max = macro_settings.get("exec_timeout_max_ms", policy.macro_exec_timeout_max_ms)
        try:
            policy.macro_exec_timeout_max_ms = max(1, int(timeout_max))
        except (TypeError, ValueError):
            pass

    recording_guard_cfg = raw.get("recording_guard")
    if isinstance(recording_guard_cfg, dict):
        recording_guard = cast(dict[str, Any], recording_guard_cfg)
        policy.recording_unlock_required = bool(
            recording_guard.get("unlock_required", policy.recording_unlock_required)
        )
        policy.macro_edit_requires_unlock = bool(
            recording_guard.get("macro_edit_requires_unlock", policy.macro_edit_requires_unlock)
        )

    gui_cfg = raw.get("gui")
    if isinstance(gui_cfg, dict):
        gui_settings = cast(dict[str, Any], gui_cfg)
        policy.gui_allow_left_right_click_remap = bool(
            gui_settings.get(
                "allow_left_right_click_remap",
                policy.gui_allow_left_right_click_remap,
            )
        )
        policy.emergency_cancel_combo_enabled = bool(
            gui_settings.get(
                "emergency_cancel_combo_enabled",
                policy.emergency_cancel_combo_enabled,
            )
        )

    return policy


def get_peer_credentials(transport_socket: Any) -> PeerCredentials | None:
    if transport_socket is None:
        return None

    try:
        creds = transport_socket.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            struct.calcsize("3i"),
        )
        pid, uid, gid = struct.unpack("3i", creds)
    except Exception:
        return None

    return PeerCredentials(pid=pid, uid=uid, gid=gid)


def command_allowed(command: str, acl: dict[str, list[str]], client_class: str) -> bool:
    entries: list[str] | None = acl.get(client_class)
    if entries is None:
        entries = []
        for value in acl.values():
            entries.extend(value)

    for raw in entries:
        token = raw.strip()
        if token.startswith("!"):
            denied = token[1:].strip()
        elif token.startswith("-"):
            denied = token[1:].strip()
        elif token.lower().startswith("deny:"):
            denied = token[5:].strip()
        else:
            continue

        if denied in {"*", command}:
            return False

    return True


def uid_allowed(uid: int, allowed_uids: list[int]) -> bool:
    if not allowed_uids:
        return True
    return uid in set(allowed_uids)
