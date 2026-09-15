"""Trusted host commands for masking jobs and their generated udev rules."""

import importlib
import os
import shutil
from pathlib import Path
from typing import cast

SYSTEM_PATH = "/usr/sbin:/usr/bin:/sbin:/bin:/run/current-system/sw/bin"
COMMAND_NAMES = ("udevadm", "systemctl", "getfacl", "setfacl", "chmod")

try:
    _build_paths = importlib.import_module("keymasq.common.build_paths")
except ModuleNotFoundError as exc:
    if exc.name != "keymasq.common.build_paths":
        raise
    _build_paths = None

BUILD_COMMAND_PATHS = cast(dict[str, str], getattr(_build_paths, "MASKING_COMMAND_PATHS", {}))


def command_path(name: str) -> str:
    """Resolve only known tools, independently of the caller's environment."""
    if name not in COMMAND_NAMES:
        raise ValueError(f"Unknown masking command: {name}")
    configured = BUILD_COMMAND_PATHS.get(name)
    if configured is not None:
        if not Path(configured).is_absolute():
            raise ValueError(f"Masking command must have an absolute path: {name}")
        if not Path(configured).is_file() or not os.access(configured, os.X_OK):
            raise FileNotFoundError(f"Required masking command is unavailable: {configured}")
        return configured
    resolved = shutil.which(name, path=SYSTEM_PATH)
    if resolved is None:
        raise FileNotFoundError(f"Required masking command is unavailable: {name}")
    return resolved


def host_commands() -> dict[str, str]:
    """Check dependencies before arming access restrictions."""
    return {name: command_path(name) for name in COMMAND_NAMES}
