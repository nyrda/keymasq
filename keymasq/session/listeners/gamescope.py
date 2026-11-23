import asyncio
import logging
import os
import struct
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from keymasq.session.dbus import SessionDBus
from keymasq.session.listeners._socket_helpers import runtime_dir
from keymasq.session.listeners.base import WindowChangeCallback, WindowListener

log = logging.getLogger("keymasq-session.listeners.gamescope")

STEAM_APP_ID = 769
STEAM_OVERLAY_LAYER = "steam-overlay"
STEAM_OSK_LAYER = "steam-osk"
GAMESCOPE_SESSION_ENV_VARS = (
    "XDG_CURRENT_DESKTOP",
    "XDG_SESSION_DESKTOP",
    "DESKTOP_SESSION",
)


def _load_xlib() -> tuple[
    object | None,
    object | None,
    tuple[type[BaseException], ...],
    tuple[type[BaseException], ...],
]:
    disconnect_errors: list[type[BaseException]] = [
        BrokenPipeError,
        ConnectionResetError,
        EOFError,
    ]
    try:
        from Xlib import X, display, error
    except ImportError:
        return None, None, (OSError,), tuple(disconnect_errors)
    for name in ("ConnectionClosedError", "DisplayConnectionError"):
        err_cls = getattr(error, name, None)
        if isinstance(err_cls, type) and issubclass(err_cls, BaseException):
            disconnect_errors.append(err_cls)
    expected_errors = (
        OSError,
        cast(type[BaseException], error.DisplayError),
        cast(type[BaseException], error.XError),
        cast(type[BaseException], error.XauthError),
    )
    return X, display, expected_errors, tuple(disconnect_errors)


xlib_constants, xlib_display, _expected_x_errors, _disconnect_x_errors = _load_xlib()


def _is_expected_x_error(exc: BaseException) -> bool:
    return isinstance(exc, _expected_x_errors)


def _is_disconnect_error(exc: BaseException) -> bool:
    return isinstance(exc, _disconnect_x_errors)


def _log_x_failure(message: str, exc: BaseException) -> None:
    if _is_expected_x_error(exc):
        log.debug("%s: %s", message, exc)
        return
    log.exception(message)


def _handle_x_protocol_error(err: object, _request: object) -> None:
    log.debug("Ignored async Gamescope X11 protocol error: %s", err)


def _install_x_error_handler(disp: "_XDisplay") -> None:
    set_error_handler = getattr(disp, "set_error_handler", None)
    if callable(set_error_handler):
        set_error_handler(_handle_x_protocol_error)


class _XDisplayModule(Protocol):
    def Display(self, display: str) -> "_XDisplay": ...  # noqa: N802 - Xlib API


class _XScreen(Protocol):
    root: "_XWindow"


class _XTree(Protocol):
    children: Sequence["_XWindow"]


class _XProperty(Protocol):
    value: object


class _XPointer(Protocol):
    root_x: int
    root_y: int


class _XWindow(Protocol):
    id: int

    def change_attributes(self, *, event_mask: int) -> None: ...

    def get_full_property(self, atom: int, property_type: int) -> _XProperty | None: ...

    def get_wm_class(self) -> Sequence[object] | None: ...

    def get_wm_name(self) -> object: ...

    def query_pointer(self) -> _XPointer: ...

    def query_tree(self) -> _XTree: ...


class _XDisplay(Protocol):
    def screen(self) -> _XScreen: ...

    def intern_atom(self, name: str) -> int: ...

    def sync(self) -> None: ...

    def close(self) -> None: ...

    def fileno(self) -> int: ...

    def pending_events(self) -> int: ...

    def next_event(self) -> "_XEvent": ...

    def create_resource_object(self, resource_type: str, resource_id: int) -> _XWindow: ...


class _XEvent(Protocol):
    type: int
    window: _XWindow | None
    atom: int | None


@dataclass
class _DisplayConnection:
    name: str
    display: _XDisplay
    root: _XWindow
    fd: int
    atom_net_wm_name: int
    atom_wm_name: int
    atom_wm_class: int
    atom_net_wm_pid: int
    atom_steam_game: int


def has_gamescope_x11_support() -> bool:
    return xlib_display is not None


def _xdisplay_module() -> _XDisplayModule | None:
    if xlib_display is None:
        return None
    return cast(_XDisplayModule, xlib_display)


def _any_property_type() -> int:
    return int(getattr(xlib_constants, "AnyPropertyType", 0))


def _is_gamescope_session_env() -> bool:
    if os.environ.get("GAMESCOPE_WAYLAND_DISPLAY", "").strip():
        return True
    for var in GAMESCOPE_SESSION_ENV_VARS:
        if "gamescope" in os.environ.get(var, "").lower():
            return True
    return False


def _read_gamescope_environment_display() -> str | None:
    env_path = runtime_dir() / "gamescope-environment"
    try:
        text = env_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("DISPLAY="):
            value = line.split("=", 1)[1].strip()
            if value:
                return value
    return None


