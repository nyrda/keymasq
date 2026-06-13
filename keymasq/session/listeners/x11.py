import asyncio
import logging
import os
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, cast

from keymasq.session.dbus import SessionDBus
from keymasq.session.listeners.base import WindowChangeCallback, WindowListener

log = logging.getLogger("keymasq-session.listeners.x11")

_x_module = None
_expected_x_errors: tuple[type[BaseException], ...] = (OSError,)
xdisplay: object | None
try:
    from Xlib import X as XLIB_X
    from Xlib import display
    from Xlib import error as xerror
except ImportError:
    xdisplay = None
else:
    _x_module = XLIB_X
    xdisplay = display
    _expected_x_errors = (
        OSError,
        cast(type[BaseException], xerror.DisplayError),
        cast(type[BaseException], xerror.XError),
        cast(type[BaseException], xerror.XauthError),
    )

X = _x_module


def _is_expected_x_error(exc: BaseException) -> bool:
    return isinstance(exc, _expected_x_errors)


def _log_x_failure(message: str, exc: BaseException) -> None:
    if _is_expected_x_error(exc):
        log.debug("%s: %s", message, exc)
        return
    log.exception(message)


class _XDisplayModule(Protocol):
    def Display(self, display: str) -> "_XDisplay": ...  # noqa: N802 - Xlib API


class _XScreen(Protocol):
    root: "_XWindow"


class _XDisplay(Protocol):
    def screen(self) -> _XScreen: ...

    def intern_atom(self, name: str) -> int: ...

    def sync(self) -> None: ...

    def close(self) -> None: ...

    def fileno(self) -> int: ...

    def pending_events(self) -> int: ...

    def next_event(self) -> "_XEvent": ...

    def create_resource_object(self, resource_type: str, resource_id: int) -> "_XWindow": ...


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


class _XEvent(Protocol):
    type: int
    window: _XWindow | None
    atom: int | None


def has_x11_support() -> bool:
    return xdisplay is not None


def _xdisplay_module() -> _XDisplayModule | None:
    if xdisplay is None:
        return None
    return cast(_XDisplayModule, xdisplay)


def _first_property_value(prop: _XProperty | None) -> object | None:
    if prop is None:
        return None
    value = prop.value
    if isinstance(value, Sequence):
        sequence = cast(Sequence[object], value)
        if not sequence:
            return None
        return sequence[0]
    return value


