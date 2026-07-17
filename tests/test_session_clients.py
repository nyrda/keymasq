import asyncio
import logging
import queue
import struct
import sys
import threading
import types
from collections.abc import Callable
from typing import Any, cast

import pytest

from keymasq.common.ipc import Command, CommandType, Response, encode_response
from keymasq.gui import session_client as gui_session_client
from keymasq.session import client as session_client_module
from keymasq.session.client import KeymasqdClient


def test_keymasqd_client_send_command_requires_connection() -> None:
    async def _run() -> None:
        client = KeymasqdClient(event_handler=lambda _event, _data: None)
        with pytest.raises(ConnectionError, match="Not connected to keymasqd"):
            await client.send_command(Command(command=CommandType.PING, data={}))

    asyncio.run(_run())


def test_keymasqd_client_handle_response_matches_pending() -> None:
    async def _run() -> None:
        client = KeymasqdClient(event_handler=lambda _event, _data: None)
        future: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
        client._pending_requests["1"] = future

        response = Response(status="ok", request_id="1", data={"pong": True})
        await client._handle_response(response)

        assert future.done() is True
        assert future.result().data["pong"] is True

    asyncio.run(_run())


def test_keymasqd_client_handle_response_dispatches_event() -> None:
    async def _run() -> None:
        calls: list[tuple[CommandType, dict[str, Any]]] = []

        async def _event_handler(event_type: CommandType, data: dict[str, Any]) -> None:
            calls.append((event_type, data))

        client = KeymasqdClient(event_handler=_event_handler)
        response = Response(
            status="event",
            data={
                "command": CommandType.PING.value,
                "data": {"ok": True},
            },
        )
        await client._handle_response(response)
        await asyncio.sleep(0)

        assert calls == [(CommandType.PING, {"ok": True})]

    asyncio.run(_run())


def test_keymasqd_client_handle_response_ignores_unknown_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _run() -> None:
        calls: list[tuple[CommandType, dict[str, Any]]] = []

        async def _event_handler(event_type: CommandType, data: dict[str, Any]) -> None:
            calls.append((event_type, data))

        client = KeymasqdClient(event_handler=_event_handler)
        response = Response(
            status="event",
            data={
                "command": "newer_daemon_event",
                "data": {"ok": True},
            },
        )
        with caplog.at_level(logging.WARNING, logger="keymasq-session.client"):
            await client._handle_response(response)

        assert calls == []
        assert "Ignoring unknown daemon event: newer_daemon_event" in caplog.text

    asyncio.run(_run())


def test_keymasqd_client_listen_loop_skips_discarded_response_frame() -> None:
    async def _run() -> None:
        calls: list[tuple[CommandType, dict[str, Any]]] = []

        async def _event_handler(event_type: CommandType, data: dict[str, Any]) -> None:
            calls.append((event_type, data))

        client = KeymasqdClient(event_handler=_event_handler)
        reader = asyncio.StreamReader()
        malformed_payload = b"{not-json"
        reader.feed_data(
            struct.pack("!I", len(malformed_payload))
            + malformed_payload
            + encode_response(
                Response(
                    status="event",
                    data={
                        "command": CommandType.PING.value,
                        "data": {"ok": True},
                    },
                )
            )
        )
        reader.feed_eof()
        client.reader = reader

        await client._listen_loop()

        assert calls == [(CommandType.PING, {"ok": True})]

    asyncio.run(_run())


class _RaisingAsyncReader:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def read(self, _size: int) -> bytes:
        raise self.error


def test_keymasqd_client_listen_loop_logs_read_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _run() -> None:
        client = KeymasqdClient(event_handler=lambda _event, _data: None)
        client.reader = cast(asyncio.StreamReader, _RaisingAsyncReader(OSError("read failed")))

        with caplog.at_level(logging.WARNING, logger="keymasq-session.client"):
            await client._listen_loop()

        assert "Daemon client listen I/O error: read failed" in caplog.text
        assert client.reader is None

    asyncio.run(_run())


def test_keymasqd_client_listen_loop_logs_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _run() -> None:
        def _raise_decode_error(_data: bytes) -> tuple[Response | None, bytes]:
            raise RuntimeError("decoder failed")

        client = KeymasqdClient(event_handler=lambda _event, _data: None)
        reader = asyncio.StreamReader()
        reader.feed_data(encode_response(Response(status="ok")))
        reader.feed_eof()
        client.reader = reader
        monkeypatch.setattr(session_client_module, "decode_response", _raise_decode_error)

        with caplog.at_level(logging.ERROR, logger="keymasq-session.client"):
            await client._listen_loop()

        assert "Unexpected daemon client listen error" in caplog.text
        assert "decoder failed" in caplog.text
        assert client.reader is None

    asyncio.run(_run())


