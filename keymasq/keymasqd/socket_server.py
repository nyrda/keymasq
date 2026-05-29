import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Protocol

from keymasq.common.ipc import (
    Command,
    CommandType,
    Response,
    decode_command,
    encode_response,
)
from keymasq.common.security import PeerCredentials, get_peer_credentials

log = logging.getLogger("keymasqd.socket")
type JsonObject = dict[str, object]


@dataclass
class ClientContext:
    connection_id: int
    pid: int
    uid: int
    gid: int
    client_class: str


class CommandHandler(Protocol):
    async def __call__(
        self,
        command_type: CommandType,
        data: JsonObject,
        client: ClientContext,
    ) -> JsonObject: ...


class DisconnectHandler(Protocol):
    async def __call__(self) -> None: ...


class PeerValidator(Protocol):
    def __call__(self, peer: PeerCredentials) -> tuple[bool, str, str]: ...


class SocketServer:
    def __init__(
        self,
        socket_path: str,
        command_handler: CommandHandler,
        disconnect_handler: DisconnectHandler | None = None,
        socket_mode: int = 0o660,
        peer_validator: PeerValidator | None = None,
        single_owner: bool = False,
        broadcast_drain_timeout_s: float = 0.25,
        close_timeout_s: float = 0.25,
    ) -> None:
        self.socket_path = socket_path
        self.command_handler = command_handler
        self.disconnect_handler = disconnect_handler
        self.socket_mode = socket_mode
        self.peer_validator = peer_validator
        self.single_owner = single_owner
        self.broadcast_drain_timeout_s = max(0.01, float(broadcast_drain_timeout_s))
        self.close_timeout_s = max(0.01, float(close_timeout_s))
        self.server: asyncio.Server | None = None
        self.clients: set[asyncio.StreamWriter] = set()
        self._buffer: dict[asyncio.StreamWriter, bytes] = {}
        self._client_context: dict[asyncio.StreamWriter, ClientContext] = {}
        self._next_connection_id = 1
        self._owner_context: ClientContext | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(
            self._handle_client,
            path=self.socket_path,
        )

        try:
            os.chown(self.socket_path, os.geteuid(), os.getegid())
            os.chmod(self.socket_path, self.socket_mode)
            mode = os.stat(self.socket_path).st_mode & 0o777
            if mode != self.socket_mode:
                raise RuntimeError(
                    f"Socket mode mismatch on {self.socket_path}: got {mode:04o}, "
                    f"expected {self.socket_mode:04o}"
                )
        except (PermissionError, OSError, RuntimeError) as exc:
            log.error(f"Failed to secure daemon socket: {exc}")
            raise

        log.info(f"Listening on {self.socket_path}")

    async def stop(self) -> None:
        if self.server:
            self.server.close()

        writers = set(self.clients) | set(self._buffer) | set(self._client_context)
        for writer in writers:
            self._request_writer_close(writer)

        await asyncio.gather(
            *(self._wait_writer_closed(writer) for writer in writers),
            return_exceptions=True,
        )

        if self.server:
            await self.server.wait_closed()

        self.clients.clear()
        self._buffer.clear()
        self._client_context.clear()
        self._owner_context = None

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        addr = writer.get_extra_info("peername") or "unknown"
        peer = self._extract_peer(writer)
        if peer is None:
            log.warning("Rejecting client without peer credentials")
            writer.close()
            await writer.wait_closed()
            return

        allowed = True
        client_class = "session"
        deny_reason = ""
        if self.peer_validator:
            try:
                allowed, client_class, deny_reason = self.peer_validator(peer)
            except Exception as exc:
                allowed = False
                deny_reason = f"peer validator exception: {exc}"

        if not allowed:
            log.warning(
                "Denied client pid=%s uid=%s reason=%s",
                peer.pid,
                peer.uid,
                deny_reason,
            )
            writer.close()
            await writer.wait_closed()
            return

        context = ClientContext(
            connection_id=self._next_connection_id,
            pid=peer.pid,
            uid=peer.uid,
            gid=peer.gid,
            client_class=client_class,
        )
        self._next_connection_id += 1

        if self.single_owner:
            if self._owner_context is None:
                self._owner_context = context
                log.info(
                    "Daemon owner claimed uid=%s pid=%s connection=%s",
                    context.uid,
                    context.pid,
                    context.connection_id,
                )
            else:
                owner = self._owner_context
                log.warning(
                    (
                        "Denied client uid=%s pid=%s connection=%s: "
                        "owner already held by uid=%s pid=%s connection=%s"
                    ),
                    context.uid,
                    context.pid,
                    context.connection_id,
                    owner.uid,
                    owner.pid,
                    owner.connection_id,
                )
                writer.close()
                await writer.wait_closed()
                return

        log.info(
            "Client connected pid=%s uid=%s class=%s addr=%s",
            context.pid,
            context.uid,
            context.client_class,
            addr,
        )

        self.clients.add(writer)
        self._buffer[writer] = b""
        self._client_context[writer] = context

        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break

                self._buffer[writer] += data

                while True:
                    buffered = self._buffer[writer]
                    cmd, remaining = decode_command(buffered)
                    if cmd is None:
                        if remaining != buffered:
                            log.warning("Dropping invalid daemon command frame")
                            self._buffer[writer] = remaining
                            continue
                        break

                    self._buffer[writer] = remaining
                    response = await self._process_command(cmd, context)
                    writer.write(encode_response(response))
                    await writer.drain()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"Error handling client: {e}")
        finally:
            log.info(f"Client disconnected: {addr}")
            await self._drop_client(writer)

    async def _process_command(self, cmd: Command, context: ClientContext) -> Response:
        try:
            result = await self.command_handler(cmd.command, cmd.data, context)
            return Response(
                status="ok",
                data=result,
                request_id=cmd.request_id,
            )
        except Exception as e:
            log.error(f"Command error: {e}")
            return Response(
                status="error",
                error=str(e),
                request_id=cmd.request_id,
            )

    def _extract_peer(self, writer: asyncio.StreamWriter) -> PeerCredentials | None:
        sock = writer.get_extra_info("socket")
        return get_peer_credentials(sock)

    async def broadcast_event(
        self,
        event_type: CommandType,
        data: JsonObject,
    ) -> None:
        cmd = Command(command=event_type, data=data)
        encoded = encode_response(
            Response(
                status="event",
                data={
                    "command": cmd.command.value,
                    "data": cmd.data,
                },
            )
        )

        failures = await asyncio.gather(
            *(self._send_broadcast_to_client(writer, encoded) for writer in list(self.clients))
        )

        for failed_writer in failures:
            if failed_writer is not None:
                await self._drop_client(failed_writer)

    async def _send_broadcast_to_client(
        self,
        writer: asyncio.StreamWriter,
        encoded: bytes,
    ) -> asyncio.StreamWriter | None:
        try:
            writer.write(encoded)
            await asyncio.wait_for(
                writer.drain(),
                timeout=self.broadcast_drain_timeout_s,
            )
            return None
        except Exception as e:
            log.warning(f"Failed to send event to client: {e}")
            return writer

    async def _drop_client(self, writer: asyncio.StreamWriter) -> None:
        if (
            writer not in self.clients
            and writer not in self._buffer
            and writer not in self._client_context
        ):
            return

        context = self._client_context.pop(writer, None)
        self.clients.discard(writer)
        self._buffer.pop(writer, None)
        is_owner = (
            self.single_owner
            and context is not None
            and self._owner_context is not None
            and self._owner_context.connection_id == context.connection_id
        )

        try:
            self._request_writer_close(writer)

            try:
                if is_owner and self.disconnect_handler:
                    await self.disconnect_handler()
            finally:
                if is_owner and context is not None:
                    log.info(
                        "Daemon owner released uid=%s pid=%s connection=%s",
                        context.uid,
                        context.pid,
                        context.connection_id,
                    )
                    self._owner_context = None

            await self._wait_writer_closed(writer, context)
        except Exception:
            pass

        if self.disconnect_handler and not is_owner and not self.clients:
            await self.disconnect_handler()

    def _request_writer_close(self, writer: asyncio.StreamWriter) -> None:
        try:
            writer.close()
        except Exception:
            pass

    async def _wait_writer_closed(
        self,
        writer: asyncio.StreamWriter,
        context: ClientContext | None = None,
    ) -> None:
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=self.close_timeout_s)
        except TimeoutError:
            peer = context or self._client_context.get(writer)
            if peer is None:
                log.warning("Timed out waiting for client socket to close")
            else:
                log.warning(
                    "Timed out waiting for client socket to close pid=%s uid=%s class=%s",
                    peer.pid,
                    peer.uid,
                    peer.client_class,
                )
            transport = getattr(writer, "transport", None)
            if transport is not None:
                try:
                    transport.abort()
                except Exception:
                    pass
        except Exception:
            pass
