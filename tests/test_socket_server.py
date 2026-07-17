import asyncio
import logging
import os
import struct

import pytest
import pytest_asyncio

from keymasq.common import paths
from keymasq.common.ipc import (
    HEADER_FORMAT,
    Command,
    CommandType,
    decode_response,
    encode_command,
)
from keymasq.keymasqd.socket_server import ClientContext, SocketServer
from tests.async_fakes import (
    FakeStreamWriter as _BroadcastWriter,
)
from tests.async_fakes import (
    HangingStreamWriter as _HangingCloseWriter,
)


class _DropClientWriter(_BroadcastWriter):
    def __init__(
        self,
        *,
        on_wait_closed=None,
    ) -> None:
        super().__init__()
        self._on_wait_closed = on_wait_closed

    async def wait_closed(self) -> None:
        await super().wait_closed()
        if self._on_wait_closed is not None:
            await self._on_wait_closed()


class MockCommandHandler:
    def __init__(self):
        self.commands_received = []

    async def handle(
        self,
        command_type: CommandType,
        data: dict,
        client: ClientContext,
    ) -> dict:
        _ = client
        self.commands_received.append((command_type, data))

        if command_type == CommandType.PING:
            return {"pong": True}
        elif command_type == CommandType.LIST_DEVICES:
            return {"devices": []}
        else:
            return {"success": True}


class MockDisconnectHandler:
    def __init__(self):
        self.disconnect_called = False
        self.disconnected = asyncio.Event()

    async def handle(self):
        self.disconnect_called = True
        self.disconnected.set()


async def _ok_handler(
    _command: CommandType,
    _data: dict,
    _client: ClientContext,
) -> dict:
    return {"ok": True}


