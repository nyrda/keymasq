import asyncio
import contextlib
import ctypes
import logging
import os
import struct
from pathlib import Path
from typing import Any

from keymasq.common.paths import (
    ANALOG_CONTROLS_DIR,
    CONFIG_DIR,
    HARDWARE_DIR,
    MOTION_CONTROLS_DIR,
    PROFILES_DIR,
    SETTINGS_PATH,
    SUPERKEYS_DIR,
    VIRTUAL_DEVICES_PATH,
)
from keymasq.session.settings import load_global_settings
from keymasq.session.virtual_devices import load_virtual_device_config

log = logging.getLogger("keymasq-session")
CONFIG_RELOAD_DEBOUNCE_S = 0.5
CONFIG_RELOAD_EXPLICIT_COALESCE_S = CONFIG_RELOAD_DEBOUNCE_S + 1.0
IN_ACCESS = 0x00000001
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_IGNORED = 0x00008000
IN_ISDIR = 0x40000000
IN_NONBLOCK = 0x00000800
IN_CLOEXEC = 0x00080000
INOTIFY_EVENT_STRUCT = struct.Struct("iIII")
INOTIFY_WATCH_MASK = (
    IN_ATTRIB
    | IN_CLOSE_WRITE
    | IN_CREATE
    | IN_DELETE
    | IN_DELETE_SELF
    | IN_MOVE_SELF
    | IN_MOVED_FROM
    | IN_MOVED_TO
)


