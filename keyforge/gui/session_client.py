import json
import logging
import queue
import socket as _socket
import threading
from collections.abc import Callable
from typing import Any

from keyforge.common.paths import SESSION_SOCKET_PATH

log = logging.getLogger("keyforge.gui.session_client")

JsonDict = dict[str, Any]


class _PersistentSessionConnection:
    def __init__(self) -> None:
        self._sock: _socket.socket | None = None
        self._reader_thread: threading.Thread | None = None
        self._buffer = b""
        self._state_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._response_queue: queue.Queue[JsonDict | None] | None = None
        self._callbacks: dict[str, list[Callable[[JsonDict], bool | None]]] = {}

    def request(self, payload: JsonDict, timeout: float = 5.0) -> JsonDict | None:
        if not self._ensure_connected(timeout=timeout):
            return None

        with self._request_lock:
            if not self._ensure_connected(timeout=timeout):
                return None

            response_queue: queue.Queue[JsonDict | None] = queue.Queue(maxsize=1)
            with self._state_lock:
                self._response_queue = response_queue

            try:
                with self._state_lock:
                    if self._sock is None:
                        return None
                    self._sock.send((json.dumps(payload) + "\n").encode())

                try:
                    response = response_queue.get(timeout=timeout)
                except queue.Empty:
                    return None
                return response
            except Exception as e:
                log.debug(f"persistent request failed: {e}")
                self._close_connection()
                return None
            finally:
                with self._state_lock:
                    if self._response_queue is response_queue:
                        self._response_queue = None

    def register_callback(self, event: str, callback: Callable[[JsonDict], bool | None]) -> None:
        with self._state_lock:
            self._callbacks.setdefault(event, []).append(callback)
        self._ensure_connected(timeout=1.0)

    def unregister_callback(self, event: str, callback: Callable[[JsonDict], bool | None]) -> None:
        with self._state_lock:
            callbacks = self._callbacks.get(event, [])
            try:
                callbacks.remove(callback)
            except ValueError:
                return

    def _ensure_connected(self, timeout: float) -> bool:
        with self._state_lock:
            if self._sock is not None:
                return True

        if not SESSION_SOCKET_PATH.exists():
            return False

        try:
            sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(str(SESSION_SOCKET_PATH))
            sock.settimeout(None)
        except Exception as e:
            log.debug(f"persistent connect failed: {e}")
            return False

        with self._state_lock:
            self._sock = sock
            if self._reader_thread is None or not self._reader_thread.is_alive():
                self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
                self._reader_thread.start()
        return True

    def _reader_loop(self) -> None:
        while True:
            try:
                with self._state_lock:
                    sock = self._sock
                if sock is None:
                    return

                data = sock.recv(4096)
                if not data:
                    self._close_connection()
                    return

                self._buffer += data
                while b"\n" in self._buffer:
                    line, self._buffer = self._buffer.split(b"\n", 1)
                    if not line.strip():
                        continue

                    try:
                        message = json.loads(line.decode())
                    except json.JSONDecodeError:
                        continue

                    if "event" in message:
                        self._dispatch_event(message)
                        continue

                    with self._state_lock:
                        response_queue = self._response_queue
                    if response_queue is not None:
                        try:
                            response_queue.put_nowait(message)
                        except queue.Full:
                            pass
            except Exception as e:
                log.debug(f"persistent reader error: {e}")
                self._close_connection()
                return

    def _dispatch_event(self, message: JsonDict) -> None:
        event = message.get("event")
        if not isinstance(event, str):
            return

        with self._state_lock:
            callbacks = list(self._callbacks.get(event, [])) + list(self._callbacks.get("*", []))

        if not callbacks:
            return

        try:
            from gi.repository import GLib  # pyright: ignore[reportAttributeAccessIssue]

            for callback in callbacks:
                GLib.idle_add(callback, message)
        except Exception:
            for callback in callbacks:
                try:
                    callback(message)
                except Exception:
                    pass

    def _close_connection(self) -> None:
        with self._state_lock:
            sock = self._sock
            self._sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

        with self._state_lock:
            response_queue = self._response_queue
        if response_queue is not None:
            try:
                response_queue.put_nowait(None)
            except Exception:
                pass


_PERSISTENT_SESSION = _PersistentSessionConnection()


def session_request(payload: JsonDict, timeout: float = 5.0) -> JsonDict | None:
    return _PERSISTENT_SESSION.request(payload, timeout=timeout)


def get_active_window(timeout: float = 5.0) -> JsonDict | None:
    return session_request({"command": "get_active_window"}, timeout=timeout)


def session_request_async(
    payload: JsonDict,
    callback: Callable[[JsonDict | None], bool | None],
    timeout: float = 5.0,
) -> None:
    def _request() -> JsonDict | None:
        return session_request(payload, timeout=timeout)

    run_gui_task(
        _request,
        callback,
    )


def run_gui_task(
    worker: Callable[[], Any],
    callback: Callable[[Any], bool | None],
    *,
    on_start: Callable[[], None] | None = None,
    on_done: Callable[[], None] | None = None,
) -> None:
    from gi.repository import GLib  # pyright: ignore[reportAttributeAccessIssue]

    if on_start is not None:
        on_start()

    def _worker() -> None:
        result = worker()

        def _dispatch() -> bool:
            try:
                callback(result)
            finally:
                if on_done is not None:
                    on_done()
            return False

        GLib.idle_add(_dispatch)

    threading.Thread(target=_worker, daemon=True).start()


def session_request_with_hooks(
    payload: JsonDict,
    callback: Callable[[JsonDict | None], bool | None],
    *,
    timeout: float = 5.0,
    on_start: Callable[[], None] | None = None,
    on_done: Callable[[], None] | None = None,
) -> None:
    def _request() -> JsonDict | None:
        return session_request(payload, timeout=timeout)

    run_gui_task(
        _request,
        callback,
        on_start=on_start,
        on_done=on_done,
    )


def get_active_window_async(
    callback: Callable[[JsonDict | None], bool | None],
    timeout: float = 5.0,
) -> None:
    session_request_async({"command": "get_active_window"}, callback, timeout=timeout)


def register_session_event_callback(
    event: str,
    callback: Callable[[JsonDict], bool | None],
) -> None:
    _PERSISTENT_SESSION.register_callback(event, callback)


def unregister_session_event_callback(
    event: str,
    callback: Callable[[JsonDict], bool | None],
) -> None:
    _PERSISTENT_SESSION.unregister_callback(event, callback)
