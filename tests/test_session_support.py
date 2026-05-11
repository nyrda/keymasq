import asyncio
import logging
import runpy
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from keymasq.common.ipc import Command, CommandType, Response, encode_response
from keymasq.session.action_handler import ActionHandler
from keymasq.session.client import KeymasqdClient
from keymasq.session.slurp import SLURP_MACRO_NAME, trigger_slurp_macro


class _FakeWriter:
    def __init__(self, wait_closed_error: Exception | None = None) -> None:
        self.writes: list[bytes] = []
        self.closed = False
        self.wait_closed_error = wait_closed_error

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        if self.wait_closed_error is not None:
            raise self.wait_closed_error


class _FakeReader:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _FakeProcess:
    def __init__(
        self,
        returncode: int = 0,
        communicate: Callable[[], Awaitable[tuple[bytes, bytes]]] | None = None,
    ) -> None:
        self.returncode = returncode
        self.killed = False
        self._communicate = communicate or self._default_communicate

    async def _default_communicate(self) -> tuple[bytes, bytes]:
        return b"", b""

    async def communicate(self) -> tuple[bytes, bytes]:
        return await self._communicate()

    def kill(self) -> None:
        self.killed = True


@pytest.mark.asyncio
async def test_keymasqd_client_connect_and_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _FakeReader([])
    writer = _FakeWriter(wait_closed_error=RuntimeError("closed"))
    opened_paths: list[str] = []

    async def _open_unix_connection(path: str) -> tuple[_FakeReader, _FakeWriter]:
        opened_paths.append(path)
        return reader, writer

    async def _listen_forever() -> None:
        await asyncio.Future()

    monkeypatch.setattr("keymasq.session.client.SOCKET_PATH", Path("/tmp/keymasq.sock"))
    monkeypatch.setattr(asyncio, "open_unix_connection", _open_unix_connection)

    client = KeymasqdClient(event_handler=lambda _event, _data: None)
    monkeypatch.setattr(client, "_listen_loop", _listen_forever)

    await client.connect()

    assert opened_paths == ["/tmp/keymasq.sock"]
    assert client.reader is reader
    assert client.writer is writer
    assert client._listen_task is not None

    await client.disconnect()

    assert client.reader is None
    assert client.writer is None
    assert client._listen_task is None
    assert writer.closed is True


@pytest.mark.asyncio
async def test_keymasqd_client_send_command_round_trip_cleans_pending() -> None:
    client = KeymasqdClient(event_handler=lambda _event, _data: None)
    writer = _FakeWriter()
    client.writer = writer

    send_task = asyncio.create_task(
        client.send_command(Command(command=CommandType.PING, data={"value": 1}))
    )

    await asyncio.sleep(0)

    assert "1" in client._pending_requests
    await client._handle_response(Response(status="ok", request_id="1", data={"pong": True}))

    response = await send_task
    assert response.data == {"pong": True}
    assert client._pending_requests == {}
    assert len(writer.writes) == 1


@pytest.mark.asyncio
async def test_keymasqd_client_send_command_timeout_cleans_pending() -> None:
    client = KeymasqdClient(event_handler=lambda _event, _data: None)
    client.writer = _FakeWriter()

    with pytest.raises(TimeoutError):
        await client.send_command(Command(command=CommandType.PING, data={}), timeout=0.01)

    assert client._pending_requests == {}


@pytest.mark.asyncio
async def test_keymasqd_client_listen_loop_decodes_partial_messages() -> None:
    response = encode_response(Response(status="ok", request_id="9", data={"done": True}))
    client = KeymasqdClient(event_handler=lambda _event, _data: None)
    client.reader = _FakeReader([response[:5], response[5:], b""])
    client.writer = _FakeWriter()

    seen: list[Response] = []

    async def _handle_response(message: Response) -> None:
        seen.append(message)

    client._handle_response = _handle_response  # type: ignore[method-assign]

    await client._listen_loop()

    assert [message.request_id for message in seen] == ["9"]
    assert client.writer is None
    assert client._disconnected_event.is_set() is True


@pytest.mark.asyncio
async def test_keymasqd_client_handle_response_logs_event_handler_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _event_handler(_event: CommandType, _data: dict[str, Any]) -> None:
        raise RuntimeError("boom")

    client = KeymasqdClient(event_handler=_event_handler)

    with caplog.at_level(logging.ERROR):
        await client._handle_response(
            Response(status="event", data={"command": CommandType.PING.value, "data": {}})
        )

    assert "Event handler error: boom" in caplog.text


@pytest.mark.asyncio
async def test_action_handler_handle_action_only_executes_exec_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = ActionHandler()
    commands: list[str] = []

    async def _execute_command(cmd: str) -> int:
        commands.append(cmd)
        return 0

    monkeypatch.setattr(handler, "execute_command", _execute_command)

    await handler.handle_action({"action_type": "exec", "cmd": "echo ok"})
    await handler.handle_action({"action_type": "exec"})
    await handler.handle_action({"action_type": "keyboard", "cmd": "echo ignored"})

    assert commands == ["echo ok"]


