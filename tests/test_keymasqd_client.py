import asyncio
import struct

import pytest

from keymasq.common.ipc import HEADER_FORMAT, Command, CommandType, Response, encode_response
from keymasq.session.client import KeymasqdClient


class _BlockingAsyncReader:
    def __init__(self) -> None:
        self._release = asyncio.Event()

    async def read(self, _size: int) -> bytes:
        await self._release.wait()
        return b""

    def release(self) -> None:
        self._release.set()


class _ChunkedAsyncReader:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def read(self, _size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _FakeAsyncWriter:
    def __init__(self) -> None:
        self.closed = False
        self.wait_closed_count = 0

    def write(self, data: bytes) -> None:
        _ = data

    async def drain(self) -> None:
        return

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.wait_closed_count += 1
        return


def test_keymasqd_client_disconnect_fails_pending_requests_immediately() -> None:
    async def _run() -> None:
        client = KeymasqdClient(event_handler=lambda _event, _data: None)
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

        with pytest.raises(ConnectionError, match="Disconnected from keymasqd"):
            await send_task
        await listen_task

        assert client.reader is None
        assert client.writer is None
        assert writer.closed is True
        assert client._disconnected_event.is_set()

    asyncio.run(_run())


def test_keymasqd_client_send_command_uses_custom_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        client = KeymasqdClient(event_handler=lambda _event, _data: None)
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


def test_keymasqd_client_disconnect_waits_for_cancelled_writer_to_close() -> None:
    async def _run() -> None:
        client = KeymasqdClient(event_handler=lambda _event, _data: None)
        client.reader = _BlockingAsyncReader()
        writer = _FakeAsyncWriter()
        client.writer = writer
        client._listen_task = asyncio.create_task(client._listen_loop())

        await asyncio.sleep(0)
        await client.disconnect()

        assert writer.closed is True
        assert writer.wait_closed_count == 1
        assert client.reader is None
        assert client.writer is None

    asyncio.run(_run())


def test_keymasqd_client_discards_oversized_response_before_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        import keymasq.common.ipc as ipc

        monkeypatch.setattr(ipc, "MAX_PAYLOAD_SIZE", 128)
        payload_len = 129
        oversized = struct.pack(HEADER_FORMAT, payload_len) + (b"x" * payload_len)
        valid = encode_response(Response(status="ok", request_id="ok", data={"done": True}))

        client = KeymasqdClient(event_handler=lambda _event, _data: None)
        client.reader = _ChunkedAsyncReader([oversized + valid, b""])
        client.writer = _FakeAsyncWriter()
        future: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
        client._pending_requests["ok"] = future

        await client._listen_loop()

        assert future.done() is True
        assert future.result().data == {"done": True}

    asyncio.run(_run())
