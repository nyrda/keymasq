import asyncio
import json
import logging
import os
import socket
from typing import Any, cast

from keymasq.common.paths import (
    SECURITY_POLICY_PATH,
    SESSION_SOCKET_PATH,
    ensure_session_socket_dir,
)
from keymasq.common.security import PeerCredentials, get_peer_credentials, uid_allowed

from .. import commands, device_inspector, recording_capture, recording_lifecycle, recording_unlock
from ..common import JsonObject

log = logging.getLogger("keymasq-session")


class SessionServerMixin:
    async def _start_session_server(self: Any) -> None:
        ensure_session_socket_dir()

        if SESSION_SOCKET_PATH.exists():
            if await session_socket_accepts_connections():
                msg = f"keymasq-session is already listening on {SESSION_SOCKET_PATH}"
                raise RuntimeError(msg)
            try:
                SESSION_SOCKET_PATH.unlink()
            except OSError:
                log.debug("Failed to remove stale session socket", exc_info=True)

        self.session_server = await asyncio.start_unix_server(
            self._handle_session_client,
            path=str(SESSION_SOCKET_PATH),
        )
        self._session_socket_owned = True
        try:
            os.chmod(SESSION_SOCKET_PATH, 0o600)
        except OSError:
            log.warning(
                "Failed to set session socket permissions to 0600 on %s; "
                "socket may be accessible to other users",
                SESSION_SOCKET_PATH,
            )
        log.info(f"Session server listening on {SESSION_SOCKET_PATH}")
        log.info(
            "Session security policy loaded from %s",
            SECURITY_POLICY_PATH,
        )

    async def _handle_session_client(
        self: Any, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = get_peer_credentials(writer.get_extra_info("socket"))
        if peer is None:
            await self._close_session_writer(writer)
            return

        if not uid_allowed(peer.uid, self.security_policy.session_allowed_uids):
            log.warning(
                "Denied session client pid=%s uid=%s reason=%s",
                peer.pid,
                peer.uid,
                f"uid {peer.uid} is not allowed by session policy",
            )
            await self._close_session_writer(writer, peer)
            return

        log.debug(
            "Session client connected pid=%s uid=%s",
            peer.pid,
            peer.uid,
        )
        self.session_clients.add(writer)
        self.session_client_peers[writer] = peer
        buffer = b""

        try:
            while self.running:
                data = await reader.read(4096)
                if not data:
                    break

                buffer += data
                if len(buffer) > self.MAX_SESSION_CLIENT_BUFFER_BYTES:
                    log.warning(
                        "Session client exceeded max buffered request size pid=%s uid=%s bytes=%s",
                        peer.pid,
                        peer.uid,
                        len(buffer),
                    )
                    break

                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.strip():
                        try:
                            request = json.loads(line.decode())
                        except json.JSONDecodeError:
                            writer.write(json.dumps({"error": "invalid json"}).encode() + b"\n")
                            await writer.drain()
                            continue

                        try:
                            response = await self._handle_session_request(
                                request,
                                peer,
                                writer,
                            )
                        except (ValueError, TypeError, KeyError) as exc:
                            response = {"status": "error", "message": str(exc)}
                        except Exception as exc:
                            log.exception(
                                "Session request failed pid=%s uid=%s",
                                peer.pid,
                                peer.uid,
                            )
                            response = {"status": "error", "message": str(exc)}
                        writer.write(json.dumps(response).encode() + b"\n")
                        await writer.drain()
        except asyncio.CancelledError:
            pass
        except OSError:
            log.debug("Session client I/O error", exc_info=True)
        except Exception:
            log.exception(
                "Unexpected session client error pid=%s uid=%s",
                peer.pid,
                peer.uid,
            )
        finally:
            try:
                await device_inspector.clear_device_inspectors_for_writer(self, writer)
            except Exception:
                log.exception("Failed to clear device inspectors for disconnected session client")
            try:
                await recording_capture.clear_captures_for_writer(self, writer)
            except Exception:
                log.exception("Failed to clear captures for disconnected session client")
            recording_lifecycle.clear_active_recording_owner_if_writer(self, writer)
            await recording_unlock.clear_recording_refresh_owner_if_writer(self, peer, writer)
            self._drop_session_client_writer(writer)
            await self._close_session_writer(writer, peer)

    async def _handle_session_request(
        self: Any,
        request: JsonObject,
        peer: PeerCredentials,
        writer: asyncio.StreamWriter,
    ) -> JsonObject:
        return await commands.handle_session_request(
            self,
            request,
            peer,
            writer,
        )

    def _broadcast_keymasqd_status(self: Any, connected: bool) -> None:
        message = {
            "event": "keymasqd_status",
            "connected": connected,
        }
        self.broadcast_to_session_clients(cast(JsonObject, message))

    def broadcast_to_session_clients(self: Any, message: JsonObject) -> None:
        self.broadcast_to_session_client_ids(message, None)

    def broadcast_to_session_client_ids(
        self: Any,
        message: JsonObject,
        writer_ids: set[int] | None,
    ) -> None:
        for writer in list(self.session_clients):
            if writer_ids is not None and id(writer) not in writer_ids:
                continue
            try:
                writer.write(json.dumps(message).encode() + b"\n")
                task = self.session_client_drain_tasks.get(writer)
                if task is None or task.done():
                    self.session_client_drain_tasks[writer] = asyncio.create_task(
                        self._drain_session_writer(writer)
                    )
            except OSError:
                peer = self.session_client_peers.get(writer)
                self._drop_session_client_writer(writer)
                asyncio.create_task(self._close_session_writer(writer, peer))
            except Exception:
                log.exception("Unexpected failure broadcasting to session client")
                peer = self.session_client_peers.get(writer)
                self._drop_session_client_writer(writer)
                asyncio.create_task(self._close_session_writer(writer, peer))

    async def _drain_session_writer(self: Any, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(writer.drain(), timeout=2.0)
        except asyncio.CancelledError:
            raise
        except OSError:
            peer = self.session_client_peers.get(writer)
            self._drop_session_client_writer(writer)
            await self._close_session_writer(writer, peer)
        except Exception:
            log.exception("Unexpected failure draining session client writer")
            peer = self.session_client_peers.get(writer)
            self._drop_session_client_writer(writer)
            await self._close_session_writer(writer, peer)
        finally:
            if self.session_client_drain_tasks.get(writer) is asyncio.current_task():
                self.session_client_drain_tasks.pop(writer, None)

    async def _close_session_writer(
        self: Any,
        writer: asyncio.StreamWriter,
        peer: PeerCredentials | None = None,
    ) -> None:
        try:
            writer.close()
            await asyncio.wait_for(
                writer.wait_closed(),
                timeout=self.SESSION_CLIENT_CLOSE_TIMEOUT_S,
            )
        except TimeoutError:
            if peer is None:
                log.debug("Timed out waiting for session client socket to close")
            else:
                log.debug(
                    "Timed out waiting for session client socket to close pid=%s uid=%s",
                    peer.pid,
                    peer.uid,
                )
            transport = getattr(writer, "transport", None)
            if transport is not None:
                try:
                    transport.abort()
                except (OSError, RuntimeError):
                    log.debug("Failed to abort session client transport", exc_info=True)
        except OSError:
            log.debug("Failed while waiting for session client socket to close", exc_info=True)
        except Exception:
            log.exception("Unexpected failure closing session client socket")

    def _drop_session_client_writer(self: Any, writer: asyncio.StreamWriter) -> None:
        self.session_clients.discard(writer)
        self.session_client_peers.pop(writer, None)
        task = self.session_client_drain_tasks.pop(writer, None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _wait_for_session_clients_to_close(self: Any, timeout_s: float = 1.0) -> None:
        if not self.session_clients:
            return

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.05, float(timeout_s))
        while self.session_clients and loop.time() < deadline:
            await asyncio.sleep(0.01)

        if self.session_clients:
            log.debug(
                "Timed out waiting for %s session client(s) to close",
                len(self.session_clients),
            )


async def session_socket_accepts_connections(timeout_s: float = 0.2) -> bool:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.setblocking(False)
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().sock_connect(sock, str(SESSION_SOCKET_PATH)),
            timeout=timeout_s,
        )
    except TimeoutError:
        return True
    except OSError:
        return False
    finally:
        sock.close()
    return True
