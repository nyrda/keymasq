import json
import logging
import queue
import select
import socket
import threading
import time
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


class _BoundedWorkerPool:
    def __init__(self, *, workers: int = 4, capacity: int = 64) -> None:
        self._queue: queue.Queue[Callable[[], None] | None] = queue.Queue(
            maxsize=max(workers, capacity)
        )
        self._closed = False
        self._lock = threading.Lock()
        self._threads = [
            threading.Thread(
                target=self._worker_loop,
                daemon=True,
                name=f"keymasq-gui-worker-{index + 1}",
            )
            for index in range(max(1, workers))
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, work: Callable[[], None]) -> bool:
        with self._lock:
            if self._closed:
                return False
            try:
                self._queue.put_nowait(work)
            except queue.Full:
                return False
            return True

    def shutdown(self, timeout: float = 1.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            while True:
                try:
                    pending = self._queue.get_nowait()
                except queue.Empty:
                    break
                else:
                    self._queue.task_done()
                    del pending
            for _thread in self._threads:
                self._queue.put_nowait(None)

        deadline = time.monotonic() + max(0.0, float(timeout))
        for thread in self._threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)

    def _worker_loop(self) -> None:
        while True:
            work = self._queue.get()
            try:
                if work is None:
                    return
                work()
            finally:
                self._queue.task_done()


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
        self._reader_threads: dict[int, threading.Thread] = {}
        self._reader_sockets: dict[int, socket.socket] = {}
        self._generation = 0
        self._closed = False
        self._buffer = b""
        self._state_lock = threading.Lock()
        self._connect_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._response_queue: queue.Queue[JsonDict | None] | None = None
        self._response_generation: int | None = None
        self._callbacks: dict[str, list[Callable[[JsonDict], bool | None]]] = {}

    def request(self, payload: JsonDict, timeout: float = 5.0) -> JsonDict | None:
        if not self._ensure_connected(timeout=timeout):
            return None

        with self._request_lock:
            if not self._ensure_connected(timeout=timeout):
                return None

            response_queue: queue.Queue[JsonDict | None] = queue.Queue(maxsize=1)
            with self._state_lock:
                sock = self._sock
                generation = self._generation
                if sock is None or self._closed:
                    return None
                self._response_queue = response_queue
                self._response_generation = generation

            try:
                encoded_payload = (json.dumps(payload) + "\n").encode()
                self._send_with_deadline(sock, encoded_payload, timeout)

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
                    if (
                        self._response_queue is response_queue
                        and self._response_generation == generation
                    ):
                        self._response_queue = None
                        self._response_generation = None

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
            if self._closed:
                return False
            if self._sock is not None:
                return True

        if not SESSION_SOCKET_PATH.exists():
            return False

        with self._connect_lock:
            with self._state_lock:
                if self._closed:
                    return False
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
                if self._closed:
                    sock.close()
                    return False
                self._generation += 1
                generation = self._generation
                self._sock = sock
                self._buffer = b""
                reader = threading.Thread(
                    target=self._reader_loop,
                    args=(generation, sock),
                    daemon=True,
                    name=f"keymasq-session-reader-{generation}",
                )
                self._reader_thread = reader
                self._reader_threads[generation] = reader
                self._reader_sockets[generation] = sock
                reader.start()
        return True

    def _reader_loop(
        self,
        generation: int | None = None,
        sock: socket.socket | None = None,
    ) -> None:
        with self._state_lock:
            generation = self._generation if generation is None else generation
            sock = self._sock if sock is None else sock
        if sock is None:
            return
        local_buffer = b""
        try:
            while True:
                try:
                    data = sock.recv(4096)
                    if not data:
                        self._close_connection_if_current(sock, generation)
                        return

                    local_buffer += data
                    lines: list[bytes] = []
                    while b"\n" in local_buffer:
                        line, local_buffer = local_buffer.split(b"\n", 1)
                        lines.append(line)

                    with self._state_lock:
                        if generation != self._generation or sock is not self._sock:
                            continue
                        self._buffer = local_buffer

                    for line in lines:
                        if not line.strip():
                            continue

                        try:
                            message = json.loads(line.decode())
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        if not isinstance(message, dict):
                            continue

                        if "event" in message:
                            self._dispatch_event(message, generation)
                            continue

                        with self._state_lock:
                            response_queue = (
                                self._response_queue
                                if generation == self._generation
                                and self._response_generation in (None, generation)
                                else None
                            )
                        if response_queue is not None:
                            try:
                                response_queue.put_nowait(message)
                            except queue.Full:
                                pass
                except Exception:
                    log.exception("persistent reader error")
                    self._close_connection_if_current(sock, generation)
                    return
        finally:
            with self._state_lock:
                self._reader_threads.pop(generation, None)
                self._reader_sockets.pop(generation, None)

    def _send_with_deadline(self, sock: socket.socket, payload: bytes, timeout: float) -> None:
        deadline = time.monotonic() + max(0.01, float(timeout))
        view = memoryview(payload)
        try:
            fileno = sock.fileno()
        except (AttributeError, OSError):
            sock.sendall(payload)
            return

        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("session request write timed out")
            _readable, writable, _exceptional = select.select([], [fileno], [], remaining)
            if not writable:
                raise TimeoutError("session request write timed out")
            try:
                sent = sock.send(view, getattr(socket, "MSG_DONTWAIT", 0))
            except BlockingIOError:
                continue
            if sent <= 0:
                raise ConnectionError("session socket closed during write")
            view = view[sent:]

    def _dispatch_event(self, message: JsonDict, generation: int | None = None) -> None:
        event = message.get("event")
        if not isinstance(event, str):
            return

        with self._state_lock:
            generation = self._generation if generation is None else generation
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
                idle_add(self._dispatch_event_callback_once, generation, callback, message)
            except (RuntimeError, TypeError) as exc:
                log.warning(
                    "Failed to schedule session event callback with GLib: %s; dropping event %s",
                    exc,
                    event,
                )
            except Exception:
                log.exception("Unexpected failure scheduling session event callback")

    def _dispatch_event_callback_once(
        self,
        generation: int,
        callback: Callable[[JsonDict], bool | None],
        message: JsonDict,
    ) -> bool:
        with self._state_lock:
            if self._closed or generation != self._generation or self._sock is None:
                return False
        try:
            callback(message)
        except Exception:
            log.exception("Session event callback failed")
        return False

    def _close_connection(self) -> None:
        with self._state_lock:
            sock = self._sock
            generation = self._generation
            self._sock = None
            self._buffer = b""
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except (AttributeError, OSError):
                pass
            try:
                sock.close()
            except OSError:
                pass

        with self._state_lock:
            response_queue = (
                self._response_queue
                if self._response_generation in (None, generation)
                else None
            )
        if response_queue is not None:
            try:
                response_queue.put_nowait(None)
            except queue.Full:
                pass

    def _close_connection_if_current(
        self,
        sock: socket.socket,
        generation: int | None = None,
    ) -> bool:
        response_queue: queue.Queue[JsonDict | None] | None = None
        with self._state_lock:
            if sock is self._sock and generation in (None, self._generation):
                self._sock = None
                self._buffer = b""
                response_queue = self._response_queue
                current = True
            else:
                current = False

        try:
            sock.shutdown(socket.SHUT_RDWR)
        except (AttributeError, OSError):
            pass
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

    def shutdown(self, timeout: float = 1.0) -> None:
        """Prevent future callbacks and close every connection generation."""

        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            sockets = set(self._reader_sockets.values())
            if self._sock is not None:
                sockets.add(self._sock)
            self._sock = None
            self._buffer = b""
            response_queue = self._response_queue
            self._response_queue = None
            self._response_generation = None
            readers = list(self._reader_threads.values())

        for current_sock in sockets:
            try:
                current_sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                current_sock.close()
            except OSError:
                pass
        if response_queue is not None:
            try:
                response_queue.put_nowait(None)
            except queue.Full:
                pass

        deadline = time.monotonic() + max(0.0, float(timeout))
        for reader in readers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            reader.join(remaining)


