__all__ = [
    "KEYFORGE_USER",
    "KEYFORGE_GROUP",
    "SOCKET_PATH",
    "SESSION_SOCKET_PATH",
    "GNOME_BRIDGE_SOCKET_PATH",
    "CONFIG_DIR",
    "HARDWARE_DIR",
    "PROFILES_DIR",
    "SUPERKEYS_DIR",
    "MACROS_DIR",
    "STATE_DIR",
    "SECURITY_POLICY_PATH",
    "RECORDING_UNLOCK_RUNTIME_DIR",
    "RECORDING_UNLOCK_PERSISTENT_DIR",
    "KEYFORGE_RECORD_HELPER_PATH",
    "resolve_keyforge_record_helper_path",
    "SLURP_PATH",
    "resolve_slurp_path",
]

import contextlib
import importlib
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

KEYFORGE_USER = "keyforge"
KEYFORGE_GROUP = "keyforge"

RUN_DIR = Path("/run/keyforge")
SOCKET_PATH = RUN_DIR / "socket"
STATE_DIR = Path("/var/lib/keyforge")
SECURITY_POLICY_PATH = Path("/etc/keyforge/security.toml")
RECORDING_UNLOCK_RUNTIME_DIR = RUN_DIR
RECORDING_UNLOCK_PERSISTENT_DIR = Path("/etc/keyforge")

XDG_RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
SESSION_SOCKET_PATH = XDG_RUNTIME_DIR / "keyforge" / "session.sock"
GNOME_BRIDGE_SOCKET_PATH = XDG_RUNTIME_DIR / "keyforge" / "gnome-bridge.sock"

CONFIG_DIR = Path.home() / ".config" / "keyforge"
HARDWARE_DIR = CONFIG_DIR / "hardware"
PROFILES_DIR = CONFIG_DIR / "profiles"
SUPERKEYS_DIR = CONFIG_DIR / "superkeys"
MACROS_DIR = CONFIG_DIR / "macros"

_build_helper_path = "/usr/bin/keyforge-record"
_build_slurp_path = "/usr/bin/slurp"
with contextlib.suppress(ImportError, AttributeError):
    build_paths = importlib.import_module("keyforge.common.build_paths")
    _build_helper_path = str(build_paths.KEYFORGE_RECORD_HELPER_PATH)
    _build_slurp_path = str(getattr(build_paths, "SLURP_PATH", _build_slurp_path))

KEYFORGE_RECORD_HELPER_PATH = Path(_build_helper_path)
SLURP_PATH = Path(_build_slurp_path)


def ensure_config_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    HARDWARE_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    SUPERKEYS_DIR.mkdir(parents=True, exist_ok=True)
    MACROS_DIR.mkdir(parents=True, exist_ok=True)


def ensure_session_socket_dir() -> None:
    session_dir = SESSION_SOCKET_PATH.parent
    session_dir.mkdir(parents=True, exist_ok=True)
    try:
        session_dir.chmod(0o700)
    except OSError:
        log.warning("Failed to set session socket directory permissions to 0o700: %s", session_dir)


def resolve_keyforge_record_helper_path() -> str | None:
    if KEYFORGE_RECORD_HELPER_PATH.is_file() and os.access(KEYFORGE_RECORD_HELPER_PATH, os.X_OK):
        return str(KEYFORGE_RECORD_HELPER_PATH)
    return None


def resolve_slurp_path() -> str | None:
    if SLURP_PATH.is_file() and os.access(SLURP_PATH, os.X_OK):
        return str(SLURP_PATH)
    return None
