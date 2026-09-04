import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


def make_stream_reader(chunks: list[bytes]) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    reader.feed_eof()
    return reader


class BlockingStreamReader:
    def __init__(self) -> None:
        self._release = asyncio.Event()

    async def read(self, _size: int) -> bytes:
        await self._release.wait()
        return b""

    async def readline(self) -> bytes:
        await self._release.wait()
        return b""

    def release(self) -> None:
        self._release.set()


class StallingStreamReader:
    async def read(self, _size: int) -> bytes:
        await asyncio.Event().wait()
        return b""

    async def readline(self) -> bytes:
        await asyncio.Event().wait()
        return b""


class FakeStreamWriter:
    def __init__(
        self,
        *,
        drain_waiter: asyncio.Event | None = None,
        drain_error: Exception | None = None,
        wait_closed_error: Exception | None = None,
        write_error: Exception | None = None,
        payload_decoder: Callable[[bytes], Any] | None = None,
    ) -> None:
        self.writes: list[bytes] = []
        self.payloads: list[Any] = []
        self.drain_calls = 0
        self.closed = False
        self.wait_closed_calls = 0
        self.wait_closed_error = wait_closed_error
        self._socket = object()
        self._drain_waiter = drain_waiter
        self._drain_error = drain_error
        self._write_error = write_error
        self._payload_decoder = payload_decoder

    def get_extra_info(self, name: str) -> object | None:
        if name == "socket":
            return self._socket
        return None

    def write(self, data: bytes) -> None:
        if self._write_error is not None:
            raise self._write_error
        self.writes.append(data)
        if self._payload_decoder is None:
            self.payloads.append(data)
        else:
            self.payloads.append(self._payload_decoder(data))

    async def drain(self) -> None:
        self.drain_calls += 1
        if self._drain_error is not None:
            raise self._drain_error
        if self._drain_waiter is not None:
            await self._drain_waiter.wait()

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.wait_closed_calls += 1
        if self.wait_closed_error is not None:
            raise self.wait_closed_error


class HangingStreamWriter(FakeStreamWriter):
    def __init__(self) -> None:
        super().__init__()
        self.abort_calls = 0
        self.transport = self

    async def wait_closed(self) -> None:
        self.wait_closed_calls += 1
        await asyncio.Event().wait()

    def abort(self) -> None:
        self.abort_calls += 1


class FakeProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        communicate: Callable[[], Awaitable[tuple[bytes, bytes]]] | None = None,
        wait: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.communicate_calls = 0
        self.wait_calls = 0
        self.terminated = False
        self.killed = False
        self.waited = False
        self._communicate = communicate
        self._wait = wait

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicate_calls += 1
        if self._communicate is not None:
            return await self._communicate()
        return self.stdout, self.stderr

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self) -> Awaitable[Any]:
        self.wait_calls += 1
        self.waited = True
        if self._wait is not None:
            return self._wait()

        async def _wait() -> int:
            return self.returncode

        return _wait()
