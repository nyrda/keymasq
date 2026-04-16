__all__ = [
    "KEYMASQ_USER",
    "KEYMASQ_GROUP",
    "SOCKET_PATH",
    "SESSION_SOCKET_PATH",
    "GNOME_BRIDGE_SOCKET_PATH",
    "CONFIG_DIR",
    "HARDWARE_DIR",
    "PROFILES_DIR",
    "SUPERKEYS_DIR",
    "STATE_DIR",
    "SECURITY_POLICY_PATH",
    "RECORDING_UNLOCK_RUNTIME_DIR",
    "RECORDING_UNLOCK_PERSISTENT_DIR",
    "KEYMASQ_RECORD_HELPER_PATH",
    "resolve_keymasq_record_helper_path",
    "SLURP_PATH",
    "resolve_slurp_path",
]

import contextlib
import importlib
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

KEYMASQ_USER = "keymasq"
KEYMASQ_GROUP = "keymasq"

RUN_DIR = Path("/run/keymasq")
SOCKET_PATH = RUN_DIR / "socket"
STATE_DIR = Path("/var/lib/keymasq")
SECURITY_POLICY_PATH = Path("/etc/keymasq/security.toml")
RECORDING_UNLOCK_RUNTIME_DIR = RUN_DIR
RECORDING_UNLOCK_PERSISTENT_DIR = Path("/etc/keymasq")

XDG_RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
SESSION_SOCKET_PATH = XDG_RUNTIME_DIR / "keymasq" / "session.sock"
GNOME_BRIDGE_SOCKET_PATH = XDG_RUNTIME_DIR / "keymasq" / "gnome-bridge.sock"

CONFIG_DIR = Path.home() / ".config" / "keymasq"
HARDWARE_DIR = CONFIG_DIR / "hardware"
PROFILES_DIR = CONFIG_DIR / "profiles"
SUPERKEYS_DIR = CONFIG_DIR / "superkeys"

_build_helper_path = "/usr/bin/keymasq-record"
_build_slurp_path = "/usr/bin/slurp"
with contextlib.suppress(ImportError, AttributeError):
    build_paths = importlib.import_module("keymasq.common.build_paths")
    _build_helper_path = str(build_paths.KEYMASQ_RECORD_HELPER_PATH)
    _build_slurp_path = str(getattr(build_paths, "SLURP_PATH", _build_slurp_path))

KEYMASQ_RECORD_HELPER_PATH = Path(_build_helper_path)
SLURP_PATH = Path(_build_slurp_path)


def ensure_config_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    HARDWARE_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    SUPERKEYS_DIR.mkdir(parents=True, exist_ok=True)


def ensure_session_socket_dir() -> None:
    session_dir = SESSION_SOCKET_PATH.parent
    session_dir.mkdir(parents=True, exist_ok=True)
    try:
        session_dir.chmod(0o700)
    except OSError:
        log.warning("Failed to set session socket directory permissions to 0o700: %s", session_dir)


def resolve_keymasq_record_helper_path() -> str | None:
    if KEYMASQ_RECORD_HELPER_PATH.is_file() and os.access(KEYMASQ_RECORD_HELPER_PATH, os.X_OK):
        return str(KEYMASQ_RECORD_HELPER_PATH)
    return None


def resolve_slurp_path() -> str | None:
    if SLURP_PATH.is_file() and os.access(SLURP_PATH, os.X_OK):
        return str(SLURP_PATH)
    return None