def _x11_socket_displays() -> list[str]:
    x11_dir = Path("/tmp/.X11-unix")
    if not x11_dir.exists():
        return []
    displays: list[str] = []
    for sock in sorted(x11_dir.glob("X*")):
        suffix = sock.name[1:]
        if suffix.isdigit():
            displays.append(f":{suffix}")
    return displays


def _sequence_values(raw: object) -> list[object]:
    if raw is None:
        return []
    tolist = getattr(raw, "tolist", None)
    if callable(tolist):
        converted = tolist()
        if isinstance(converted, list):
            return cast(list[object], converted)
    if isinstance(raw, (bytes, bytearray, str)):
        return [raw]
    if isinstance(raw, Sequence):
        return list(cast(Sequence[object], raw))
    return [raw]


def _extract_cardinal(prop: _XProperty | None) -> int | None:
    if prop is None:
        return None
    values = _sequence_values(prop.value)
    if not values:
        return None
    try:
        return int(cast(int | str | bytes | bytearray, values[0]))
    except (TypeError, ValueError):
        return None


def _is_x11_display_name(name: str) -> bool:
    return name.startswith(":") and len(name) >= 2


def _decode_display_name(raw: object) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()
    if isinstance(raw, str):
        return raw.split("\x00", 1)[0].strip()

    # Gamescope packs a NUL-terminated display string (e.g. ":1\0") into CARDINAL uint32s.
    values = _sequence_values(raw)
    if not values:
        return ""
    buf = bytearray()
    for item in values:
        try:
            word = int(cast(int | str, item))
        except (TypeError, ValueError):
            return ""
        if word < 0 or word > 0xFFFFFFFF:
            return ""
        buf.extend(struct.pack("=I", word))
    return bytes(buf).split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()


def _is_known_pid(pid: int | None) -> bool:
    return pid is not None and 0 < pid < 0xFFFFFFFF


def _parse_focusable_windows(raw: object) -> dict[int, list[tuple[int, int]]]:
    values = _sequence_values(raw)
    result: dict[int, list[tuple[int, int]]] = {}
    for index in range(0, len(values) - 2, 3):
        try:
            xid = int(cast(int | str, values[index]))
            app_id = int(cast(int | str, values[index + 1]))
            pid = int(cast(int | str, values[index + 2]))
        except (TypeError, ValueError):
            continue
        if xid > 0:
            entries = result.setdefault(xid, [])
            entry = (app_id, pid if _is_known_pid(pid) else 0)
            if entry not in entries:
                entries.append(entry)
    return result


def _read_window_class(win: _XWindow) -> str:
    try:
        class_data = win.get_wm_class()
        if class_data:
            return str(class_data[-1] or class_data[0] or "")
    except Exception as exc:  # noqa: BLE001 - Xlib metadata reads can fail broadly.
        if _is_disconnect_error(exc):
            raise
        _log_x_failure("Failed to read Gamescope window class", exc)
    return ""


def _read_window_title(win: _XWindow, net_wm_name_atom: int) -> str:
    try:
        net_name = win.get_full_property(net_wm_name_atom, _any_property_type())
        if net_name and net_name.value:
            raw = net_name.value
            if isinstance(raw, bytes):
                return raw.decode("utf-8", errors="replace")
            return str(raw)
    except Exception as exc:  # noqa: BLE001 - Xlib metadata reads can fail broadly.
        if _is_disconnect_error(exc):
            raise
        _log_x_failure("Failed to read Gamescope window title", exc)

    try:
        fallback = win.get_wm_name()
        if isinstance(fallback, bytes):
            return fallback.decode("utf-8", errors="replace")
        return str(fallback or "")
    except Exception as exc:  # noqa: BLE001 - Xlib metadata reads can fail broadly.
        if _is_disconnect_error(exc):
            raise
        _log_x_failure("Failed to read fallback Gamescope window title", exc)
    return ""


