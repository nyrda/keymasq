import queue
import threading

import pytest

import keymasq.gui.session_client as session_client_module
from keymasq.gui.session_client import _BoundedWorkerPool, _PersistentSessionConnection


class _FakeSocket:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.closed = False

    def recv(self, _size: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    def close(self) -> None:
        self.closed = True


class _NeverWritableSocket:
    def fileno(self) -> int:
        return 123

    def send(self, _payload: memoryview, _flags: int) -> int:
        raise AssertionError("send must not run without writable readiness")


def test_old_reader_generation_cannot_deliver_response_to_replacement() -> None:
    connection = _PersistentSessionConnection()
    old_socket = _FakeSocket([b'{"status":"ok","generation":1}\n'])
    new_socket = _FakeSocket([b'{"status":"ok","generation":2}\n'])
    response_queue: queue.Queue[dict | None] = queue.Queue(maxsize=1)
    connection._generation = 2
    connection._sock = new_socket  # type: ignore[assignment]
    connection._response_queue = response_queue
    connection._response_generation = 2

    connection._reader_loop(1, old_socket)  # type: ignore[arg-type]
    assert response_queue.empty()

    connection._reader_loop(2, new_socket)  # type: ignore[arg-type]
    assert response_queue.get_nowait() == {"status": "ok", "generation": 2}


def test_stale_event_callback_is_suppressed_after_generation_change() -> None:
    connection = _PersistentSessionConnection()
    connection._generation = 3
    connection._sock = _FakeSocket([])  # type: ignore[assignment]
    calls: list[dict] = []

    assert connection._dispatch_event_callback_once(2, calls.append, {"event": "old"}) is False
    assert calls == []

    assert connection._dispatch_event_callback_once(3, calls.append, {"event": "new"}) is False
    assert calls == [{"event": "new"}]


def test_reconnect_starts_new_reader_while_old_generation_is_still_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SocketPath:
        def exists(self) -> bool:
            return True

        def __str__(self) -> str:
            return "/fake/session.sock"

    class _BlockingSocket:
        def __init__(self) -> None:
            self.read_started = threading.Event()
            self.release = threading.Event()

        def settimeout(self, _timeout: float | None) -> None:
            return

        def connect(self, _path: str) -> None:
            return

        def recv(self, _size: int) -> bytes:
            self.read_started.set()
            self.release.wait(1.0)
            return b""

        def close(self) -> None:
            return

        def shutdown(self, _how: int) -> None:
            self.release.set()

    sockets = [_BlockingSocket(), _BlockingSocket()]
    monkeypatch.setattr(session_client_module, "SESSION_SOCKET_PATH", _SocketPath())
    monkeypatch.setattr(session_client_module.socket, "socket", lambda *_args: sockets.pop(0))
    connection = _PersistentSessionConnection()

    assert connection._ensure_connected(timeout=0.1) is True
    first_socket = connection._sock
    assert isinstance(first_socket, _BlockingSocket)
    assert first_socket.read_started.wait(1.0) is True
    with connection._state_lock:
        connection._sock = None
        connection._buffer = b""

    assert connection._ensure_connected(timeout=0.1) is True
    second_socket = connection._sock
    assert isinstance(second_socket, _BlockingSocket)
    assert second_socket.read_started.wait(1.0) is True
    assert set(connection._reader_threads) == {1, 2}

    connection.shutdown(timeout=1.0)
    first_socket.release.set()


def test_gui_worker_pool_has_bounded_admission() -> None:
    pool = _BoundedWorkerPool(workers=1, capacity=1)
    started = threading.Event()
    release = threading.Event()

    def blocking_work() -> None:
        started.set()
        release.wait(1.0)

    assert pool.submit(blocking_work) is True
    assert started.wait(1.0) is True
    assert pool.submit(lambda: None) is True
    assert pool.submit(lambda: None) is False

    release.set()
    pool.shutdown(timeout=1.0)


def test_persistent_session_write_has_a_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _PersistentSessionConnection()
    monkeypatch.setattr(
        session_client_module.select,
        "select",
        lambda *_args: ([], [], []),
    )

    with pytest.raises(TimeoutError, match="write timed out"):
        connection._send_with_deadline(_NeverWritableSocket(), b"request", 0.01)  # type: ignore[arg-type]
