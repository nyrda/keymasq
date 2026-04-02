import asyncio
import queue
import sys
import types
from collections.abc import Callable
from typing import Any

import pytest

from keyforge.common.ipc import Command, CommandType, Response
from keyforge.gui import session_client as gui_session_client
from keyforge.session.client import KeyforgedClient


def test_keyforged_client_send_command_requires_connection() -> None:
    async def _run() -> None:
        client = KeyforgedClient(event_handler=lambda _event, _data: None)
        with pytest.raises(RuntimeError):
            await client.send_command(Command(command=CommandType.PING, data={}))

    asyncio.run(_run())


def test_keyforged_client_handle_response_matches_pending() -> None:
    async def _run() -> None:
        client = KeyforgedClient(event_handler=lambda _event, _data: None)
        future: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
        client._pending_requests["1"] = future

        response = Response(status="ok", request_id="1", data={"pong": True})
        await client._handle_response(response)

        assert future.done() is True
        assert future.result().data["pong"] is True

    asyncio.run(_run())


def test_keyforged_client_handle_response_dispatches_event() -> None:
    async def _run() -> None:
        calls: list[tuple[CommandType, dict[str, Any]]] = []

        async def _event_handler(event_type: CommandType, data: dict[str, Any]) -> None:
            calls.append((event_type, data))

        client = KeyforgedClient(event_handler=_event_handler)
        response = Response(
            status="event",
            data={
                "command": CommandType.PING.value,
                "data": {"ok": True},
            },
        )
        await client._handle_response(response)

        assert calls == [(CommandType.PING, {"ok": True})]

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


def _install_fake_glib(monkeypatch: pytest.MonkeyPatch) -> None:
    glib_module = types.ModuleType("GLib")

    def _idle_add(callback: Callable[..., Any], *args: Any) -> bool:
        callback(*args)
        return True

    glib_module.idle_add = _idle_add

    repository_module = types.ModuleType("repository")
    repository_module.GLib = glib_module
    gi_module = types.ModuleType("gi")
    gi_module.repository = repository_module

    monkeypatch.setitem(sys.modules, "gi", gi_module)
    monkeypatch.setitem(sys.modules, "gi.repository", repository_module)
    monkeypatch.setitem(sys.modules, "gi.repository.GLib", glib_module)


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


def test_get_active_window_async_uses_session_request_async(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[dict[str, Any], float]] = []

    def _fake_session_request_async(
        payload: dict[str, Any],
        callback: Callable[[dict | None], bool | None],
        timeout: float = 5.0,
    ) -> None:
        calls.append((payload, timeout))
        callback({"status": "ok", "class": "steam", "title": "Library", "tags": []})

    monkeypatch.setattr(gui_session_client, "session_request_async", _fake_session_request_async)

    results: list[dict | None] = []
    gui_session_client.get_active_window_async(
        lambda response: results.append(response),
        timeout=2.5,
    )

    assert calls == [({"command": "get_active_window"}, 2.5)]
    assert results == [{"status": "ok", "class": "steam", "title": "Library", "tags": []}]


def test_run_gui_task_invokes_hooks_and_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_glib(monkeypatch)
    events: list[tuple[str, Any]] = []

    gui_session_client.run_gui_task(
        lambda: {"status": "ok"},
        lambda result: events.append(("callback", result)),
        on_start=lambda: events.append(("start", None)),
        on_done=lambda: events.append(("done", None)),
    )

    assert events == [
        ("start", None),
        ("callback", {"status": "ok"}),
        ("done", None),
    ]


def test_session_request_with_hooks_uses_session_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_glib(monkeypatch)
    calls: list[tuple[str, Any]] = []

    monkeypatch.setattr(
        gui_session_client,
        "session_request",
        lambda payload, timeout=5.0: {"status": "ok", "payload": payload, "timeout": timeout},
    )

    gui_session_client.session_request_with_hooks(
        {"command": "reload"},
        lambda result: calls.append(("callback", result)),
        timeout=2.0,
        on_start=lambda: calls.append(("start", None)),
        on_done=lambda: calls.append(("done", None)),
    )

    assert calls == [
        ("start", None),
        (
            "callback",
            {"status": "ok", "payload": {"command": "reload"}, "timeout": 2.0},
        ),
        ("done", None),
    ]