class X11Listener(WindowListener):
    def __init__(
        self,
        callback: WindowChangeCallback,
        client: object | None = None,
        dbus: SessionDBus | None = None,
    ) -> None:
        super().__init__(callback, client, dbus=dbus)
        self._xdisplay: _XDisplay | None = None
        self._root: _XWindow | None = None
        self._atom_active: int | None = None
        self._atom_net_wm_name: int | None = None
        self._atom_wm_name: int | None = None
        self._atom_wm_class: int | None = None
        self._window_watch_atoms: set[int] = set()
        self._active_window_id: int | None = None
        self._active_window: _XWindow | None = None
        self._fd_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._display_fd: int | None = None
        self._last_class = ""
        self._last_title = ""
        self._x_lock = threading.RLock()
        self._display_name: str | None = None

    @property
    def name(self) -> str:
        return "x11"

    @classmethod
    def _candidate_displays(cls) -> list[str]:
        candidates: list[str] = []
        env_display = os.environ.get("DISPLAY")
        if env_display:
            candidates.append(env_display)

        x11_dir = Path("/tmp/.X11-unix")
        if x11_dir.exists():
            for sock in sorted(x11_dir.glob("X*")):
                suffix = sock.name[1:]
                if suffix.isdigit():
                    candidates.append(f":{suffix}")

        dedup: list[str] = []
        for value in candidates:
            if value not in dedup:
                dedup.append(value)
        return dedup

    @classmethod
    def _can_open_display(cls, display_name: str) -> bool:
        display_mod = _xdisplay_module()
        if display_mod is None:
            return False
        try:
            disp = display_mod.Display(display_name)
            disp.close()
            return True
        except Exception as exc:  # noqa: BLE001 - Xlib can raise backend-specific errors.
            _log_x_failure(f"Could not open X11 display {display_name}", exc)
            return False

    @classmethod
    def pick_display_name(cls) -> str | None:
        if not has_x11_support():
            return None
        for display_name in cls._candidate_displays():
            if cls._can_open_display(display_name):
                return display_name
        return None

    @classmethod
    async def probe_available(cls, dbus: SessionDBus | None = None) -> bool:
        _ = dbus
        return await asyncio.to_thread(cls.pick_display_name) is not None

    async def start(self) -> None:
        if not has_x11_support():
            raise RuntimeError("X11 listener requires optional dependency 'python-xlib'")

        self._display_name = self.pick_display_name()
        if not self._display_name:
            raise RuntimeError("X11 listener requires an active X display")

        await asyncio.to_thread(self._open_display)
        self.running = True
        self._task = asyncio.create_task(self._listen())
        log.info("X11 listener started")

    async def stop(self) -> None:
        self.running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        await asyncio.to_thread(self._close_display)
        log.info("X11 listener stopped")

    async def get_active_window(self) -> tuple[str, str, list[str]]:
        try:
            return await asyncio.to_thread(self._query_active_window)
        except Exception as exc:  # noqa: BLE001 - keep the listener query fallback alive.
            _log_x_failure("X11 active window query failed", exc)
            return "", "", []

    async def get_cursor_position(self) -> tuple[int, int] | None:
        try:
            return await asyncio.to_thread(self._query_cursor_position)
        except Exception as exc:  # noqa: BLE001 - keep the cursor query fallback alive.
            _log_x_failure("X11 cursor get failed", exc)
            return None

    @property
    def supports_realtime_cursor_position(self) -> bool:
        return True

    async def _listen(self) -> None:
        if self._xdisplay is None:
            return

        self._loop = asyncio.get_running_loop()
        self._fd_event = asyncio.Event()

        try:
            self._display_fd = await asyncio.to_thread(self._get_display_fd)
            if self._display_fd is not None and self._display_fd >= 0:
                self._loop.add_reader(self._display_fd, self._on_display_readable)

            await self._emit_active_window_if_changed()

            while self.running:
                await self._fd_event.wait()
                self._fd_event.clear()

                changed = await asyncio.to_thread(self._drain_events)
                if changed:
                    await self._emit_active_window_if_changed()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - listener task must log and stop cleanly.
            _log_x_failure("X11 listener error", exc)
        finally:
            if self._loop and self._display_fd is not None:
                try:
                    self._loop.remove_reader(self._display_fd)
                except (RuntimeError, ValueError):
                    log.debug("Failed to remove X11 display reader", exc_info=True)
            self._fd_event = None
            self._loop = None
            self._display_fd = None

    def _on_display_readable(self) -> None:
        if self._fd_event and not self._fd_event.is_set():
            self._fd_event.set()

    def _open_display(self) -> None:
        display_mod = _xdisplay_module()
        if display_mod is None or X is None:
            raise RuntimeError("python-xlib is unavailable")
        if not self._display_name:
            raise RuntimeError("X11 display name is not set")

        with self._x_lock:
            self._xdisplay = display_mod.Display(self._display_name)
            self._root = self._xdisplay.screen().root
            self._atom_active = self._xdisplay.intern_atom("_NET_ACTIVE_WINDOW")
            self._atom_net_wm_name = self._xdisplay.intern_atom("_NET_WM_NAME")
            self._atom_wm_name = self._xdisplay.intern_atom("WM_NAME")
            self._atom_wm_class = self._xdisplay.intern_atom("WM_CLASS")

            self._window_watch_atoms = {
                self._atom_net_wm_name,
                self._atom_wm_name,
                self._atom_wm_class,
            }

            self._root.change_attributes(event_mask=int(getattr(X, "PropertyChangeMask", 0)))
            self._sync_active_window_watch_unlocked()
            self._xdisplay.sync()

    def _close_display(self) -> None:
        if X is None:
            return

        with self._x_lock:
            if self._xdisplay is None:
                return

            if self._active_window is not None:
                try:
                    self._active_window.change_attributes(
                        event_mask=int(getattr(X, "NoEventMask", 0))
                    )
                except Exception as exc:  # noqa: BLE001 - cleanup should not block shutdown.
                    _log_x_failure("Failed to clear X11 active window event mask", exc)

            try:
                self._xdisplay.close()
            except Exception as exc:  # noqa: BLE001 - cleanup should not block shutdown.
                _log_x_failure("Failed to close X11 display", exc)

            self._xdisplay = None
            self._root = None
            self._atom_active = None
            self._atom_net_wm_name = None
            self._atom_wm_name = None
            self._atom_wm_class = None
            self._window_watch_atoms = set()
            self._active_window_id = None
            self._active_window = None

    async def health_check(self) -> bool:
        if not await super().health_check():
            return False
        return await self.__class__.probe_available()

    def _get_display_fd(self) -> int | None:
        with self._x_lock:
            if self._xdisplay is None:
                return None
            return int(self._xdisplay.fileno())

    def _drain_events(self) -> bool:
        with self._x_lock:
            if self._xdisplay is None:
                return False

            changed = False
            try:
                while self._xdisplay.pending_events() > 0:
                    event = self._xdisplay.next_event()
                    if self._handle_x_event_unlocked(event):
                        changed = True
            except Exception as exc:  # noqa: BLE001 - Xlib event drains can fail broadly.
                _log_x_failure("Failed to drain X11 events", exc)
                return False

            return changed

    def _handle_x_event_unlocked(self, event: _XEvent) -> bool:
        if X is None or event.type != int(getattr(X, "PropertyNotify", -1)):
            return False

        event_window = event.window
        event_window_id = event_window.id if event_window is not None else None
        event_atom = event.atom

        if (
            self._root is not None
            and event_window_id == self._root.id
            and event_atom == self._atom_active
        ):
            self._sync_active_window_watch_unlocked()
            return True

        if self._active_window_id is None:
            return False

        if event_window_id != self._active_window_id:
            return False

        return bool(event_atom in self._window_watch_atoms)

    def _sync_active_window_watch_unlocked(self) -> None:
        if X is None or self._xdisplay is None:
            return

        next_window_id = self._query_active_window_id_unlocked()
        if next_window_id == self._active_window_id:
            return

        if self._active_window is not None:
            try:
                self._active_window.change_attributes(
                    event_mask=int(getattr(X, "NoEventMask", 0))
                )
            except Exception as exc:  # noqa: BLE001 - cleanup should not block watch updates.
                _log_x_failure("Failed to clear previous X11 active window event mask", exc)

        self._active_window = None
        self._active_window_id = None

        if not next_window_id:
            return

        try:
            win = self._xdisplay.create_resource_object("window", next_window_id)
            win.change_attributes(event_mask=int(getattr(X, "PropertyChangeMask", 0)))
            self._active_window = win
            self._active_window_id = next_window_id
            self._xdisplay.sync()
        except Exception as exc:  # noqa: BLE001 - Xlib resource creation can fail broadly.
            _log_x_failure("Failed to watch X11 active window", exc)
            self._active_window = None
            self._active_window_id = None

    async def _emit_active_window_if_changed(self) -> None:
        window_class, window_title, tags = await asyncio.to_thread(self._query_active_window)
        if window_class == self._last_class and window_title == self._last_title:
            return

        self._last_class = window_class
        self._last_title = window_title
        await self.callback(window_class, window_title, tags)

    def _query_active_window_id_unlocked(self) -> int | None:
        if X is None or self._root is None or self._atom_active is None:
            return None

        prop = self._root.get_full_property(
            self._atom_active,
            int(getattr(X, "AnyPropertyType", 0)),
        )
        raw_window_id = _first_property_value(prop)
        if not raw_window_id:
            return None

        try:
            window_id = int(cast(int | str | bytes | bytearray, raw_window_id))
        except (TypeError, ValueError):
            return None
        if window_id == 0:
            return None

        return window_id

    def _query_active_window(self) -> tuple[str, str, list[str]]:
        with self._x_lock:
            if (
                X is None
                or self._xdisplay is None
                or self._root is None
                or self._atom_active is None
                or self._atom_net_wm_name is None
            ):
                return "", "", []

            try:
                window_id = self._query_active_window_id_unlocked()
                if window_id is None:
                    return "", "", []

                win = self._xdisplay.create_resource_object("window", window_id)

                wm_class = ""
                try:
                    class_data = win.get_wm_class()
                    if class_data:
                        wm_class = str(class_data[-1] or class_data[0] or "")
                except Exception as exc:  # noqa: BLE001 - Xlib metadata reads can fail broadly.
                    _log_x_failure("Failed to read X11 window class", exc)
                    wm_class = ""

                title = ""
                try:
                    net_name = win.get_full_property(
                        int(self._atom_net_wm_name),
                        int(getattr(X, "AnyPropertyType", 0)),
                    )
                    if net_name and net_name.value:
                        raw = net_name.value
                        if isinstance(raw, bytes):
                            title = raw.decode(errors="replace")
                        else:
                            title = str(raw)
                except Exception as exc:  # noqa: BLE001 - Xlib metadata reads can fail broadly.
                    _log_x_failure("Failed to read X11 window title", exc)
                    title = ""

                if not title:
                    try:
                        fallback = win.get_wm_name()
                        title = str(fallback or "")
                    except Exception as exc:  # noqa: BLE001 - Xlib metadata reads can fail broadly.
                        _log_x_failure("Failed to read fallback X11 window title", exc)
                        title = ""

                return wm_class, title, []
            except Exception as exc:  # noqa: BLE001 - keep the active-window fallback alive.
                _log_x_failure("Failed to query X11 active window", exc)
                return "", "", []

    def _query_cursor_position(self) -> tuple[int, int] | None:
        with self._x_lock:
            if self._root is None:
                return None
            try:
                pointer = self._root.query_pointer()
                return int(pointer.root_x), int(pointer.root_y)
            except (TypeError, ValueError):
                return None
            except Exception as exc:  # noqa: BLE001 - Xlib cursor query can fail broadly.
                _log_x_failure("Failed to query X11 cursor position", exc)
                return None
