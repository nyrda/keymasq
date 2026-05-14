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
    "SETTINGS_PATH",
    "ANALOG_CONTROLS_DIR",
    "VIRTUAL_DEVICES_PATH",
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
import shutil
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

XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
CONFIG_DIR = XDG_CONFIG_HOME / "keymasq"
HARDWARE_DIR = CONFIG_DIR / "hardware"
PROFILES_DIR = CONFIG_DIR / "profiles"
SUPERKEYS_DIR = CONFIG_DIR / "superkeys"
SETTINGS_PATH = CONFIG_DIR / "settings.toml"
ANALOG_CONTROLS_DIR = CONFIG_DIR / "analog_controls"
VIRTUAL_DEVICES_PATH = CONFIG_DIR / "virtual_devices.toml"

_build_helper_path = "/usr/bin/keymasq-record"
_build_slurp_path = "/usr/bin/slurp"
with contextlib.suppress(ImportError, AttributeError):
    build_paths = importlib.import_module("keymasq.common.build_paths")
    _build_helper_path = str(build_paths.KEYMASQ_RECORD_HELPER_PATH)
    _build_slurp_path = str(getattr(build_paths, "SLURP_PATH", _build_slurp_path))

KEYMASQ_RECORD_HELPER_PATH = Path(_build_helper_path)
KEYMASQ_RECORD_HELPER_FALLBACK_PATHS = (
    Path("/run/current-system/sw/bin/keymasq-record"),
)
SLURP_PATH = Path(_build_slurp_path)
SLURP_FALLBACK_PATHS = (
    Path("/usr/bin/slurp"),
    Path("/run/current-system/sw/bin/slurp"),
)


def ensure_config_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    HARDWARE_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    SUPERKEYS_DIR.mkdir(parents=True, exist_ok=True)
    ANALOG_CONTROLS_DIR.mkdir(parents=True, exist_ok=True)


def ensure_session_socket_dir() -> None:
    session_dir = SESSION_SOCKET_PATH.parent
    session_dir.mkdir(parents=True, exist_ok=True)
    try:
        session_dir.chmod(0o700)
    except OSError:
        log.warning("Failed to set session socket directory permissions to 0o700: %s", session_dir)


def resolve_keymasq_record_helper_path() -> str | None:
    candidates = [KEYMASQ_RECORD_HELPER_PATH, *KEYMASQ_RECORD_HELPER_FALLBACK_PATHS]
    seen: set[str] = set()
    for candidate in candidates:
        candidate_str = str(candidate)
        if candidate_str in seen:
            continue
        seen.add(candidate_str)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate_str
    return None


def resolve_slurp_path() -> str | None:
    env_slurp_path = os.environ.get("SLURP_PATH")
    if env_slurp_path is not None:
        if env_slurp_path == "":
            return None
        env_path = Path(env_slurp_path)
        if env_path.is_file() and os.access(env_path, os.X_OK):
            return str(env_path)
        return None

    candidates: list[Path] = [SLURP_PATH, *SLURP_FALLBACK_PATHS]
    path_slurp = shutil.which("slurp")
    if path_slurp:
        candidates.append(Path(path_slurp))

    seen: set[str] = set()
    for candidate in candidates:
        candidate_str = str(candidate)
        if candidate_str in seen:
            continue
        seen.add(candidate_str)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate_str
    return None
