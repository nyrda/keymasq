import asyncio
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

import keymasq.common.paths as paths_module
import keymasq.session.manager.core as core_module
from keymasq.session.manager import SessionManager


@pytest.fixture
def session_socket_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="km-") as runtime_dir:
        socket_path = Path(runtime_dir) / "session.sock"
        monkeypatch.setattr(paths_module, "SESSION_SOCKET_PATH", socket_path)
        monkeypatch.setattr(core_module, "SESSION_SOCKET_PATH", socket_path)
        yield socket_path


@pytest.mark.asyncio
async def test_start_session_server_refuses_live_existing_socket(session_socket_path) -> None:
    session_socket_path.parent.mkdir(parents=True, exist_ok=True)

    async def handle_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        _ = reader
        writer.close()
        await writer.wait_closed()

    existing_server = await asyncio.start_unix_server(
        handle_client,
        path=str(session_socket_path),
    )
    manager = SessionManager()

    try:
        with pytest.raises(RuntimeError, match="already listening"):
            await manager._start_session_server()

        assert session_socket_path.exists()
    finally:
        existing_server.close()
        await existing_server.wait_closed()
        session_socket_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_start_session_server_replaces_stale_socket(session_socket_path) -> None:
    session_socket_path.parent.mkdir(parents=True, exist_ok=True)
    stale_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        stale_sock.bind(str(session_socket_path))
    finally:
        stale_sock.close()

    manager = SessionManager()

    try:
        await manager._start_session_server()

        assert manager._session_socket_owned is True
        assert session_socket_path.exists()
        assert await core_module._session_socket_accepts_connections()
    finally:
        if manager.session_server is not None:
            manager.session_server.close()
            await manager.session_server.wait_closed()
        session_socket_path.unlink(missing_ok=True)