class GamescopeListener(WindowListener):
    def __init__(
        self,
        callback: WindowChangeCallback,
        client: object | None = None,
        dbus: SessionDBus | None = None,
    ) -> None:
        super().__init__(callback, client, dbus=dbus)
        self._x_lock = threading.RLock()
        self._control_display_name: str | None = None
        self._control: _DisplayConnection | None = None
        self._gamescope_pid: int | None = None
        self._siblings: dict[str, _DisplayConnection] = {}
        self._pending_close: list[_DisplayConnection] = []

        self._atom_gamescope_pid: int | None = None
        self._atom_focused_window: int | None = None
        self._atom_focused_app: int | None = None
        self._atom_focused_app_gfx: int | None = None
        self._atom_focusable_windows: int | None = None
        self._atom_focus_display: int | None = None
        self._atom_mouse_focus_display: int | None = None
        self._atom_keyboard_focus_display: int | None = None
        self._atom_steam_overlay: int | None = None
        self._atom_steam_input_focus: int | None = None
        self._atom_gamescope_no_focus: int | None = None

        self._root_watch_atoms: set[int] = set()
        self._overlay_watch_atoms: set[int] = set()
        self._watched_overlay_windows: dict[int, _XWindow] = {}

        self._active_watch_display: str | None = None
        self._active_watch_window_id: int | None = None
        self._active_watch_window: _XWindow | None = None

        self._fd_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._registered_readers: dict[int, _DisplayConnection] = {}

        self._active_layer: str = ""
        self._last_class: str = ""
        self._last_title: str = ""
        self._last_layer: str = ""

    @property
    def name(self) -> str:
        return "gamescope"

    @property
    def active_layer(self) -> str:
        return self._active_layer

    @property
    def supports_realtime_cursor_position(self) -> bool:
        return True

    @classmethod
    def _candidate_control_displays(cls) -> list[str]:
        env_display = os.environ.get("DISPLAY", "").strip()
        is_gamescope_env = _is_gamescope_session_env()
        # On a regular X11 desktop running a nested gamescope window, avoid scanning
        # unrelated /tmp/.X11-unix sockets unless session metadata indicates Gamescope.
        if env_display and not is_gamescope_env:
            return [env_display]

        file_display = _read_gamescope_environment_display()
        if not is_gamescope_env and not file_display:
            return []

        candidates: list[str] = []
        if env_display:
            candidates.append(env_display)
        if file_display:
            candidates.append(file_display)
        if is_gamescope_env:
            candidates.extend(_x11_socket_displays())

        dedup: list[str] = []
        for value in candidates:
            if value not in dedup:
                dedup.append(value)
        return dedup

    @classmethod
    def _probe_display_gamescope_meta(
        cls,
        display_name: str,
    ) -> tuple[bool, tuple[int, int | None] | None]:
        display_mod = _xdisplay_module()
        if display_mod is None or xlib_constants is None:
            return False, None
        disp: _XDisplay | None = None
        connected = False
        try:
            disp = display_mod.Display(display_name)
            connected = True
            _install_x_error_handler(disp)
            root = disp.screen().root
            pid_atom = disp.intern_atom("GAMESCOPE_PID")
            pid = _extract_cardinal(root.get_full_property(pid_atom, _any_property_type()))
            if not pid or pid <= 0:
                return True, None
            server_id_atom = disp.intern_atom("GAMESCOPE_XWAYLAND_SERVER_ID")
            server_id = _extract_cardinal(
                root.get_full_property(server_id_atom, _any_property_type())
            )
            return True, (pid, server_id)
        except Exception as exc:  # noqa: BLE001 - Xlib can raise backend-specific errors.
            _log_x_failure(f"Could not probe Gamescope display {display_name}", exc)
            return connected, None
        finally:
            if disp is not None:
                try:
                    disp.close()
                except Exception as exc:  # noqa: BLE001 - cleanup should not fail probes.
                    _log_x_failure(f"Could not close probed display {display_name}", exc)

    @classmethod
    def _inspect_display_gamescope_meta(cls, display_name: str) -> tuple[int, int | None] | None:
        _, meta = cls._probe_display_gamescope_meta(display_name)
        return meta

    @classmethod
    def pick_control_display(cls) -> tuple[str, int] | None:
        if not has_gamescope_x11_support():
            return None
        env_display = os.environ.get("DISPLAY", "").strip()
        if env_display and not _is_gamescope_session_env():
            connected, meta = cls._probe_display_gamescope_meta(env_display)
            if meta is not None:
                return env_display, meta[0]
            if connected:
                return None
            file_display = _read_gamescope_environment_display()
            if not file_display or file_display == env_display:
                return None
            _, file_meta = cls._probe_display_gamescope_meta(file_display)
            return (file_display, file_meta[0]) if file_meta is not None else None

        fallback: tuple[str, int] | None = None
        for display_name in cls._candidate_control_displays():
            meta = cls._inspect_display_gamescope_meta(display_name)
            if meta is None:
                continue
            pid, server_id = meta
            if server_id == 0:
                return display_name, pid
            if fallback is None:
                fallback = (display_name, pid)
        return fallback

    @classmethod
    async def probe_available(cls, dbus: SessionDBus | None = None) -> bool:
        _ = dbus
        return await asyncio.to_thread(cls.pick_control_display) is not None

    async def start(self) -> None:
        if not has_gamescope_x11_support():
            raise RuntimeError("Gamescope listener requires optional dependency 'python-xlib'")

        picked = await asyncio.to_thread(self.pick_control_display)
        if picked is None:
            raise RuntimeError("Gamescope listener requires an active Gamescope Xwayland display")

        self._control_display_name, self._gamescope_pid = picked
        await asyncio.to_thread(self._open_control_display)
        self.running = True
        self._task = asyncio.create_task(self._listen())
        log.info(
            "Gamescope listener started on %s (pid=%s)",
            self._control_display_name,
            self._gamescope_pid,
        )

    async def stop(self) -> None:
        self.running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        self._clear_loop_readers()
        await asyncio.to_thread(self._close_all_displays)
        self._active_layer = ""
        log.info("Gamescope listener stopped")

    async def health_check(self) -> bool:
        if not await super().health_check():
            return False
        alive = await asyncio.to_thread(self._check_control_alive)
        if alive:
            await self._wake_if_buffered_events()
        return alive

    async def get_active_window(self) -> tuple[str, str, list[str]]:
        try:
            window_class, window_title, layer = await asyncio.to_thread(self._query_active_state)
            self._active_layer = layer
            await self._sync_loop_readers()
            await self._wake_if_buffered_events()
            return window_class, window_title, []
        except Exception as exc:  # noqa: BLE001 - keep active-window fallback alive.
            _log_x_failure("Gamescope active window query failed", exc)
            return "", "", []

    async def get_cursor_position(self) -> tuple[int, int] | None:
        try:
            pos = await asyncio.to_thread(self._query_cursor_position)
            await self._sync_loop_readers()
            await self._wake_if_buffered_events()
            return pos
        except Exception as exc:  # noqa: BLE001 - keep cursor query fallback alive.
            _log_x_failure("Gamescope cursor query failed", exc)
            return None

    async def _listen(self) -> None:
        if self._control is None:
            return

        self._loop = asyncio.get_running_loop()
        self._fd_event = asyncio.Event()

        try:
            await self._sync_loop_readers()
            await self._emit_active_window_if_changed()

            skip_buffered_check = False
            while self.running:
                if not self._fd_event.is_set():
                    has_buffered = (
                        False
                        if skip_buffered_check
                        else await asyncio.to_thread(self._has_buffered_events)
                    )
                    if not has_buffered:
                        await self._fd_event.wait()
                self._fd_event.clear()

                changed, drain_failed = await asyncio.to_thread(self._drain_events)
                skip_buffered_check = drain_failed
                await self._sync_loop_readers()
                if changed:
                    await self._emit_active_window_if_changed()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - listener task must log and stop cleanly.
            _log_x_failure("Gamescope listener error", exc)
        finally:
            self._clear_loop_readers()
            self._fd_event = None
            self._loop = None

    def _on_display_readable(self) -> None:
        if self._fd_event and not self._fd_event.is_set():
            self._fd_event.set()

    def _has_buffered_events(self) -> bool:
        with self._x_lock:
            conns: list[_DisplayConnection] = []
            if self._control is not None:
                conns.append(self._control)
            conns.extend(self._siblings.values())
            for conn in conns:
                try:
                    if conn.display.pending_events() > 0:
                        return True
                except Exception as exc:  # noqa: BLE001 - ignore dead display while checking queue.
                    _log_x_failure(f"Failed to check Gamescope event queue on {conn.name}", exc)
            return False

    async def _wake_if_buffered_events(self) -> None:
        if self._fd_event is None or self._fd_event.is_set():
            return
        if await asyncio.to_thread(self._has_buffered_events):
            self._fd_event.set()

    async def _sync_loop_readers(self) -> None:
        loop = self._loop
        if loop is None:
            await asyncio.to_thread(self._close_pending_displays)
            return

        current_by_fd = await asyncio.to_thread(self._get_open_connections)
        for fd, prev_conn in list(self._registered_readers.items()):
            if current_by_fd.get(fd) is not prev_conn:
                try:
                    loop.remove_reader(fd)
                except (RuntimeError, ValueError):
                    log.debug("Failed to remove Gamescope display reader fd=%s", fd, exc_info=True)
                self._registered_readers.pop(fd, None)

        await asyncio.to_thread(self._close_pending_displays)

        for fd, conn in current_by_fd.items():
            if fd >= 0 and self._registered_readers.get(fd) is not conn:
                loop.add_reader(fd, self._on_display_readable)
                self._registered_readers[fd] = conn

    def _clear_loop_readers(self) -> None:
        loop = self._loop
        if loop is not None:
            for fd in list(self._registered_readers):
                try:
                    loop.remove_reader(fd)
                except (RuntimeError, ValueError):
                    log.debug("Failed to remove Gamescope display reader fd=%s", fd, exc_info=True)
        self._registered_readers.clear()

    def _get_open_connections(self) -> dict[int, _DisplayConnection]:
        with self._x_lock:
            connections: dict[int, _DisplayConnection] = {}
            if self._control is not None and self._control.fd >= 0:
                connections[self._control.fd] = self._control
            for conn in self._siblings.values():
                if conn.fd >= 0:
                    connections[conn.fd] = conn
            return connections

    def _close_pending_displays(self) -> None:
        with self._x_lock:
            pending = self._pending_close
            self._pending_close = []
        for conn in pending:
            try:
                conn.display.close()
            except Exception as exc:  # noqa: BLE001 - cleanup should not fail loop sync.
                _log_x_failure(f"Failed to close Gamescope sibling display {conn.name}", exc)

    def _open_connection_unlocked(self, display_name: str) -> _DisplayConnection:
        display_mod = _xdisplay_module()
        if display_mod is None or xlib_constants is None:
            raise RuntimeError("python-xlib is unavailable")
        disp = display_mod.Display(display_name)
        _install_x_error_handler(disp)
        root = disp.screen().root
        return _DisplayConnection(
            name=display_name,
            display=disp,
            root=root,
            fd=int(disp.fileno()),
            atom_net_wm_name=disp.intern_atom("_NET_WM_NAME"),
            atom_wm_name=disp.intern_atom("WM_NAME"),
            atom_wm_class=disp.intern_atom("WM_CLASS"),
            atom_net_wm_pid=disp.intern_atom("_NET_WM_PID"),
            atom_steam_game=disp.intern_atom("STEAM_GAME"),
        )

    def _open_control_display(self) -> None:
        if xlib_constants is None:
            raise RuntimeError("python-xlib is unavailable")
        if not self._control_display_name:
            raise RuntimeError("Gamescope control display name is not set")

        with self._x_lock:
            conn = self._open_connection_unlocked(self._control_display_name)
            disp = conn.display
            self._control = conn

            self._atom_gamescope_pid = disp.intern_atom("GAMESCOPE_PID")
            self._atom_focused_window = disp.intern_atom("GAMESCOPE_FOCUSED_WINDOW")
            self._atom_focused_app = disp.intern_atom("GAMESCOPE_FOCUSED_APP")
            self._atom_focused_app_gfx = disp.intern_atom("GAMESCOPE_FOCUSED_APP_GFX")
            self._atom_focusable_windows = disp.intern_atom("GAMESCOPE_FOCUSABLE_WINDOWS")
            self._atom_focus_display = disp.intern_atom("GAMESCOPE_FOCUS_DISPLAY")
            self._atom_mouse_focus_display = disp.intern_atom("GAMESCOPE_MOUSE_FOCUS_DISPLAY")
            self._atom_keyboard_focus_display = disp.intern_atom(
                "GAMESCOPE_KEYBOARD_FOCUS_DISPLAY"
            )
            self._atom_steam_overlay = disp.intern_atom("STEAM_OVERLAY")
            self._atom_steam_input_focus = disp.intern_atom("STEAM_INPUT_FOCUS")
            self._atom_gamescope_no_focus = disp.intern_atom("GAMESCOPE_NO_FOCUS")

            self._root_watch_atoms = {
                self._atom_focused_window,
                self._atom_focused_app,
                self._atom_focused_app_gfx,
                self._atom_focusable_windows,
                self._atom_focus_display,
                self._atom_mouse_focus_display,
                self._atom_keyboard_focus_display,
            }
            self._overlay_watch_atoms = {
                self._atom_steam_overlay,
                self._atom_steam_input_focus,
                self._atom_gamescope_no_focus,
            }

            event_mask = int(getattr(xlib_constants, "PropertyChangeMask", 0)) | int(
                getattr(xlib_constants, "SubstructureNotifyMask", 0)
            )
            conn.root.change_attributes(event_mask=event_mask)
            self._refresh_overlay_watches_unlocked()
            disp.sync()

    def _close_all_displays(self) -> None:
        with self._x_lock:
            self._clear_active_window_watch_unlocked()
            self._watched_overlay_windows.clear()

            connections: list[_DisplayConnection] = list(self._pending_close)
            self._pending_close = []
            connections.extend(self._siblings.values())
            self._siblings.clear()
            if self._control is not None:
                connections.append(self._control)
                self._control = None

            self._root_watch_atoms = set()
            self._overlay_watch_atoms = set()

        for conn in connections:
            try:
                conn.display.close()
            except Exception as exc:  # noqa: BLE001 - cleanup should not block shutdown.
                _log_x_failure(f"Failed to close Gamescope display {conn.name}", exc)

    def _check_control_alive(self) -> bool:
        with self._x_lock:
            if self._control is None or self._atom_gamescope_pid is None:
                return False
            try:
                pid = _extract_cardinal(
                    self._control.root.get_full_property(
                        self._atom_gamescope_pid,
                        _any_property_type(),
                    )
                )
                return bool(pid and (self._gamescope_pid is None or pid == self._gamescope_pid))
            except Exception as exc:  # noqa: BLE001 - health checks must fail closed.
                _log_x_failure("Gamescope health check failed", exc)
                return False

    def _get_or_open_display_unlocked(self, display_name: str) -> _DisplayConnection | None:
        if not display_name:
            return None
        if self._control is not None and display_name == self._control.name:
            return self._control
        existing = self._siblings.get(display_name)
        if existing is not None:
            return existing
        try:
            conn = self._open_connection_unlocked(display_name)
            pid_atom = conn.display.intern_atom("GAMESCOPE_PID")
            pid = _extract_cardinal(conn.root.get_full_property(pid_atom, _any_property_type()))
            if self._gamescope_pid is not None and pid != self._gamescope_pid:
                conn.display.close()
                return None
            self._siblings[display_name] = conn
            return conn
        except Exception as exc:  # noqa: BLE001 - sibling Xwayland servers may come and go.
            _log_x_failure(f"Could not open Gamescope sibling display {display_name}", exc)
            return None

    def _drop_sibling_unlocked(self, display_name: str) -> None:
        conn = self._siblings.pop(display_name, None)
        if conn is not None:
            if self._active_watch_display == display_name:
                self._active_watch_display = None
                self._active_watch_window_id = None
                self._active_watch_window = None
            self._pending_close.append(conn)

    def _maybe_watch_overlay_window_unlocked(self, win: _XWindow) -> None:
        if (
            xlib_constants is None
            or self._control is None
            or self._atom_steam_overlay is None
            or self._atom_steam_input_focus is None
        ):
            return
        if win.id in self._watched_overlay_windows:
            return
        try:
            has_overlay = (
                win.get_full_property(self._atom_steam_overlay, _any_property_type()) is not None
            )
            has_input_focus = (
                win.get_full_property(self._atom_steam_input_focus, _any_property_type())
                is not None
            )
            steam_game = _extract_cardinal(
                win.get_full_property(self._control.atom_steam_game, _any_property_type())
            )
            if has_overlay or has_input_focus or steam_game == STEAM_APP_ID:
                win.change_attributes(
                    event_mask=int(getattr(xlib_constants, "PropertyChangeMask", 0))
                )
                self._watched_overlay_windows[win.id] = win
        except Exception as exc:  # noqa: BLE001 - transient windows vanish quickly.
            _log_x_failure("Failed to inspect Gamescope overlay candidate window", exc)

    def _refresh_overlay_watches_unlocked(self) -> None:
        if self._control is None:
            return
        try:
            children = self._control.root.query_tree().children
        except Exception as exc:  # noqa: BLE001 - root query can fail if server exits.
            _log_x_failure("Failed to query Gamescope root children", exc)
            return
        live_ids = {child.id for child in children}
        for stale_id in list(self._watched_overlay_windows):
            if stale_id not in live_ids:
                self._watched_overlay_windows.pop(stale_id, None)
        for child in children:
            self._maybe_watch_overlay_window_unlocked(child)

    def _clear_active_window_watch_unlocked(self) -> None:
        if xlib_constants is None:
            return
        if (
            self._active_watch_window is not None
            and self._active_watch_window_id not in self._watched_overlay_windows
        ):
            try:
                self._active_watch_window.change_attributes(
                    event_mask=int(getattr(xlib_constants, "NoEventMask", 0))
                )
            except Exception as exc:  # noqa: BLE001 - closed windows ignore mask clears.
                _log_x_failure("Failed to clear Gamescope active window event mask", exc)
        self._active_watch_display = None
        self._active_watch_window_id = None
        self._active_watch_window = None

    def _sync_active_window_watch_unlocked(
        self,
        conn: _DisplayConnection | None,
        window_id: int | None,
    ) -> None:
        if xlib_constants is None:
            return
        next_display = conn.name if conn is not None else None
        if next_display == self._active_watch_display and window_id == self._active_watch_window_id:
            return

        self._clear_active_window_watch_unlocked()
        if conn is None or not window_id:
            return

        try:
            win = conn.display.create_resource_object("window", window_id)
            win.change_attributes(event_mask=int(getattr(xlib_constants, "PropertyChangeMask", 0)))
            conn.display.sync()
            self._active_watch_display = conn.name
            self._active_watch_window_id = window_id
            self._active_watch_window = win
        except Exception as exc:  # noqa: BLE001 - window may have closed concurrently.
            _log_x_failure("Failed to watch Gamescope active window", exc)

    def _drain_events(self) -> tuple[bool, bool]:
        with self._x_lock:
            changed = False
            had_error = False
            if self._control is not None:
                ctrl_changed, ctrl_error = self._drain_display_unlocked(
                    self._control, is_control=True
                )
                changed = ctrl_changed or changed
                had_error = ctrl_error or had_error
            for display_name in list(self._siblings):
                conn = self._siblings.get(display_name)
                if conn is None:
                    continue
                try:
                    sib_changed, _ = self._drain_display_unlocked(conn, is_control=False)
                    if sib_changed:
                        changed = True
                except Exception as exc:  # noqa: BLE001 - dead sibling Xwayland server.
                    _log_x_failure(f"Gamescope sibling display {display_name} disconnected", exc)
                    self._drop_sibling_unlocked(display_name)
                    changed = True
            return changed, had_error

    def _drain_display_unlocked(
        self,
        conn: _DisplayConnection,
        *,
        is_control: bool,
    ) -> tuple[bool, bool]:
        changed = False
        try:
            while conn.display.pending_events() > 0:
                event = conn.display.next_event()
                if self._handle_x_event_unlocked(conn, event, is_control=is_control):
                    changed = True
        except Exception as exc:  # noqa: BLE001 - Xlib event drains can fail broadly.
            if not is_control:
                raise
            _log_x_failure("Failed to drain Gamescope control events", exc)
            return False, True
        return changed, False

    def _handle_x_event_unlocked(
        self,
        conn: _DisplayConnection,
        event: _XEvent,
        *,
        is_control: bool,
    ) -> bool:
        if xlib_constants is None:
            return False

        event_type = event.type
        prop_notify = int(getattr(xlib_constants, "PropertyNotify", -1))
        create_notify = int(getattr(xlib_constants, "CreateNotify", -2))
        map_notify = int(getattr(xlib_constants, "MapNotify", -3))
        destroy_notify = int(getattr(xlib_constants, "DestroyNotify", -4))

        if is_control and event_type in (create_notify, map_notify):
            if event.window is not None:
                self._maybe_watch_overlay_window_unlocked(event.window)
            return event_type == map_notify

        if is_control and event_type == destroy_notify:
            if event.window is not None:
                self._watched_overlay_windows.pop(event.window.id, None)
            return False

        if event_type != prop_notify:
            return False

        event_window = event.window
        event_window_id = event_window.id if event_window is not None else None
        event_atom = event.atom

        if is_control and event_window_id == conn.root.id:
            return bool(event_atom in self._root_watch_atoms)

        if is_control and event_window_id in self._watched_overlay_windows:
            if event_atom in self._overlay_watch_atoms:
                return True

        if (
            self._active_watch_display == conn.name
            and self._active_watch_window_id is not None
            and event_window_id == self._active_watch_window_id
        ):
            return event_atom in {
                conn.atom_net_wm_name,
                conn.atom_wm_name,
                conn.atom_wm_class,
            }

        return False

    async def _emit_active_window_if_changed(self) -> None:
        window_class, window_title, layer = await asyncio.to_thread(self._query_active_state)
        await self._sync_loop_readers()
        if (
            window_class == self._last_class
            and window_title == self._last_title
            and layer == self._last_layer
        ):
            return

        self._last_class = window_class
        self._last_title = window_title
        self._last_layer = layer
        self._active_layer = layer
        await self.callback(window_class, window_title, [])

    def _read_control_root_cardinal_unlocked(self, atom: int | None) -> int | None:
        if self._control is None or atom is None:
            return None
        return _extract_cardinal(self._control.root.get_full_property(atom, _any_property_type()))

    def _read_control_root_display_unlocked(self, atom: int | None) -> str:
        if self._control is None or atom is None:
            return ""
        prop = self._control.root.get_full_property(atom, _any_property_type())
        return _decode_display_name(prop.value) if prop is not None else ""

    def _detect_focused_overlay_unlocked(
        self,
        *,
        focused_app: int | None,
        focused_app_gfx: int | None,
        focus_display: str,
        mouse_display: str,
        keyboard_display: str,
    ) -> str:
        if (
            self._atom_steam_overlay is not None
            and self._atom_steam_input_focus is not None
            and self._atom_gamescope_no_focus is not None
        ):
            for win_id, win in list(self._watched_overlay_windows.items()):
                try:
                    no_focus = _extract_cardinal(
                        win.get_full_property(self._atom_gamescope_no_focus, _any_property_type())
                    )
                    if no_focus == 1:
                        continue
                    steam_overlay = _extract_cardinal(
                        win.get_full_property(self._atom_steam_overlay, _any_property_type())
                    )
                    steam_input_focus = _extract_cardinal(
                        win.get_full_property(self._atom_steam_input_focus, _any_property_type())
                    )
                except Exception as exc:  # noqa: BLE001 - closed window drops out cleanly.
                    _log_x_failure("Failed to read Gamescope overlay window properties", exc)
                    self._watched_overlay_windows.pop(win_id, None)
                    continue
                if steam_overlay == 1 and steam_input_focus:
                    return STEAM_OSK_LAYER if steam_input_focus == 2 else STEAM_OVERLAY_LAYER

        app_diverged = bool(focused_app and focused_app_gfx and focused_app != focused_app_gfx)
        mouse_diverged = bool(focus_display and mouse_display and mouse_display != focus_display)
        kbd_diverged = bool(
            focus_display and keyboard_display and keyboard_display != focus_display
        )
        if not (app_diverged or mouse_diverged or kbd_diverged):
            return ""

        control_name = self._control.name if self._control is not None else ""
        if focused_app == STEAM_APP_ID or (
            not focused_app and mouse_display and mouse_display == control_name
        ):
            if mouse_diverged and not kbd_diverged:
                return STEAM_OSK_LAYER
            return STEAM_OVERLAY_LAYER
        if focused_app:
            return f"overlay-app-{focused_app}"
        return "overlay"

    def _window_matches_expected_unlocked(
        self,
        conn: _DisplayConnection,
        window_id: int,
        expected_entries: Sequence[tuple[int, int]],
        *,
        expected_app: int | None = None,
    ) -> tuple[_XWindow, str, str] | None:
        try:
            win = conn.display.create_resource_object("window", window_id)
            wm_class = _read_window_class(win)
            title = _read_window_title(win, conn.atom_net_wm_name)
            win_pid = _extract_cardinal(
                win.get_full_property(conn.atom_net_wm_pid, _any_property_type())
            )
            win_app = _extract_cardinal(
                win.get_full_property(conn.atom_steam_game, _any_property_type())
            )
            if expected_app and win_app and win_app != expected_app:
                return None
            win_pid_known = _is_known_pid(win_pid)
            if expected_entries:
                matched = any(
                    (not _is_known_pid(exp_pid) or not win_pid_known or win_pid == exp_pid)
                    and (not exp_app or not win_app or win_app == exp_app)
                    for exp_app, exp_pid in expected_entries
                )
                if not matched:
                    return None
            if not wm_class and not title and win_pid is None and win_app is None:
                return None
            return win, wm_class, title
        except Exception as exc:  # noqa: BLE001 - XID may belong to a different Xwayland server.
            _log_x_failure(f"Window {hex(window_id)} not readable on {conn.name}", exc)
            if _is_disconnect_error(exc) and conn is not self._control:
                self._drop_sibling_unlocked(conn.name)
            return None

    def _resolve_focused_window_unlocked(
        self,
        window_id: int | None,
        focus_display: str,
        focusable_windows: dict[int, list[tuple[int, int]]],
        *,
        focused_app: int | None = None,
    ) -> tuple[_DisplayConnection | None, str, str]:
        if not window_id:
            return None, "", ""

        all_expected = focusable_windows.get(window_id, [])
        if focused_app and all_expected:
            app_filtered = [entry for entry in all_expected if entry[0] == focused_app]
            expected_entries = app_filtered or all_expected
        else:
            expected_entries = all_expected

        if _is_x11_display_name(focus_display):
            focused_conn = self._get_or_open_display_unlocked(focus_display)
            if focused_conn is not None:
                resolved = self._window_matches_expected_unlocked(
                    focused_conn,
                    window_id,
                    expected_entries,
                    expected_app=focused_app,
                )
                if resolved is not None:
                    _win, wm_class, title = resolved
                    return focused_conn, wm_class, title

        candidate_names: list[str] = []
        if self._control is not None:
            candidate_names.append(self._control.name)
        for name in list(self._siblings):
            if name not in candidate_names:
                candidate_names.append(name)
        for name in _x11_socket_displays():
            if name not in candidate_names:
                candidate_names.append(name)

        for display_name in candidate_names:
            if display_name == focus_display:
                continue
            conn = self._get_or_open_display_unlocked(display_name)
            if conn is None:
                continue
            resolved = self._window_matches_expected_unlocked(
                conn,
                window_id,
                expected_entries,
                expected_app=focused_app,
            )
            if resolved is not None:
                _win, wm_class, title = resolved
                return conn, wm_class, title
        return None, "", ""

    def _query_active_state(self) -> tuple[str, str, str]:
        with self._x_lock:
            if self._control is None or xlib_constants is None:
                return "", "", ""

            try:
                focused_window_id = self._read_control_root_cardinal_unlocked(
                    self._atom_focused_window
                )
                focused_app = self._read_control_root_cardinal_unlocked(self._atom_focused_app)
                focused_app_gfx = self._read_control_root_cardinal_unlocked(
                    self._atom_focused_app_gfx
                )
                focus_display = self._read_control_root_display_unlocked(self._atom_focus_display)
                mouse_display = self._read_control_root_display_unlocked(
                    self._atom_mouse_focus_display
                )
                keyboard_display = self._read_control_root_display_unlocked(
                    self._atom_keyboard_focus_display
                )

                overlay_layer = self._detect_focused_overlay_unlocked(
                    focused_app=focused_app,
                    focused_app_gfx=focused_app_gfx,
                    focus_display=focus_display,
                    mouse_display=mouse_display,
                    keyboard_display=keyboard_display,
                )
                if overlay_layer:
                    self._clear_active_window_watch_unlocked()
                    return "", "", overlay_layer

                focusable_prop = (
                    self._control.root.get_full_property(
                        self._atom_focusable_windows,
                        _any_property_type(),
                    )
                    if self._atom_focusable_windows is not None
                    else None
                )
                focusable_windows = (
                    _parse_focusable_windows(focusable_prop.value)
                    if focusable_prop is not None
                    else {}
                )

                conn, wm_class, title = self._resolve_focused_window_unlocked(
                    focused_window_id,
                    focus_display,
                    focusable_windows,
                    focused_app=focused_app,
                )
                self._sync_active_window_watch_unlocked(conn, focused_window_id)

                if not wm_class and focused_app and focused_app > 0:
                    wm_class = (
                        "steam" if focused_app == STEAM_APP_ID else f"steam_app_{focused_app}"
                    )

                return wm_class, title, ""
            except Exception as exc:  # noqa: BLE001 - keep active-window fallback alive.
                _log_x_failure("Failed to query Gamescope active state", exc)
                return "", "", ""

    def _query_cursor_position(self) -> tuple[int, int] | None:
        with self._x_lock:
            if self._control is None:
                return None
            target_display = self._read_control_root_display_unlocked(
                self._atom_mouse_focus_display
            ) or self._read_control_root_display_unlocked(self._atom_focus_display)
            if target_display:
                if not _is_x11_display_name(target_display):
                    return None
                conn = self._get_or_open_display_unlocked(target_display)
                if conn is None:
                    return None
            else:
                conn = self._control
            try:
                pointer = conn.root.query_pointer()
                return int(pointer.root_x), int(pointer.root_y)
            except (TypeError, ValueError):
                return None
            except Exception as exc:  # noqa: BLE001 - Xlib cursor query can fail broadly.
                _log_x_failure("Failed to query Gamescope cursor position", exc)
                if conn is not self._control:
                    self._drop_sibling_unlocked(conn.name)
                return None