@pytest.mark.asyncio
class TestSocketServer:
    @pytest_asyncio.fixture
    async def server_and_handlers(self, temp_socket_dir):
        cmd_handler = MockCommandHandler()
        disc_handler = MockDisconnectHandler()

        server = SocketServer(
            str(paths.SOCKET_PATH),
            cmd_handler.handle,
            disc_handler.handle,
        )

        await server.start()

        yield server, cmd_handler, disc_handler

        await server.stop()

    async def test_server_starts(self, temp_socket_dir):
        cmd_handler = MockCommandHandler()
        server = SocketServer(str(paths.SOCKET_PATH), cmd_handler.handle)

        await server.start()

        assert paths.SOCKET_PATH.exists()
        assert (os.stat(paths.SOCKET_PATH).st_mode & 0o777) == 0o660

        await server.stop()

    async def test_start_cleans_up_when_socket_hardening_fails(
        self,
        monkeypatch,
        temp_socket_dir,
    ):
        real_chmod = os.chmod
        server = SocketServer(str(paths.SOCKET_PATH), _ok_handler)

        def fail_chmod(_path: str, _mode: int) -> None:
            raise OSError("chmod failed")

        monkeypatch.setattr(os, "chmod", fail_chmod)

        with pytest.raises(OSError, match="chmod failed"):
            await server.start()

        assert server.server is None
        assert not paths.SOCKET_PATH.exists()

        monkeypatch.setattr(os, "chmod", real_chmod)
        await server.start()
        await server.stop()

    async def test_denied_peer_is_disconnected(self, temp_socket_dir):
        cmd_handler = MockCommandHandler()
        server = SocketServer(
            str(paths.SOCKET_PATH),
            cmd_handler.handle,
            peer_validator=lambda _peer: (False, "denied for test"),
        )
        await server.start()

        reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))
        writer.write(b"{}")
        await writer.drain()
        try:
            data = await reader.read(128)
            assert data == b""
        except ConnectionResetError:
            pass

        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionResetError:
            pass
        await server.stop()

    async def test_client_connect_and_ping(self, server_and_handlers):
        _server, _cmd_handler, _ = server_and_handlers

        reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))

        cmd = Command(command=CommandType.PING, data={})
        writer.write(encode_command(cmd))
        await writer.drain()

        response_data = await reader.read(1024)
        response, _ = decode_response(response_data)

        assert response is not None
        assert response.status == "ok"
        assert response.data["pong"] is True

        writer.close()
        await writer.wait_closed()

    async def test_malformed_frame_does_not_block_following_command(self, server_and_handlers):
        _server, cmd_handler, _ = server_and_handlers

        reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))

        malformed_payload = b"not-json"
        malformed_frame = struct.pack(HEADER_FORMAT, len(malformed_payload)) + malformed_payload
        writer.write(
            malformed_frame
            + encode_command(Command(command=CommandType.PING, data={}, request_id="after-bad"))
        )
        await writer.drain()

        try:
            response_data = await asyncio.wait_for(reader.read(1024), timeout=1.0)
            response, _ = decode_response(response_data)

            assert response is not None
            assert response.status == "ok"
            assert response.request_id == "after-bad"
            assert cmd_handler.commands_received == [(CommandType.PING, {})]
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_oversized_frame_does_not_block_following_command(
        self,
        server_and_handlers,
        monkeypatch,
    ):
        import keymasq.common.ipc as ipc

        _server, cmd_handler, _ = server_and_handlers
        monkeypatch.setattr(ipc, "MAX_PAYLOAD_SIZE", 128)

        reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))

        payload_len = 129
        writer.write(
            struct.pack(HEADER_FORMAT, payload_len)
            + (b"x" * payload_len)
            + encode_command(
                Command(command=CommandType.PING, data={}, request_id="after-oversized")
            )
        )
        await writer.drain()

        try:
            response_data = await asyncio.wait_for(reader.read(1024), timeout=1.0)
            response, _ = decode_response(response_data)

            assert response is not None
            assert response.status == "ok"
            assert response.request_id == "after-oversized"
            assert cmd_handler.commands_received == [(CommandType.PING, {})]
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_command_handler_receives_command(self, server_and_handlers):
        _server, cmd_handler, _ = server_and_handlers

        reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))

        cmd = Command(
            command=CommandType.LIST_DEVICES,
            data={"filter": "mouse"},
            request_id="test-req",
        )
        writer.write(encode_command(cmd))
        await writer.drain()

        response_data = await asyncio.wait_for(reader.read(1024), timeout=1.0)
        response, _ = decode_response(response_data)

        assert response is not None
        assert response.status == "ok"
        assert len(cmd_handler.commands_received) == 1
        received_type, received_data = cmd_handler.commands_received[0]
        assert received_type == CommandType.LIST_DEVICES
        assert received_data["filter"] == "mouse"

        writer.close()
        await writer.wait_closed()

    async def test_disconnect_handler_called(self, server_and_handlers):
        _server, _cmd_handler, disc_handler = server_and_handlers

        _reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))

        writer.close()
        await writer.wait_closed()

        await asyncio.wait_for(disc_handler.disconnected.wait(), timeout=1.0)

        assert disc_handler.disconnect_called

    async def test_error_response_on_exception(self, temp_socket_dir):
        async def failing_handler(cmd_type, data, client):
            _ = cmd_type, data, client
            raise ValueError("Test error")

        server = SocketServer(str(paths.SOCKET_PATH), failing_handler)
        await server.start()

        reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))

        cmd = Command(command=CommandType.PING, data={})
        writer.write(encode_command(cmd))
        await writer.drain()

        response_data = await reader.read(1024)
        response, _ = decode_response(response_data)

        assert response.status == "error"
        assert response.error is not None
        assert "Test error" in response.error

        writer.close()
        await writer.wait_closed()
        await server.stop()

    async def test_handler_type_error_is_not_retried_without_context(self, temp_socket_dir):
        calls: list[int] = []

        async def failing_handler(cmd_type, data, context):
            _ = cmd_type, data
            calls.append(context.connection_id)
            raise TypeError("internal handler bug")

        server = SocketServer(str(paths.SOCKET_PATH), failing_handler)
        await server.start()

        reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))
        writer.write(encode_command(Command(command=CommandType.PING, data={})))
        await writer.drain()

        response_data = await reader.read(1024)
        response, _ = decode_response(response_data)

        assert response.status == "error"
        assert response.error is not None
        assert "internal handler bug" in response.error
        assert calls == [1]

        writer.close()
        await writer.wait_closed()
        await server.stop()

    async def test_command_handler_receives_client_context(self, temp_socket_dir):
        contexts: list[ClientContext] = []

        async def handle(
            _command: CommandType,
            _data: dict,
            client: ClientContext,
        ) -> dict:
            contexts.append(client)
            return {"pong": True}

        server = SocketServer(str(paths.SOCKET_PATH), handle)
        await server.start()

        reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))
        writer.write(encode_command(Command(command=CommandType.PING, data={})))
        await writer.drain()

        response_data = await reader.read(1024)
        response, _ = decode_response(response_data)

        assert response is not None
        assert response.status == "ok"
        assert len(contexts) == 1
        assert contexts[0].connection_id == 1
        assert contexts[0].pid == os.getpid()
        assert contexts[0].uid == os.geteuid()
        assert contexts[0].gid == os.getegid()

        writer.close()
        await writer.wait_closed()
        await server.stop()

    async def test_single_owner_handoff_after_owner_disconnect(self, temp_socket_dir):
        owner_disconnected = asyncio.Event()

        async def handle(_command: CommandType, _data: dict, _client: ClientContext) -> dict:
            return {"pong": True}

        async def handle_disconnect() -> None:
            owner_disconnected.set()

        server = SocketServer(
            str(paths.SOCKET_PATH),
            handle,
            disconnect_handler=handle_disconnect,
            single_owner=True,
        )

        await server.start()

        reader1, writer1 = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))
        ping = Command(command=CommandType.PING, data={})
        writer1.write(encode_command(ping))
        await writer1.drain()

        response_data = await reader1.read(1024)
        response, _ = decode_response(response_data)
        assert response is not None
        assert response.status == "ok"

        # A second owner cannot connect until the first owner disconnects.
        second_reader, second_writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))
        dropped = await second_reader.read(16)
        assert dropped == b""
        second_writer.close()
        await second_writer.wait_closed()

        writer1.close()
        await writer1.wait_closed()

        await asyncio.wait_for(owner_disconnected.wait(), timeout=1.0)
        assert server.owner_context is None

        # After release, a new owner may connect.
        third_reader, third_writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))
        third_writer.write(encode_command(ping))
        await third_writer.drain()

        response_data = await third_reader.read(1024)
        third_response, _ = decode_response(response_data)
        assert third_response is not None
        assert third_response.status == "ok"

        third_writer.close()
        await third_writer.wait_closed()
        await server.stop()

    async def test_disconnect_handler_fires_only_after_last_client(self, temp_socket_dir):
        class Counter:
            def __init__(self) -> None:
                self.calls = 0
                self.called = asyncio.Event()

            async def handle(self) -> None:
                self.calls += 1
                self.called.set()

        counter = Counter()

        server = SocketServer(
            str(paths.SOCKET_PATH),
            _ok_handler,
            counter.handle,
        )

        await server.start()

        first_drop = asyncio.Event()
        original_drop_client = server._drop_client
        drop_count = 0

        async def observe_drop(writer: asyncio.StreamWriter) -> None:
            nonlocal drop_count
            await original_drop_client(writer)
            drop_count += 1
            if drop_count == 1:
                first_drop.set()

        server._drop_client = observe_drop  # type: ignore[method-assign]

        c1_reader, c1_writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))
        c2_reader, c2_writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))
        for reader, writer in ((c1_reader, c1_writer), (c2_reader, c2_writer)):
            writer.write(encode_command(Command(command=CommandType.PING, data={})))
            await writer.drain()
            response_data = await asyncio.wait_for(reader.read(1024), timeout=1.0)
            response, _ = decode_response(response_data)
            assert response is not None
            assert response.status == "ok"

        c1_writer.close()
        await c1_writer.wait_closed()

        await asyncio.wait_for(first_drop.wait(), timeout=1.0)
        assert counter.calls == 0

        c2_writer.close()
        await c2_writer.wait_closed()

        await asyncio.wait_for(counter.called.wait(), timeout=1.0)
        assert counter.calls == 1

        await server.stop()

    async def test_single_owner_disconnect_runs_cleanup_before_owner_release(
        self,
        temp_socket_dir,
    ):
        calls: list[str] = []

        async def handle_disconnect() -> None:
            calls.append("disconnect")

        server = SocketServer(
            str(paths.SOCKET_PATH),
            _ok_handler,
            handle_disconnect,
            single_owner=True,
        )

        reconnect_writer = _BroadcastWriter()

        async def on_wait_closed() -> None:
            assert calls == ["disconnect"]
            assert server._owner_context is not None
            server.clients.add(reconnect_writer)

        owner_writer = _DropClientWriter(on_wait_closed=on_wait_closed)
        owner_context = ClientContext(
            connection_id=1,
            pid=100,
            uid=1000,
            gid=1000,
        )
        server.clients.add(owner_writer)
        server._buffer[owner_writer] = b""
        server._client_context[owner_writer] = owner_context
        server._owner_context = owner_context

        await server._drop_client(owner_writer)  # type: ignore[arg-type]

        assert calls == ["disconnect"]
        assert server._owner_context is None

    async def test_connection_rejected_when_peer_credentials_missing(
        self, monkeypatch, temp_socket_dir
    ):
        server = SocketServer(str(paths.SOCKET_PATH), _ok_handler)

        await server.start()
        monkeypatch.setattr(server, "_extract_peer", lambda _writer: None)

        reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))
        data = await reader.read(32)
        assert data == b""

        writer.close()
        await writer.wait_closed()
        await server.stop()

    async def test_broadcast_event_does_not_block_on_slow_client(
        self,
        temp_socket_dir,
        caplog,
    ):
        server = SocketServer(
            str(paths.SOCKET_PATH),
            _ok_handler,
            broadcast_drain_timeout_s=0.01,
        )
        slow_writer = _BroadcastWriter(drain_waiter=asyncio.Event())
        fast_writer = _BroadcastWriter()
        server.clients = {slow_writer, fast_writer}  # type: ignore[assignment]

        with caplog.at_level(logging.WARNING, logger="keymasqd.socket"):
            await asyncio.wait_for(
                server.broadcast_event(CommandType.PING, {"pong": True}),
                timeout=1.0,
            )

        assert len(fast_writer.writes) == 1
        assert fast_writer.drain_calls == 1
        assert slow_writer.closed is True
        assert slow_writer.wait_closed_calls == 1
        assert slow_writer not in server.clients  # type: ignore[operator]
        assert fast_writer in server.clients  # type: ignore[operator]
        assert "Timed out sending event to client" in caplog.text

    async def test_broadcast_event_removes_failed_last_client_and_fires_disconnect_handler(
        self,
        temp_socket_dir,
        caplog,
    ):
        disconnects: list[str] = []

        async def handle_disconnect() -> None:
            disconnects.append("done")

        server = SocketServer(
            str(paths.SOCKET_PATH),
            _ok_handler,
            disconnect_handler=handle_disconnect,
            broadcast_drain_timeout_s=0.01,
        )
        failed_writer = _BroadcastWriter(drain_error=ConnectionError("broken pipe"))
        server.clients = {failed_writer}  # type: ignore[assignment]

        with caplog.at_level(logging.WARNING, logger="keymasqd.socket"):
            await server.broadcast_event(CommandType.PING, {"pong": True})

        assert failed_writer.closed is True
        assert failed_writer.wait_closed_calls == 1
        assert not server.clients
        assert disconnects == ["done"]
        assert "Failed to send event to client: broken pipe" in caplog.text

    async def test_broadcast_event_logs_unexpected_send_failure(
        self,
        temp_socket_dir,
        caplog,
    ):
        server = SocketServer(
            str(paths.SOCKET_PATH),
            _ok_handler,
            broadcast_drain_timeout_s=0.01,
        )
        failed_writer = _BroadcastWriter(drain_error=RuntimeError("writer state invalid"))
        server.clients = {failed_writer}  # type: ignore[assignment]

        with caplog.at_level(logging.ERROR, logger="keymasqd.socket"):
            await server.broadcast_event(CommandType.PING, {"pong": True})

        assert failed_writer.closed is True
        assert failed_writer.wait_closed_calls == 1
        assert not server.clients
        assert "Unexpected failure sending event to client" in caplog.text
        assert "RuntimeError: writer state invalid" in caplog.text

    async def test_server_stop_times_out_stuck_client_close(self, temp_socket_dir):
        server = SocketServer(
            str(paths.SOCKET_PATH),
            _ok_handler,
            close_timeout_s=0.01,
        )
        stuck_writer = _HangingCloseWriter()
        server.clients = {stuck_writer}  # type: ignore[assignment]

        await asyncio.wait_for(server.stop(), timeout=1.0)

        assert stuck_writer.closed is True
        assert stuck_writer.wait_closed_calls == 1
        assert stuck_writer.abort_calls == 1
        assert not server.clients

    async def test_server_stop_closes_open_client_without_hanging(self, temp_socket_dir):
        server = SocketServer(
            str(paths.SOCKET_PATH),
            _ok_handler,
        )
        await server.start()

        _reader, _writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))

        await asyncio.wait_for(server.stop(), timeout=1.0)

    async def test_server_stop_drains_active_command_before_deadline(self, temp_socket_dir):
        started = asyncio.Event()
        release = asyncio.Event()
        completed = asyncio.Event()

        async def handler(_command, _data, _client):
            started.set()
            await release.wait()
            completed.set()
            return {"ok": True}

        server = SocketServer(
            str(paths.SOCKET_PATH),
            handler,
            handler_drain_timeout_s=0.5,
        )
        await server.start()
        reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))
        writer.write(encode_command(Command(command=CommandType.PING, data={})))
        await writer.drain()
        await started.wait()

        stop_task = asyncio.create_task(server.stop())
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(stop_task, timeout=1.0)

        response_data = await asyncio.wait_for(reader.read(1024), timeout=1.0)
        response, _ = decode_response(response_data)
        assert response is not None
        assert response.status == "ok"
        assert completed.is_set()
        assert server._handler_tasks == set()

    async def test_accept_registers_handler_before_coroutine_runs(self, temp_socket_dir):
        server = SocketServer(str(paths.SOCKET_PATH), _ok_handler)
        reader = asyncio.StreamReader()
        writer = _BroadcastWriter()

        server._accept_client(reader, writer)  # type: ignore[arg-type]

        assert len(server._handler_tasks) == 1
        await server.stop()
        assert server._handler_tasks == set()

    async def test_server_stop_drains_accept_callback_already_queued(
        self,
        temp_socket_dir,
    ):
        server = SocketServer(str(paths.SOCKET_PATH), _ok_handler)
        await server.start()
        reader = asyncio.StreamReader()
        writer = _BroadcastWriter()
        asyncio.get_running_loop().call_soon(
            server._accept_client,
            reader,
            writer,
        )

        await server.stop()

        assert writer.closed is True
        assert server._handler_tasks == set()

    async def test_server_stop_cancels_stuck_command_after_deadline(self, temp_socket_dir):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def handler(_command, _data, _client):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        server = SocketServer(
            str(paths.SOCKET_PATH),
            handler,
            handler_drain_timeout_s=0.01,
        )
        await server.start()
        _reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))
        writer.write(encode_command(Command(command=CommandType.PING, data={})))
        await writer.drain()
        await started.wait()

        await asyncio.wait_for(server.stop(), timeout=1.0)

        assert cancelled.is_set()
        assert server._handler_tasks == set()

    async def test_handler_drain_bounds_cancellation_cleanup_and_blocks_restart(
        self,
        temp_socket_dir,
    ):
        cancel_seen = asyncio.Event()
        release = asyncio.Event()

        async def stubborn_handler() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancel_seen.set()
                await release.wait()

        server = SocketServer(
            str(paths.SOCKET_PATH),
            _ok_handler,
            handler_drain_timeout_s=0.01,
        )
        task = asyncio.create_task(stubborn_handler())
        server._handler_tasks.add(task)
        await asyncio.sleep(0)

        await asyncio.wait_for(server._drain_handler_tasks(graceful=True), timeout=0.2)
        assert cancel_seen.is_set()
        assert not task.done()
        with pytest.raises(RuntimeError, match="prior handler"):
            await server.start()

        release.set()
        await asyncio.wait_for(task, timeout=0.2)
        server._handler_tasks.discard(task)

    async def test_quiescing_server_rejects_newly_processed_command(self, temp_socket_dir):
        handler_calls: list[bool] = []

        async def handler(_command, _data, _client):
            handler_calls.append(True)
            return {"ok": True}

        server = SocketServer(str(paths.SOCKET_PATH), handler)
        server._quiescing = True
        response = await server._process_command(
            Command(command=CommandType.PING, data={}, request_id="late"),
            ClientContext(connection_id=1, pid=2, uid=3, gid=4),
        )

        assert response.status == "error"
        assert response.request_id == "late"
        assert handler_calls == []

    async def test_server_stop_clears_single_owner_state_for_restart(self, temp_socket_dir):
        server = SocketServer(
            str(paths.SOCKET_PATH),
            _ok_handler,
            single_owner=True,
        )
        await server.start()

        reader1, writer1 = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))
        writer1.write(encode_command(Command(command=CommandType.PING, data={})))
        await writer1.drain()

        response_data = await reader1.read(1024)
        response, _ = decode_response(response_data)
        assert response is not None
        assert response.status == "ok"
        assert server._owner_context is not None

        await asyncio.wait_for(server.stop(), timeout=1.0)

        assert not paths.SOCKET_PATH.exists()
        assert not server.clients
        assert not server._buffer
        assert not server._client_context
        assert server._owner_context is None

        await server.start()
        reader2, writer2 = await asyncio.open_unix_connection(str(paths.SOCKET_PATH))
        writer2.write(encode_command(Command(command=CommandType.PING, data={})))
        await writer2.drain()

        response_data = await reader2.read(1024)
        response, _ = decode_response(response_data)
        assert response is not None
        assert response.status == "ok"

        writer1.close()
        writer2.close()
        await asyncio.gather(
            writer1.wait_closed(),
            writer2.wait_closed(),
            return_exceptions=True,
        )
        await server.stop()
