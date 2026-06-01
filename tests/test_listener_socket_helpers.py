import asyncio
import socket
import tempfile
from pathlib import Path

import pytest

from keymasq.session.listeners._socket_helpers import (
    candidate_wayland_sockets,
    unix_socket_connectable,
)


def _bind_unix_socket(path: Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    return sock


@pytest.mark.asyncio
async def test_candidate_wayland_sockets_returns_sorted_runtime_sockets(monkeypatch) -> None:
    with tempfile.TemporaryDirectory(prefix="kmsq-", dir="/tmp") as tmp_dir:
        runtime_path = Path(tmp_dir)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_path))
        (runtime_path / "wayland-1").write_text("not a socket")
        (runtime_path / "other.sock").write_text("not a wayland socket")

        sockets = [
            _bind_unix_socket(runtime_path / "wayland-2"),
            _bind_unix_socket(runtime_path / "wayland-10"),
            _bind_unix_socket(runtime_path / "wayland-0"),
        ]
        try:
            assert await candidate_wayland_sockets() == [
                runtime_path / "wayland-0",
                runtime_path / "wayland-10",
                runtime_path / "wayland-2",
            ]
        finally:
            for sock in sockets:
                sock.close()


@pytest.mark.asyncio
async def test_unix_socket_connectable_closes_probe_connection() -> None:
    with tempfile.TemporaryDirectory(prefix="kmsq-", dir="/tmp") as tmp_dir:
        socket_path = Path(tmp_dir) / "probe.sock"
        peer_closed = asyncio.Event()

        async def _handle_client(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            await reader.read(1)
            peer_closed.set()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(_handle_client, path=str(socket_path))
        try:
            assert await unix_socket_connectable(socket_path, timeout_s=0.5) is True
            await asyncio.wait_for(peer_closed.wait(), timeout=0.5)
        finally:
            server.close()
            await server.wait_closed()
            socket_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_unix_socket_connectable_returns_false_for_missing_socket() -> None:
    with tempfile.TemporaryDirectory(prefix="kmsq-", dir="/tmp") as tmp_dir:
        assert (
            await unix_socket_connectable(Path(tmp_dir) / "missing.sock", timeout_s=0.01) is False
        )
