import logging
import socket
import struct
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

log = logging.getLogger(__name__)
DEFAULT_MACRO_RECORDING_TIME_LIMIT = 10
_REMOVED_RECORDING_GUARD_KEYS = ("unlock_required", "macro_edit_requires_unlock")


@dataclass
class PeerCredentials:
    pid: int
    uid: int
    gid: int


@dataclass
class SecurityPolicy:
    daemon_allowed_uids: list[int] = field(default_factory=list)
    session_allowed_uids: list[int] = field(default_factory=list)
    macro_exec_timeout_max_ms: int = 30000
    macro_recording_allowed: bool = True
    macro_recording_time_limit: int = DEFAULT_MACRO_RECORDING_TIME_LIMIT
    emergency_cancel_combo_enabled: bool = True


class SecurityPolicyError(RuntimeError):
    """Raised when the security policy file exists but cannot be loaded."""


def _to_int_list(value: Any, setting_name: str) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SecurityPolicyError(f"{setting_name} must be a list of integer UIDs")

    items = cast(list[object], value)
    out: list[int] = []
    for item in items:
        if isinstance(item, bool) or not isinstance(item, int | str):
            raise SecurityPolicyError(f"{setting_name} contains invalid UID {item!r}")
        try:
            out.append(int(item))
        except ValueError as exc:
            raise SecurityPolicyError(f"{setting_name} contains invalid UID {item!r}") from exc
    return out


def _to_bool(value: Any, setting_name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise SecurityPolicyError(f"{setting_name} must be a boolean")
    return value


def load_security_policy(config_path: Path) -> SecurityPolicy:
    policy = SecurityPolicy()

    try:
        config_text = config_path.read_text()
    except FileNotFoundError:
        return policy
    except OSError as exc:
        raise SecurityPolicyError(f"Failed to read security policy {config_path}: {exc}") from exc

    try:
        raw: dict[str, Any] = tomllib.loads(config_text)
    except tomllib.TOMLDecodeError as exc:
        raise SecurityPolicyError(f"Invalid security policy TOML at {config_path}: {exc}") from exc

    policy.daemon_allowed_uids = _to_int_list(
        raw.get("daemon_allowed_uids"),
        "daemon_allowed_uids",
    )
    policy.session_allowed_uids = _to_int_list(
        raw.get("session_allowed_uids"),
        "session_allowed_uids",
    )

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
        for removed_key in _REMOVED_RECORDING_GUARD_KEYS:
            if removed_key in recording_guard:
                log.warning(
                    "Ignoring recording_guard.%s in %s: the capture unlock was removed",
                    removed_key,
                    config_path,
                )
        policy.macro_recording_allowed = _to_bool(
            recording_guard.get("macro_recording_allowed"),
            "recording_guard.macro_recording_allowed",
            policy.macro_recording_allowed,
        )
        time_limit = recording_guard.get(
            "macro_recording_time_limit",
            policy.macro_recording_time_limit,
        )
        if isinstance(time_limit, bool) or not isinstance(time_limit, int):
            raise SecurityPolicyError(
                "recording_guard.macro_recording_time_limit must be a non-negative integer"
            )
        if time_limit < 0:
            raise SecurityPolicyError(
                "recording_guard.macro_recording_time_limit must be a non-negative integer"
            )
        policy.macro_recording_time_limit = time_limit

    gui_cfg = raw.get("gui")
    if isinstance(gui_cfg, dict):
        gui_settings = cast(dict[str, Any], gui_cfg)
        policy.emergency_cancel_combo_enabled = _to_bool(
            gui_settings.get("emergency_cancel_combo_enabled"),
            "gui.emergency_cancel_combo_enabled",
            policy.emergency_cancel_combo_enabled,
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
    except OSError:
        return None
    except struct.error:
        return None
    except Exception:
        log.exception("Unexpected failure reading peer credentials")
        return None

    return PeerCredentials(pid=pid, uid=uid, gid=gid)


def uid_allowed(uid: int, allowed_uids: list[int]) -> bool:
    if allowed_uids == []:
        return True
    return uid in set(allowed_uids)