class _FakeSocket:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.closed = False

    def recv(self, _size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def close(self) -> None:
        self.closed = True


class _RequestOnlySocket:
    def __init__(self) -> None:
        self.sent = b""
        self.closed = False

    def send(self, data: bytes) -> int:
        raise AssertionError("partial-write-prone send() must not be used")

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def close(self) -> None:
        self.closed = True


class _BlockingSocket:
    def __init__(self, chunk: bytes, ready: threading.Event, release: threading.Event) -> None:
        self._chunk = chunk
        self._ready = ready
        self._release = release
        self.closed = False
        self._read = False

    def recv(self, _size: int) -> bytes:
        self._ready.set()
        self._release.wait(1.0)
        if self._read:
            return b""
        self._read = True
        return self._chunk

    def close(self) -> None:
        self.closed = True


class _BlockingErrorSocket:
    def __init__(self, ready: threading.Event, release: threading.Event) -> None:
        self._ready = ready
        self._release = release
        self.closed = False

    def recv(self, _size: int) -> bytes:
        self._ready.set()
        self._release.wait(1.0)
        raise OSError("stale read failed")

    def close(self) -> None:
        self.closed = True


class _ExistingSessionSocketPath:
    def exists(self) -> bool:
        return True

    def __str__(self) -> str:
        return "/tmp/keymasq-test-session.sock"


class _ConnectBlockingSocket:
    instances: list["_ConnectBlockingSocket"] = []
    instances_lock = threading.Lock()

    def __init__(self) -> None:
        self.closed = False
        self.connected_path: str | None = None
        self._closed_event = threading.Event()
        with self.instances_lock:
            self.instances.append(self)

    def settimeout(self, _timeout: float | None) -> None:
        return

    def connect(self, path: str) -> None:
        self.connected_path = path

    def recv(self, _size: int) -> bytes:
        self._closed_event.wait(1.0)
        return b""

    def close(self) -> None:
        self.closed = True
        self._closed_event.set()


class _ConnectFailingSocket:
    instances: list["_ConnectFailingSocket"] = []

    def __init__(self) -> None:
        self.closed = False
        self.timeout: float | None = None
        self.instances.append(self)

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout

    def connect(self, _path: str) -> None:
        raise TimeoutError("connect timed out")

    def close(self) -> None:
        self.closed = True


def _install_fake_glib(
    monkeypatch: pytest.MonkeyPatch,
    idle_add: Callable[..., bool] | None = None,
) -> types.ModuleType:
    glib_module = types.ModuleType("GLib")

    if idle_add is None:
        def _idle_add(callback: Callable[..., Any], *args: Any) -> bool:
            callback(*args)
            return True

        idle_add = _idle_add

    glib_module.idle_add = idle_add

    repository_module = types.ModuleType("repository")
    repository_module.GLib = glib_module
    gi_module = types.ModuleType("gi")
    gi_module.repository = repository_module

    monkeypatch.setitem(sys.modules, "gi", gi_module)
    monkeypatch.setitem(sys.modules, "gi.repository", repository_module)
    monkeypatch.setitem(sys.modules, "gi.repository.GLib", glib_module)
    return glib_module


def test_persistent_session_dispatch_event_callback_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_glib(monkeypatch)
    connection = gui_session_client._PersistentSessionConnection()
    calls: list[dict[str, Any]] = []

    connection.register_callback("macro_saved", lambda message: calls.append(message))
    connection._dispatch_event({"event": "macro_saved", "name": "example"})

    assert len(calls) == 1
    assert calls[0]["name"] == "example"


def test_persistent_session_dispatch_event_callback_is_one_shot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    idle_returns: list[bool] = []

    def _idle_add(callback: Callable[..., Any], *args: Any) -> bool:
        idle_returns.append(bool(callback(*args)))
        return True

    _install_fake_glib(monkeypatch, idle_add=_idle_add)

    connection = gui_session_client._PersistentSessionConnection()
    calls: list[dict[str, Any]] = []

    def callback(message: dict[str, Any]) -> bool:
        calls.append(message)
        return True

    connection.register_callback("macro_saved", callback)
    connection._dispatch_event({"event": "macro_saved", "name": "example"})

    assert [call["name"] for call in calls] == ["example"]
    assert idle_returns == [False]


def test_persistent_session_dispatch_event_falls_back_when_idle_add_fails(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _idle_add(_callback: Callable[..., Any], *_args: Any) -> bool:
        raise RuntimeError("main loop unavailable")

    _install_fake_glib(monkeypatch, idle_add=_idle_add)
    connection = gui_session_client._PersistentSessionConnection()
    calls: list[dict[str, Any]] = []
    connection.register_callback("macro_saved", lambda message: calls.append(message))
    caplog.set_level(logging.WARNING, logger="keymasq.gui.session_client")

    connection._dispatch_event({"event": "macro_saved", "name": "example"})

    assert calls == []
    assert "Failed to schedule session event callback with GLib" in caplog.text
    assert "dropping event macro_saved" in caplog.text


def test_persistent_session_dispatch_event_drops_when_idle_add_unavailable(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    glib_module = _install_fake_glib(monkeypatch)
    glib_module.idle_add = None
    connection = gui_session_client._PersistentSessionConnection()
    calls: list[dict[str, Any]] = []
    connection.register_callback("macro_saved", lambda message: calls.append(message))
    caplog.set_level(logging.WARNING, logger="keymasq.gui.session_client")

    connection._dispatch_event({"event": "macro_saved", "name": "example"})

    assert calls == []
    assert "GLib.idle_add unavailable" in caplog.text
    assert "dropping event macro_saved" in caplog.text


def test_persistent_session_reader_loop_routes_events_and_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_glib(monkeypatch)
    connection = gui_session_client._PersistentSessionConnection()
    events: list[str] = []
    response_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=2)

    connection._callbacks = {"macro_saved": [lambda message: events.append(message["event"])]}
    connection._response_queue = response_queue
    connection._sock = _FakeSocket(
        [
            b'{"event":"macro_saved","name":"one"}\n{"status":"ok","value":1}\n',
            b"",
        ]
    )

    connection._reader_loop()

    assert events == ["macro_saved"]
    queued = response_queue.get_nowait()
    assert queued is not None
    assert queued["status"] == "ok"


def test_persistent_session_request_timeout_closes_connection() -> None:
    connection = gui_session_client._PersistentSessionConnection()
    sock = _RequestOnlySocket()
    connection._sock = sock  # pyright: ignore[reportPrivateUsage]

    response = connection.request({"command": "get_status"}, timeout=0.01)

    assert response is None
    assert sock.sent == b'{"command": "get_status"}\n'
    assert sock.closed is True
    assert connection._sock is None  # pyright: ignore[reportPrivateUsage]


def test_stale_request_timeout_does_not_close_replacement_connection() -> None:
    connection = gui_session_client._PersistentSessionConnection()
    old_sock = _RequestOnlySocket()
    new_sock = _RequestOnlySocket()
    connection._sock = old_sock  # pyright: ignore[reportPrivateUsage]

    def replace_during_wait(*, timeout: float) -> None:
        _ = timeout
        with connection._state_lock:
            connection._generation += 1
            connection._sock = new_sock
        raise queue.Empty

    response_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=1)
    response_queue.get = replace_during_wait  # type: ignore[method-assign]
    original_queue = gui_session_client.queue.Queue
    gui_session_client.queue.Queue = lambda maxsize=0: response_queue  # type: ignore[assignment]
    try:
        response = connection.request({"command": "get_status"}, timeout=0.01)
    finally:
        gui_session_client.queue.Queue = original_queue

    assert response is None
    assert old_sock.closed is True
    assert new_sock.closed is False
    assert connection._sock is new_sock  # pyright: ignore[reportPrivateUsage]


def test_persistent_session_failed_connect_closes_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ConnectFailingSocket.instances = []
    connection = gui_session_client._PersistentSessionConnection()

    def _fake_socket(_family: int, _kind: int) -> _ConnectFailingSocket:
        return _ConnectFailingSocket()

    monkeypatch.setattr(gui_session_client, "SESSION_SOCKET_PATH", _ExistingSessionSocketPath())
    monkeypatch.setattr(gui_session_client.socket, "socket", _fake_socket)

    assert connection._ensure_connected(timeout=0.5) is False  # pyright: ignore[reportPrivateUsage]
    assert connection._ensure_connected(timeout=0.5) is False  # pyright: ignore[reportPrivateUsage]
    assert [sock.closed for sock in _ConnectFailingSocket.instances] == [True, True]
    assert connection._sock is None  # pyright: ignore[reportPrivateUsage]


def test_persistent_session_concurrent_first_connect_uses_one_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ConnectBlockingSocket.instances = []
    connection = gui_session_client._PersistentSessionConnection()
    start = threading.Barrier(3)
    results: list[bool] = []
    errors: list[BaseException] = []
    results_lock = threading.Lock()

    def _fake_socket(_family: int, _kind: int) -> _ConnectBlockingSocket:
        return _ConnectBlockingSocket()

    monkeypatch.setattr(gui_session_client, "SESSION_SOCKET_PATH", _ExistingSessionSocketPath())
    monkeypatch.setattr(gui_session_client.socket, "socket", _fake_socket)

    def _connect() -> None:
        try:
            start.wait(timeout=1.0)
            result = connection._ensure_connected(timeout=0.5)  # pyright: ignore[reportPrivateUsage]
            with results_lock:
                results.append(result)
        except (OSError, RuntimeError, TimeoutError, TypeError, ValueError, AssertionError) as exc:
            with results_lock:
                errors.append(exc)

    threads = [threading.Thread(target=_connect) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        try:
            start.wait(timeout=1.0)
        except threading.BrokenBarrierError as exc:
            pytest.fail(f"connect workers did not reach barrier: {exc!r}")
        for thread in threads:
            thread.join(1.0)

        assert [thread.is_alive() for thread in threads] == [False, False]
        assert errors == []
        assert results == [True, True]
        assert len(_ConnectBlockingSocket.instances) == 1
        assert connection._sock is _ConnectBlockingSocket.instances[0]  # pyright: ignore[reportPrivateUsage]
    finally:
        for thread in threads:
            thread.join(1.0)
        connection._close_connection()  # pyright: ignore[reportPrivateUsage]
        reader_thread = connection._reader_thread  # pyright: ignore[reportPrivateUsage]
        if reader_thread is not None:
            reader_thread.join(1.0)


def test_persistent_session_reader_thread_survives_socket_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_glib(monkeypatch)
    connection = gui_session_client._PersistentSessionConnection()
    response_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=2)
    ready = threading.Event()
    release = threading.Event()
    old_sock = _BlockingSocket(b'{"status":"old"}\n', ready, release)
    new_sock = _FakeSocket([b'{"status":"new"}\n', b""])

    connection._response_queue = response_queue
    connection._response_generation = 2
    connection._generation = 1
    connection._sock = old_sock
    thread = threading.Thread(target=connection._reader_loop, args=(1, old_sock), daemon=True)
    connection._reader_thread = thread
    thread.start()

    assert ready.wait(1.0) is True
    with connection._state_lock:
        connection._generation = 2
        connection._sock = new_sock
        connection._buffer = b""
    new_thread = threading.Thread(
        target=connection._reader_loop,
        args=(2, new_sock),
        daemon=True,
    )
    new_thread.start()
    release.set()

    queued = response_queue.get(timeout=1.0)
    assert queued == {"status": "new"}
    thread.join(1.0)
    new_thread.join(1.0)
    assert thread.is_alive() is False
    assert new_thread.is_alive() is False


def test_persistent_session_reader_ignores_stale_eof_after_socket_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_glib(monkeypatch)
    connection = gui_session_client._PersistentSessionConnection()
    response_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=2)
    ready = threading.Event()
    release = threading.Event()
    old_sock = _BlockingSocket(b"", ready, release)
    new_sock = _FakeSocket([b'{"status":"new"}\n', b""])

    connection._response_queue = response_queue
    connection._response_generation = 2
    connection._generation = 1
    connection._sock = old_sock
    thread = threading.Thread(target=connection._reader_loop, args=(1, old_sock), daemon=True)
    connection._reader_thread = thread
    thread.start()

    assert ready.wait(1.0) is True
    with connection._state_lock:
        connection._generation = 2
        connection._sock = new_sock
        connection._buffer = b""
    new_thread = threading.Thread(
        target=connection._reader_loop,
        args=(2, new_sock),
        daemon=True,
    )
    new_thread.start()
    release.set()

    queued = response_queue.get(timeout=1.0)
    assert queued == {"status": "new"}
    assert new_sock.closed is True
    thread.join(1.0)
    new_thread.join(1.0)
    assert thread.is_alive() is False
    assert new_thread.is_alive() is False


