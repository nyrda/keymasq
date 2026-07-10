import json
import logging
import queue
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from keymasq.common.paths import SESSION_SOCKET_PATH

log = logging.getLogger("keymasq.gui.session_client")

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class GuiTaskResult[T]:
    value: T | None = None
    error: Exception | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _gui_task_error_payload(error: Exception) -> JsonDict:
    message = str(error).strip() or error.__class__.__name__
    return {
        "status": "error",
        "error_code": "gui_task_failed",
        "message": message,
    }


class _PersistentSessionConnection:
    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._reader_thread: threading.Thread | None = None
        self._buffer = b""
        self._state_lock = threading.Lock()
        self._connect_lock = threading.Lock()
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
                encoded_payload = (json.dumps(payload) + "\n").encode()
                with self._state_lock:
                    if self._sock is None:
                        return None
                    self._sock.sendall(encoded_payload)

                try:
                    response = response_queue.get(timeout=timeout)
                except queue.Empty:
                    log.debug("persistent request timed out")
                    self._close_connection()
                    return None
                return response
            except Exception:
                log.exception("persistent request failed")
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

        with self._connect_lock:
            with self._state_lock:
                if self._sock is not None:
                    return True

            sock: socket.socket | None = None
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                sock.connect(str(SESSION_SOCKET_PATH))
                sock.settimeout(None)
            except OSError as e:
                log.debug(f"persistent connect failed: {e}")
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                return False

            with self._state_lock:
                self._sock = sock
                self._buffer = b""
                if self._reader_thread is None or not self._reader_thread.is_alive():
                    self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
                    self._reader_thread.start()
        return True

    def _reader_loop(self) -> None:
        while True:
            sock: socket.socket | None = None
            try:
                with self._state_lock:
                    sock = self._sock
                if sock is None:
                    return

                data = sock.recv(4096)
                if not data:
                    if self._close_connection_if_current(sock):
                        return
                    continue

                with self._state_lock:
                    if sock is not self._sock:
                        continue
                    self._buffer += data
                    lines: list[bytes] = []
                    while b"\n" in self._buffer:
                        line, self._buffer = self._buffer.split(b"\n", 1)
                        lines.append(line)

                for line in lines:
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
            except Exception:
                log.exception("persistent reader error")
                if sock is not None and self._close_connection_if_current(sock):
                    return
                continue

    def _dispatch_event(self, message: JsonDict) -> None:
        event = message.get("event")
        if not isinstance(event, str):
            return

        with self._state_lock:
            callbacks = list(self._callbacks.get(event, [])) + list(self._callbacks.get("*", []))

        if not callbacks:
            return

        idle_add: Callable[..., object] | None = None
        try:
            from gi.repository import GLib  # pyright: ignore[reportAttributeAccessIssue]
        except ImportError:
            pass
        except Exception:
            log.exception("Unexpected failure loading GLib for session event dispatch")
        else:
            raw_idle_add = getattr(GLib, "idle_add", None)
            if callable(raw_idle_add):
                idle_add = raw_idle_add
            else:
                log.warning("GLib.idle_add unavailable; dropping session event callback")

        for callback in callbacks:
            if idle_add is None:
                log.warning(
                    "Cannot marshal session event callback to GTK main loop; dropping event %s",
                    event,
                )
                continue

            try:
                idle_add(self._dispatch_event_callback_once, callback, message)
            except (RuntimeError, TypeError) as exc:
                log.warning(
                    "Failed to schedule session event callback with GLib: %s; dropping event %s",
                    exc,
                    event,
                )
            except Exception:
                log.exception("Unexpected failure scheduling session event callback")

    @staticmethod
    def _dispatch_event_callback_once(
        callback: Callable[[JsonDict], bool | None],
        message: JsonDict,
    ) -> bool:
        try:
            callback(message)
        except Exception:
            log.exception("Session event callback failed")
        return False

    def _close_connection(self) -> None:
        with self._state_lock:
            sock = self._sock
            self._sock = None
            self._buffer = b""
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

        with self._state_lock:
            response_queue = self._response_queue
        if response_queue is not None:
            try:
                response_queue.put_nowait(None)
            except queue.Full:
                pass

    def _close_connection_if_current(self, sock: socket.socket) -> bool:
        response_queue: queue.Queue[JsonDict | None] | None = None
        with self._state_lock:
            if sock is self._sock:
                self._sock = None
                self._buffer = b""
                response_queue = self._response_queue
                current = True
            else:
                current = False

        try:
            sock.close()
        except OSError:
            pass

        if current and response_queue is not None:
            try:
                response_queue.put_nowait(None)
            except queue.Full:
                pass
        return current


_PERSISTENT_SESSION = _PersistentSessionConnection()


def session_request(payload: JsonDict, timeout: float = 5.0) -> JsonDict | None:
    return _PERSISTENT_SESSION.request(payload, timeout=timeout)


def get_active_window(timeout: float = 5.0) -> JsonDict | None:
    return session_request({"command": "get_active_window"}, timeout=timeout)


def session_request_async(
    payload: JsonDict,
    callback: Callable[[JsonDict | None], bool | None],
    timeout: float = 5.0,
    *,
    on_start: Callable[[], None] | None = None,
    on_done: Callable[[], None] | None = None,
) -> None:
    def _request() -> JsonDict | None:
        return session_request(payload, timeout=timeout)

    def _on_result(result: GuiTaskResult[JsonDict | None]) -> bool | None:
        if result.ok:
            return callback(result.value)
        error = result.error or RuntimeError("GUI task failed without an exception")
        return callback(_gui_task_error_payload(error))

    run_gui_task(
        _request,
        _on_result,
        on_start=on_start,
        on_done=on_done,
    )


def run_gui_task[T](
    worker: Callable[[], T],
    callback: Callable[[GuiTaskResult[T]], bool | None],
    *,
    on_start: Callable[[], None] | None = None,
    on_done: Callable[[], None] | None = None,
) -> None:
    from gi.repository import GLib  # pyright: ignore[reportAttributeAccessIssue]

    if on_start is not None:
        on_start()

    def _worker() -> None:
        try:
            result = GuiTaskResult(value=worker())
        except Exception as exc:
            log.exception("GUI worker task failed")
            result = GuiTaskResult(error=exc)

        def _dispatch() -> bool:
            try:
                callback(result)
            except Exception:
                log.exception("GUI task callback failed")
            finally:
                if on_done is not None:
                    on_done()
            return False

        GLib.idle_add(_dispatch)

    threading.Thread(target=_worker, daemon=True).start()


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