@pytest.mark.asyncio
async def test_action_handler_execute_command_handles_failures(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    handler = ActionHandler()

    async def _failing_create_subprocess_shell(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        raise OSError("spawn failed")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _failing_create_subprocess_shell)

    with caplog.at_level(logging.ERROR):
        result = await handler.execute_command("bad command")

    assert result == -1
    assert "Failed to execute command: spawn failed" in caplog.text


@pytest.mark.asyncio
async def test_action_handler_execute_command_warns_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    handler = ActionHandler()

    async def _create_subprocess_shell(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        return _FakeProcess(
            returncode=7,
            communicate=lambda: asyncio.sleep(0, result=(b"", b"nope")),
        )

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _create_subprocess_shell)

    with caplog.at_level(logging.WARNING):
        result = await handler.execute_command("false")

    assert result == 7
    assert "Command failed with code 7: nope" in caplog.text


@pytest.mark.asyncio
async def test_action_handler_execute_command_kills_timed_out_process(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    handler = ActionHandler()
    process = _FakeProcess(communicate=lambda: asyncio.sleep(360))

    async def _create_subprocess_shell(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        return process

    async def _wait_for(_awaitable: Any, timeout: float) -> Any:
        _awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _create_subprocess_shell)
    monkeypatch.setattr(asyncio, "wait_for", _wait_for)

    with caplog.at_level(logging.ERROR):
        result = await handler.execute_command("sleep 999")

    assert result == -1
    assert process.killed is True
    assert "Command timed out after 300s, killing: sleep 999" in caplog.text


@pytest.mark.asyncio
async def test_action_handler_execute_command_sync_tracks_task_until_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = ActionHandler()
    started = asyncio.Event()
    finish = asyncio.Event()

    async def _execute_command(cmd: str) -> int:
        assert cmd == "echo ok"
        started.set()
        await finish.wait()
        return 0

    monkeypatch.setattr(handler, "execute_command", _execute_command)

    handler.execute_command_sync("echo ok")
    await asyncio.wait_for(started.wait(), timeout=1.0)

    assert len(handler._background_tasks) == 1  # pyright: ignore[reportPrivateUsage]
    task = next(iter(handler._background_tasks))  # pyright: ignore[reportPrivateUsage]

    finish.set()
    assert await task == 0
    await asyncio.sleep(0)

    assert handler._background_tasks == set()  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_action_handler_execute_command_sync_logs_unhandled_task_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = ActionHandler()

    async def _execute_command(_cmd: str) -> int:
        raise RuntimeError("exec exploded")

    monkeypatch.setattr(handler, "execute_command", _execute_command)

    with caplog.at_level(logging.ERROR, logger="keymasq-session.actions"):
        handler.execute_command_sync("bad")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert handler._background_tasks == set()  # pyright: ignore[reportPrivateUsage]
    assert "Unhandled exception in async command task" in caplog.text
    assert "RuntimeError: exec exploded" in caplog.text


@pytest.mark.asyncio
async def test_action_handler_cancel_background_tasks_cancels_pending_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = ActionHandler()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _execute_command(_cmd: str) -> int:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return 0

    monkeypatch.setattr(handler, "execute_command", _execute_command)

    handler.execute_command_sync("sleep 999")
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert len(handler._background_tasks) == 1  # pyright: ignore[reportPrivateUsage]

    await handler.cancel_background_tasks()

    assert cancelled.is_set()
    assert handler._background_tasks == set()  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_trigger_slurp_macro_sends_expected_command() -> None:
    calls: list[Command] = []

    class _DaemonClient:
        async def send_command(self, command: Command) -> Response:
            calls.append(command)
            return Response(status="ok")

    await trigger_slurp_macro(_DaemonClient())  # type: ignore[arg-type]

    assert len(calls) == 1
    assert calls[0].command is CommandType.MACRO_PLAY_BY_NAME
    assert calls[0].data == {"name": SLURP_MACRO_NAME, "speed": 1.0}


@pytest.mark.asyncio
async def test_trigger_slurp_macro_logs_failures(caplog: pytest.LogCaptureFixture) -> None:
    class _DaemonClient:
        async def send_command(self, command: Command) -> Response:
            raise RuntimeError("slurp failed")

    with caplog.at_level(logging.ERROR):
        await trigger_slurp_macro(_DaemonClient())  # type: ignore[arg-type]

    assert "failed to trigger slurp macro" in caplog.text


def test_session_main_module_calls_manager_main(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr("keymasq.session.manager.main", lambda: calls.append("module"))
    runpy.run_module("keymasq.session.__main__", run_name="__main__")

    assert calls == ["module"]
