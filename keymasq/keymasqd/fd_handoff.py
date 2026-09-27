"""Receive file descriptors that bounded root jobs cannot return in their request file.

Only root peers are accepted, and descriptors without a pending one-time token are closed.
"""

import array
import asyncio
import contextlib
import json
import logging
import os
import re
import socket
import weakref
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import cast

from keymasq.common.paths import HANDOFF_SOCKET_PATH
from keymasq.common.security import get_peer_credentials

log = logging.getLogger("keymasqd.handoff")
TOKEN = re.compile(r"[0-9a-f]{32}\Z")
MAX_FDS = 8
MAX_MESSAGE = 512
CONNECTION_TIMEOUT_S = 5.0


def _close_all(fds: list[int]) -> None:
    for fd in fds:
        with contextlib.suppress(OSError):
            os.close(fd)


class PendingHandoff:
    def __init__(self) -> None:
        self.future: asyncio.Future[list[int]] = asyncio.get_running_loop().create_future()
        self.claimed = False

    async def claim(self, timeout: float) -> list[int]:
        fds = await asyncio.wait_for(asyncio.shield(self.future), timeout)
        self.claimed = True
        return fds

    def discard(self) -> None:
        if self.claimed:
            return
        if self.future.done() and not self.future.cancelled():
            _close_all(self.future.result())
        else:
            self.future.cancel()


class FdHandoffServer:
    def __init__(self, path: Path = HANDOFF_SOCKET_PATH, *, trusted_uid: int = 0) -> None:
        self.path = path
        self.trusted_uid = trusted_uid
        self._socket: socket.socket | None = None
        self._pending: dict[str, PendingHandoff] = {}
        self._connections: dict[socket.socket, asyncio.TimerHandle] = {}

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.path.unlink(missing_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_NONBLOCK)
        try:
            sock.bind(str(self.path))
            os.chmod(self.path, 0o600)
            sock.listen(8)
            loop.add_reader(sock.fileno(), self._accept)
        except BaseException:
            sock.close()
            self.path.unlink(missing_ok=True)
            raise
        self._socket = sock
        _servers[loop] = self

    async def stop(self) -> None:
        sock, self._socket = self._socket, None
        loop = asyncio.get_running_loop()
        if _servers.get(loop) is self:
            del _servers[loop]
        if sock is not None:
            loop.remove_reader(sock.fileno())
            sock.close()
            self.path.unlink(missing_ok=True)
        for connection in list(self._connections):
            self._drop(connection)
        for pending in list(self._pending.values()):
            pending.discard()
        self._pending.clear()

    @contextlib.asynccontextmanager
    async def expect(self, token: str) -> AsyncGenerator[PendingHandoff]:
        if not TOKEN.fullmatch(token) or token in self._pending:
            raise ValueError("Invalid handoff token")
        pending = PendingHandoff()
        self._pending[token] = pending
        try:
            yield pending
        finally:
            if self._pending.get(token) is pending:
                del self._pending[token]
            pending.discard()

    def _accept(self) -> None:
        sock = self._socket
        if sock is None:
            return
        loop = asyncio.get_running_loop()
        while True:
            try:
                connection, _ = sock.accept()
            except BlockingIOError:
                return
            except OSError as exc:
                log.warning("Handoff accept failed: %s", exc)
                return
            peer = get_peer_credentials(connection)
            if peer is None or peer.uid != self.trusted_uid:
                log.warning("Rejected fd handoff from uid %s", None if peer is None else peer.uid)
                connection.close()
                continue
            connection.setblocking(False)
            self._connections[connection] = loop.call_later(
                CONNECTION_TIMEOUT_S, self._drop, connection
            )
            loop.add_reader(connection.fileno(), self._receive, connection)

    def _drop(self, connection: socket.socket) -> None:
        timer = self._connections.pop(connection, None)
        if timer is None:
            return
        timer.cancel()
        with contextlib.suppress(ValueError, OSError):
            asyncio.get_running_loop().remove_reader(connection.fileno())
        connection.close()

    def _receive(self, connection: socket.socket) -> None:
        fds = array.array("i")
        try:
            data, ancillary, flags, _ = connection.recvmsg(
                MAX_MESSAGE, socket.CMSG_SPACE(MAX_FDS * fds.itemsize)
            )
        except BlockingIOError:
            return
        except OSError as exc:
            log.warning("Handoff receive failed: %s", exc)
            self._drop(connection)
            return
        for level, kind, payload in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                fds.frombytes(payload[: len(payload) - len(payload) % fds.itemsize])
        received = list(fds)
        self._drop(connection)
        try:
            if flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC):
                raise ValueError("truncated handoff")
            message = cast(object, json.loads(data))
            token = (
                cast(dict[str, object], message).get("token") if isinstance(message, dict) else None
            )
            pending = self._pending.pop(token, None) if isinstance(token, str) else None
            if pending is None or pending.future.done():
                raise ValueError("unexpected handoff token")
        except ValueError as exc:
            log.warning("Discarded fd handoff: %s", exc)
            _close_all(received)
            return
        pending.future.set_result(received)


_servers: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, FdHandoffServer] = (
    weakref.WeakKeyDictionary()
)


def handoff_server() -> FdHandoffServer:
    server = _servers.get(asyncio.get_running_loop())
    if server is None:
        raise OSError("The daemon is not accepting privileged handoffs")
    return server


def send(path: Path, token: str, fds: list[int], *, expected_uid: int) -> None:
    """Deliver descriptors to the daemon listening at ``path``; used by root jobs."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
        connection.settimeout(CONNECTION_TIMEOUT_S)
        connection.connect(str(path))
        peer = get_peer_credentials(connection)
        if peer is None or peer.uid != expected_uid:
            raise PermissionError("The handoff socket does not belong to the Keymasq daemon")
        connection.sendmsg(
            [json.dumps({"token": token}).encode()],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))],
        )