def test_persistent_session_reader_ignores_stale_error_after_socket_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_glib(monkeypatch)
    connection = gui_session_client._PersistentSessionConnection()
    response_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=2)
    ready = threading.Event()
    release = threading.Event()
    old_sock = _BlockingErrorSocket(ready, release)
    new_sock = _FakeSocket([b'{"status":"new"}\n', b""])

    connection._response_queue = response_queue
    connection._response_generation = 2
    connection._generation = 1
    connection._sock = old_sock
    thread = threading.Thread(target=connection._reader_loop, args=(1, old_sock), daemon=True)
    connection._reader_thread = thread
    thread.start()

    assert ready.wait(1.0) is True
    with connection._state_lock:
        connection._generation = 2
        connection._sock = new_sock
        connection._buffer = b""
    new_thread = threading.Thread(
        target=connection._reader_loop,
        args=(2, new_sock),
        daemon=True,
    )
    new_thread.start()
    release.set()

    queued = response_queue.get(timeout=1.0)
    assert queued == {"status": "new"}
    assert new_sock.closed is True
    thread.join(1.0)
    new_thread.join(1.0)
    assert thread.is_alive() is False
    assert new_thread.is_alive() is False


def test_run_gui_task_invokes_hooks_and_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_glib(monkeypatch)
    events: list[tuple[str, Any]] = []
    done = threading.Event()

    gui_session_client.run_gui_task(
        lambda: {"status": "ok"},
        lambda result: events.append(("callback", result)),
        on_start=lambda: events.append(("start", None)),
        on_done=lambda: events.append(("done", None)) or done.set(),
    )

    assert done.wait(1.0) is True
    assert events == [
        ("start", None),
        ("callback", gui_session_client.GuiTaskResult(value={"status": "ok"})),
        ("done", None),
    ]


