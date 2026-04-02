import asyncio

import pytest

from keyforge.common.ipc import Command, CommandType, Response
from keyforge.session.client import KeyforgedClient


class _BlockingAsyncReader:
    def __init__(self) -> None:
        self._release = asyncio.Event()

    async def read(self, _size: int) -> bytes:
        await self._release.wait()
        return b""

    def release(self) -> None:
        self._release.set()


class _FakeAsyncWriter:
    def __init__(self) -> None:
        self.closed = False

    def write(self, data: bytes) -> None:
        _ = data

    async def drain(self) -> None:
        return

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return


def test_keyforged_client_disconnect_fails_pending_requests_immediately() -> None:
    async def _run() -> None:
        client = KeyforgedClient(event_handler=lambda _event, _data: None)
        reader = _BlockingAsyncReader()
        writer = _FakeAsyncWriter()
        client.reader = reader
        client.writer = writer

        listen_task = asyncio.create_task(client._listen_loop())
        send_task = asyncio.create_task(
            client.send_command(Command(command=CommandType.PING, data={}))
        )

        await asyncio.sleep(0)
        assert client._pending_requests

        reader.release()

        with pytest.raises(ConnectionError, match="Disconnected from keyforged"):
            await send_task
        await listen_task

        assert client.reader is None
        assert client.writer is None
        assert writer.closed is True
        assert client._disconnected_event.is_set()

    asyncio.run(_run())


def test_keyforged_client_send_command_uses_custom_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        client = KeyforgedClient(event_handler=lambda _event, _data: None)
        client.writer = _FakeAsyncWriter()
        timeouts: list[float] = []

        async def fake_wait_for(awaitable, timeout):
            timeouts.append(timeout)
            awaitable.set_result(Response(status="ok", request_id="1", data={"pong": True}))
            return await awaitable

        monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

        response = await client.send_command(
            Command(command=CommandType.PING, data={}),
            timeout=42.0,
        )

        assert response.status == "ok"
        assert response.data == {"pong": True}
        assert timeouts == [42.0]

    asyncio.run(_run())