class ConfigWatcherMixin:
    def reload_config_from_disk(self: Any) -> None:
        with self.profiles.profile_file_transaction():
            superkeys_snapshot = self.superkeys.snapshot_superkeys()
            analog_controls_snapshot = self.analog_controls.snapshot_analog_controls()
            motion_controls_snapshot = self.motion_controls.snapshot_motion_controls()
            profiles_snapshot = self.profiles.snapshot_profiles_for_reload()
            hardware_snapshot = self.hardware.snapshot_hardware()
            old_virtual_gamepad_count = self.virtual_gamepad_count
            old_virtual_device_config = self.virtual_device_config

            try:
                self.superkeys.reload()
                self.analog_controls.reload()
                self.motion_controls.reload()
                self.profiles.reload()
                self.hardware.reload()
                settings = load_global_settings(strict=True)
                self.virtual_gamepad_count = settings.virtual_gamepad_count
                self.virtual_device_config = load_virtual_device_config(strict=True)
            except Exception:
                self.superkeys.restore_superkeys(superkeys_snapshot)
                self.analog_controls.restore_analog_controls(analog_controls_snapshot)
                self.motion_controls.restore_motion_controls(motion_controls_snapshot)
                self.profiles.restore_profiles(profiles_snapshot)
                self.hardware.restore_hardware(hardware_snapshot)
                self.virtual_gamepad_count = old_virtual_gamepad_count
                self.virtual_device_config = old_virtual_device_config
                raise

    def _start_config_watcher(self: Any) -> None:
        try:
            fd = _inotify_init()
        except OSError as exc:
            log.warning("Failed to start user config watcher: %s", exc)
            return

        self.config_watch_fd = fd
        self._refresh_config_watches()
        try:
            asyncio.get_running_loop().add_reader(fd, self._handle_config_watch_events)
        except (OSError, RuntimeError, ValueError) as exc:
            log.warning("Failed to register user config watcher: %s", exc)
            self._stop_config_watcher()

    def _stop_config_watcher(self: Any) -> None:
        timer = self.config_reload_timer
        if timer is not None:
            timer.cancel()
            self.config_reload_timer = None

        fd = self.config_watch_fd
        if fd is None:
            return

        with contextlib.suppress(RuntimeError, ValueError):
            asyncio.get_running_loop().remove_reader(fd)
        with contextlib.suppress(OSError):
            os.close(fd)
        self.config_watch_fd = None
        self.config_watch_watches.clear()

    def _refresh_config_watches(self: Any) -> None:
        fd = self.config_watch_fd
        if fd is None:
            return

        watched_paths = set(self.config_watch_watches.values())
        for path in (
            CONFIG_DIR,
            PROFILES_DIR,
            HARDWARE_DIR,
            SUPERKEYS_DIR,
            ANALOG_CONTROLS_DIR,
            MOTION_CONTROLS_DIR,
        ):
            if path in watched_paths:
                continue
            try:
                wd = _inotify_add_watch(fd, path, INOTIFY_WATCH_MASK)
            except OSError as exc:
                log.debug("Failed to watch config path %s: %s", path, exc)
                continue
            self.config_watch_watches[wd] = path

    def _handle_config_watch_events(self: Any) -> None:
        fd = self.config_watch_fd
        if fd is None:
            return

        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            return
        except OSError as exc:
            log.warning("User config watcher failed: %s", exc)
            self._stop_config_watcher()
            return

        should_reload = False
        offset = 0
        while offset + INOTIFY_EVENT_STRUCT.size <= len(data):
            wd, mask, _cookie, name_len = INOTIFY_EVENT_STRUCT.unpack_from(data, offset)
            offset += INOTIFY_EVENT_STRUCT.size
            raw_name = data[offset : offset + name_len]
            offset += name_len
            name = raw_name.split(b"\0", 1)[0].decode(errors="replace")
            watched_path = self.config_watch_watches.get(wd)
            if mask & IN_IGNORED:
                self.config_watch_watches.pop(wd, None)
            if watched_path is None:
                continue
            if self._config_watch_event_is_relevant(watched_path, name, mask):
                should_reload = True

        self._refresh_config_watches()
        if should_reload:
            self._schedule_config_reload()

    def _config_watch_event_is_relevant(
        self: Any, watched_path: Path, name: str, mask: int
    ) -> bool:
        if watched_path == CONFIG_DIR:
            if name in {SETTINGS_PATH.name, VIRTUAL_DEVICES_PATH.name}:
                return True
            return bool(mask & IN_ISDIR) and name in {
                PROFILES_DIR.name,
                HARDWARE_DIR.name,
                SUPERKEYS_DIR.name,
                ANALOG_CONTROLS_DIR.name,
            }
        if name:
            return name.endswith(".toml")
        return bool(mask & (IN_DELETE_SELF | IN_MOVE_SELF | IN_ATTRIB))

    def _schedule_config_reload(self: Any) -> None:
        loop = asyncio.get_running_loop()
        if self._config_reload_is_coalesced(loop):
            log.debug("Skipping config watcher reload after explicit reload request")
            return

        timer = self.config_reload_timer
        if timer is not None:
            timer.cancel()
        self.config_reload_timer = loop.call_later(
            CONFIG_RELOAD_DEBOUNCE_S,
            self._run_scheduled_config_reload,
        )

    def _config_reload_is_coalesced(self: Any, loop: asyncio.AbstractEventLoop) -> bool:
        return loop.time() <= self._config_reload_coalesce_until

    def suppress_config_watcher_reload(self: Any) -> None:
        timer = self.config_reload_timer
        if timer is not None:
            timer.cancel()
            self.config_reload_timer = None
        loop = asyncio.get_running_loop()
        self._config_reload_coalesce_until = max(
            self._config_reload_coalesce_until,
            loop.time() + CONFIG_RELOAD_EXPLICIT_COALESCE_S,
        )

    async def wait_for_running_config_reload(self: Any) -> bool | None:
        task = self.reload_task
        if task is None or task.done():
            return None
        log.debug("Waiting for running config reload before handling explicit reload request")
        try:
            return await task
        except Exception:
            log.exception("Running config reload failed")
            return False

    def _run_scheduled_config_reload(self: Any) -> None:
        self.config_reload_timer = None
        if not self.running:
            return
        if self._config_reload_is_coalesced(asyncio.get_running_loop()):
            log.debug("Skipping scheduled config reload after explicit reload request")
            return
        if self.reload_task is not None and not self.reload_task.done():
            log.debug("Config reload already running; skipping scheduled reload")
            return
        log.info("Detected user config file change; reloading")
        self.reload_task = asyncio.create_task(self.reload_profiles())


def _inotify_init() -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    inotify_init1 = libc.inotify_init1
    inotify_init1.argtypes = [ctypes.c_int]
    inotify_init1.restype = ctypes.c_int
    fd = int(inotify_init1(IN_NONBLOCK | IN_CLOEXEC))
    if fd < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return fd


def _inotify_add_watch(fd: int, path: Path, mask: int) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    inotify_add_watch = libc.inotify_add_watch
    inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    inotify_add_watch.restype = ctypes.c_int
    wd = int(inotify_add_watch(fd, os.fsencode(path), mask))
    if wd < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno), str(path))
    return wd
