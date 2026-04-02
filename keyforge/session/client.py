import asyncio
import logging
from typing import Any

from keyforge.common.ipc import (
    Command,
    CommandType,
    Response,
    decode_response,
    encode_command,
)
from keyforge.common.paths import SOCKET_PATH

log = logging.getLogger("keyforge-session.client")


class KeyforgedClient:
    def __init__(self, event_handler: Any) -> None:
        self.event_handler = event_handler
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._buffer = b""
        self._pending_requests: dict[str, asyncio.Future] = {}
        self._request_counter = 0
        self._listen_task: asyncio.Task | None = None
        self._disconnected_event = asyncio.Event()

    async def connect(self) -> None:
        self._disconnected_event.clear()
        self.reader, self.writer = await asyncio.open_unix_connection(str(SOCKET_PATH))
        self._listen_task = asyncio.create_task(self._listen_loop())

    async def disconnect(self) -> None:
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None

        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass

        self.reader = None
        self.writer = None

    async def send_command(self, command: Command, timeout: float = 10.0) -> Response:
        if not self.writer:
            raise RuntimeError("Not connected to keyforged")

        self._request_counter += 1
        request_id = str(self._request_counter)
        command.request_id = request_id

        future: asyncio.Future[Response] = asyncio.get_event_loop().create_future()
        self._pending_requests[request_id] = future

        try:
            self.writer.write(encode_command(command))
            await self.writer.drain()

            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending_requests.pop(request_id, None)

    async def _listen_loop(self) -> None:
        if not self.reader:
            return

        try:
            while True:
                data = await self.reader.read(4096)
                if not data:
                    break

                self._buffer += data

                while True:
                    response, remaining = decode_response(self._buffer)
                    if response is None:
                        break

                    self._buffer = remaining
                    await self._handle_response(response)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"Listen error: {e}")
        finally:
            self._finalize_disconnect()

    async def wait_disconnected(self) -> None:
        await self._disconnected_event.wait()

    async def _handle_response(self, response: Response) -> None:
        if response.request_id and response.request_id in self._pending_requests:
            future = self._pending_requests[response.request_id]
            if not future.done():
                future.set_result(response)

        elif response.status == "event":
            try:
                event_type = CommandType(response.data.get("command"))
                data = response.data.get("data", {})
                await self.event_handler(event_type, data)
            except Exception as e:
                log.error(f"Event handler error: {e}")

    def _finalize_disconnect(self) -> None:
        error = ConnectionError("Disconnected from keyforged")
        for future in list(self._pending_requests.values()):
            if not future.done():
                future.set_exception(error)

        writer = self.writer
        self.reader = None
        self.writer = None
        self._buffer = b""

        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass

        self._disconnected_event.set()
