import asyncio
import struct

import pytest

from keymasq.common.ipc import HEADER_FORMAT, Command, CommandType, Response, encode_response
from keymasq.session.client import KeymasqdClient
from tests.async_fakes import BlockingStreamReader as _BlockingReader
from tests.async_fakes import FakeStreamReader as _FakeReader
from tests.async_fakes import FakeStreamWriter as _FakeWriter


def test_fake_stream_reader_read_honors_size() -> None:
    async def _run() -> None:
        reader = _FakeReader([b"abcdef", b"gh"])

        assert await reader.read(2) == b"ab"
        assert await reader.read(3) == b"cde"
        assert await reader.read(10) == b"f"
        assert await reader.read(10) == b"gh"
        assert await reader.read(10) == b""

    asyncio.run(_run())


def test_fake_stream_reader_readline_splits_newlines() -> None:
    async def _run() -> None:
        reader = _FakeReader([b"one\ntwo\n", b"three"])

        assert await reader.readline() == b"one\n"
        assert await reader.readline() == b"two\n"
        assert await reader.readline() == b"three"
        assert await reader.readline() == b""

    asyncio.run(_run())


def test_keymasqd_client_disconnect_fails_pending_requests_immediately() -> None:
    async def _run() -> None:
        client = KeymasqdClient(event_handler=lambda _event, _data: None)
        reader = _BlockingReader()
        writer = _FakeWriter()
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
        client.writer = _FakeWriter()
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
        client.reader = _BlockingReader()
        writer = _FakeWriter()
        client.writer = writer
        client._listen_task = asyncio.create_task(client._listen_loop())

        await asyncio.sleep(0)
        await client.disconnect()

        assert writer.closed is True
        assert writer.wait_closed_calls == 1
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
        client.reader = _FakeReader([oversized + valid, b""])
        client.writer = _FakeWriter()
        future: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
        client._pending_requests["ok"] = future

        await client._listen_loop()

        assert future.done() is True
        assert future.result().data == {"done": True}

    asyncio.run(_run())


def test_keymasqd_client_event_can_complete_nested_request() -> None:
    async def _run() -> None:
        reader = asyncio.StreamReader()

        class _ReplyWriter(_FakeWriter):
            def write(self, data: bytes) -> None:
                super().write(data)
                reader.feed_data(
                    encode_response(
                        Response(status="ok", request_id="1", data={"stored": True})
                    )
                )

        nested_response: Response | None = None

        async def _event_handler(_event: CommandType, _data: object) -> None:
            nonlocal nested_response
            nested_response = await client.send_command(
                Command(command=CommandType.MACRO_DELETE_RECORDING)
            )
            reader.feed_eof()

        client = KeymasqdClient(event_handler=_event_handler)
        client.reader = reader
        client.writer = _ReplyWriter()
        reader.feed_data(
            encode_response(
                Response(
                    status="event",
                    data={"command": CommandType.RECORDING_STOPPED.value, "data": {}},
                )
            )
        )

        await asyncio.wait_for(client._listen_loop(), timeout=0.5)
        await asyncio.gather(*client._event_tasks)

        assert nested_response is not None
        assert nested_response.data == {"stored": True}

    asyncio.run(_run())


def test_keymasqd_client_processes_events_in_wire_order() -> None:
    async def _run() -> None:
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        calls: list[CommandType] = []

        async def _event_handler(event: CommandType, _data: object) -> None:
            calls.append(event)
            if event == CommandType.RECORDING_STARTED:
                first_started.set()
                await release_first.wait()

        client = KeymasqdClient(event_handler=_event_handler)
        await client._handle_response(
            Response(
                status="event",
                data={"command": CommandType.RECORDING_STARTED.value, "data": {}},
            )
        )
        await client._handle_response(
            Response(
                status="event",
                data={"command": CommandType.RECORDING_STOPPED.value, "data": {}},
            )
        )

        await asyncio.wait_for(first_started.wait(), timeout=0.5)
        await asyncio.sleep(0)
        assert calls == [CommandType.RECORDING_STARTED]

        release_first.set()
        await asyncio.gather(*client._event_tasks)
        assert calls == [CommandType.RECORDING_STARTED, CommandType.RECORDING_STOPPED]

    asyncio.run(_run())


def test_keymasqd_client_event_handler_can_disconnect() -> None:
    async def _run() -> None:
        writer = _FakeWriter()

        async def _event_handler(_event: CommandType, _data: object) -> None:
            await client.disconnect()

        client = KeymasqdClient(event_handler=_event_handler)
        client.writer = writer
        await client._handle_response(
            Response(
                status="event",
                data={"command": CommandType.PING.value, "data": {}},
            )
        )
        await asyncio.gather(*client._event_tasks)

        assert writer.closed is True
        assert writer.wait_closed_calls == 1
        assert client.writer is None

    asyncio.run(_run())


def test_keymasqd_client_unexpected_disconnect_cancels_event_worker() -> None:
    async def _run() -> None:
        reader = asyncio.StreamReader()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def _event_handler(_event: CommandType, _data: object) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        client = KeymasqdClient(event_handler=_event_handler)
        client.reader = reader
        client.writer = _FakeWriter()
        reader.feed_data(
            encode_response(
                Response(
                    status="event",
                    data={"command": CommandType.PING.value, "data": {}},
                )
            )
        )
        reader.feed_eof()

        await client._listen_loop()

        assert started.is_set()
        assert cancelled.is_set()
        assert client._event_tasks == set()
        assert client._disconnected_event.is_set()

    asyncio.run(_run())


def test_keymasqd_client_disconnect_fails_nested_request_before_worker_cancel() -> None:
    async def _run() -> None:
        reader = asyncio.StreamReader()
        request_started = asyncio.Event()

        class _RequestWriter(_FakeWriter):
            def write(self, data: bytes) -> None:
                super().write(data)
                request_started.set()

        async def _event_handler(_event: CommandType, _data: object) -> None:
            request_task = asyncio.create_task(
                client.send_command(Command(command=CommandType.PING))
            )
            try:
                await asyncio.shield(request_task)
            except asyncio.CancelledError:
                await asyncio.shield(request_task)
                raise

        client = KeymasqdClient(event_handler=_event_handler)
        client.reader = reader
        client.writer = _RequestWriter()
        reader.feed_data(
            encode_response(
                Response(
                    status="event",
                    data={"command": CommandType.PING.value, "data": {}},
                )
            )
        )
        listen_task = asyncio.create_task(client._listen_loop())
        await asyncio.wait_for(request_started.wait(), timeout=0.5)
        reader.feed_eof()

        await asyncio.wait_for(listen_task, timeout=0.5)

        assert request_started.is_set()
        assert client._pending_requests == {}
        assert client._event_tasks == set()
        assert client._disconnected_event.is_set()

    asyncio.run(_run())