_PERSISTENT_SESSION = _PersistentSessionConnection()
_gui_state_lock = threading.Lock()
_gui_runtime_generation = 0
_gui_shutdown = False
_gui_pool: _BoundedWorkerPool | None = None


def _gui_generation() -> int | None:
    with _gui_state_lock:
        return None if _gui_shutdown else _gui_runtime_generation


def _gui_generation_is_current(generation: int) -> bool:
    with _gui_state_lock:
        return not _gui_shutdown and generation == _gui_runtime_generation


def _gui_worker_pool() -> _BoundedWorkerPool | None:
    global _gui_pool
    with _gui_state_lock:
        if _gui_shutdown:
            return None
        if _gui_pool is None:
            _gui_pool = _BoundedWorkerPool()
        return _gui_pool


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

    generation = _gui_generation()
    if generation is None:
        return

    if on_start is not None:
        on_start()

    def _worker() -> None:
        try:
            result = GuiTaskResult(value=worker())
        except Exception as exc:
            log.exception("GUI worker task failed")
            result = GuiTaskResult(error=exc)

        def _dispatch() -> bool:
            if not _gui_generation_is_current(generation):
                return False
            try:
                callback(result)
            except Exception:
                log.exception("GUI task callback failed")
            finally:
                if on_done is not None:
                    on_done()
            return False

        GLib.idle_add(_dispatch)

    pool = _gui_worker_pool()
    if pool is None or not pool.submit(_worker):
        result = GuiTaskResult[T](error=RuntimeError("GUI worker queue is unavailable"))

        def _dispatch_rejected() -> bool:
            if not _gui_generation_is_current(generation):
                return False
            try:
                callback(result)
            finally:
                if on_done is not None:
                    on_done()
            return False

        GLib.idle_add(_dispatch_rejected)


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


def shutdown_gui_runtime(timeout: float = 1.0) -> None:
    """Invalidate GTK callbacks and drain GUI IPC/background ownership."""

    global _gui_runtime_generation, _gui_shutdown
    with _gui_state_lock:
        if _gui_shutdown:
            return
        _gui_shutdown = True
        _gui_runtime_generation += 1
        pool = _gui_pool
    _PERSISTENT_SESSION.shutdown(timeout=timeout)
    if pool is not None:
        pool.shutdown(timeout=timeout)
