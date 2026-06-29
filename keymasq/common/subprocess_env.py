from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

APPIMAGE_HOST_SUBPROCESS_DROP_ENV = (
    "APPDIR",
    "APPIMAGE",
    "APPIMAGE_EXTRACT_AND_RUN",
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONNOUSERSITE",
    "GI_TYPELIB_PATH",
    "GDK_PIXBUF_MODULE_FILE",
    "GDK_PIXBUF_MODULEDIR",
    "GIO_MODULE_DIR",
    "XKB_CONFIG_ROOT",
    "GTK_PATH",
    "GTK_EXE_PREFIX",
    "GTK_DATA_PREFIX",
    "GSETTINGS_SCHEMA_DIR",
)


def appimage_environment_root(env: Mapping[str, str]) -> str | None:
    appdir = env.get("APPDIR")
    if appdir:
        return appdir

    pythonhome = env.get("PYTHONHOME")
    if pythonhome and (env.get("APPIMAGE") or "/.mount_" in pythonhome):
        return pythonhome

    return None


def _strip_appimage_path_entries(value: str, appimage_root: str, subdir: str) -> str:
    root = os.path.normpath(appimage_root)
    appimage_path = os.path.normpath(os.path.join(root, subdir))
    entries: list[str] = []
    for raw_entry in value.split(os.pathsep):
        if not raw_entry:
            continue
        entry = os.path.normpath(raw_entry)
        if entry == appimage_path or entry.startswith(f"{appimage_path}{os.sep}"):
            continue
        entries.append(raw_entry)
    return os.pathsep.join(entries)


def _resolved_path(path: str) -> Path:
    try:
        return Path(path).resolve(strict=False)
    except (OSError, RuntimeError):
        return Path(os.path.abspath(path))


def path_is_inside(path: str, root: str) -> bool:
    resolved_path = _resolved_path(path)
    resolved_root = _resolved_path(root)
    try:
        return resolved_path == resolved_root or resolved_path.is_relative_to(resolved_root)
    except ValueError:
        return False


def is_appimage_path(path: str, env: Mapping[str, str] | None = None) -> bool:
    process_env = os.environ if env is None else env
    appimage_root = appimage_environment_root(process_env)
    if appimage_root is None:
        return False
    return path_is_inside(path, appimage_root)


def host_subprocess_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    clean_env = dict(os.environ if env is None else env)
    appimage_root = appimage_environment_root(clean_env)
    if appimage_root is None:
        return clean_env

    ld_library_path = clean_env.get("LD_LIBRARY_PATH")
    if ld_library_path:
        clean_ld_library_path = _strip_appimage_path_entries(
            ld_library_path,
            appimage_root,
            "lib",
        )
        if clean_ld_library_path:
            clean_env["LD_LIBRARY_PATH"] = clean_ld_library_path
        else:
            clean_env.pop("LD_LIBRARY_PATH", None)

    xdg_data_dirs = clean_env.get("XDG_DATA_DIRS")
    if xdg_data_dirs:
        clean_xdg_data_dirs = _strip_appimage_path_entries(
            xdg_data_dirs,
            appimage_root,
            "share",
        )
        if clean_xdg_data_dirs:
            clean_env["XDG_DATA_DIRS"] = clean_xdg_data_dirs
        else:
            clean_env.pop("XDG_DATA_DIRS", None)

    for key in APPIMAGE_HOST_SUBPROCESS_DROP_ENV:
        clean_env.pop(key, None)

    return clean_env