def test_session_request_async_uses_session_request_and_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_glib(monkeypatch)
    calls: list[tuple[str, Any]] = []
    done = threading.Event()

    monkeypatch.setattr(
        gui_session_client,
        "session_request",
        lambda payload, timeout=5.0: {"status": "ok", "payload": payload, "timeout": timeout},
    )

    gui_session_client.session_request_async(
        {"command": "reload"},
        lambda result: calls.append(("callback", result)),
        timeout=2.0,
        on_start=lambda: calls.append(("start", None)),
        on_done=lambda: calls.append(("done", None)) or done.set(),
    )

    assert done.wait(1.0) is True
    assert calls == [
        ("start", None),
        (
            "callback",
            {"status": "ok", "payload": {"command": "reload"}, "timeout": 2.0},
        ),
        ("done", None),
    ]


def test_session_request_async_surfaces_worker_errors_to_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_glib(monkeypatch)
    results: list[dict[str, Any] | None] = []
    done = threading.Event()

    def _raise_request(_payload: dict[str, Any], timeout: float = 5.0) -> dict[str, Any] | None:
        _ = timeout
        raise RuntimeError("boom")

    monkeypatch.setattr(gui_session_client, "session_request", _raise_request)

    gui_session_client.session_request_async(
        {"command": "reload"},
        lambda result: results.append(result) or done.set(),
    )

    assert done.wait(1.0) is True
    assert results == [
        {
            "status": "error",
            "error_code": "gui_task_failed",
            "message": "boom",
        }
    ]


def test_session_request_async_hooks_surfaces_worker_errors_to_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_glib(monkeypatch)
    calls: list[tuple[str, Any]] = []
    done = threading.Event()

    def _raise_request(_payload: dict[str, Any], timeout: float = 5.0) -> dict[str, Any] | None:
        _ = timeout
        raise RuntimeError("boom")

    monkeypatch.setattr(gui_session_client, "session_request", _raise_request)

    gui_session_client.session_request_async(
        {"command": "reload"},
        lambda result: calls.append(("callback", result)) or done.set(),
        on_start=lambda: calls.append(("start", None)),
        on_done=lambda: calls.append(("done", None)),
    )

    assert done.wait(1.0) is True
    assert calls == [
        ("start", None),
        (
            "callback",
            {
                "status": "error",
                "error_code": "gui_task_failed",
                "message": "boom",
            },
        ),
        ("done", None),
    ]
