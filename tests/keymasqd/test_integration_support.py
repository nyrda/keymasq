from keymasq.common.ipc import Command, CommandType, Response, encode_response
from tests.keymasqd.integration_support import IntegrationTestBase


class _ChunkedReader:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def read(self, _size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        return


async def test_integration_send_command_reads_fragmented_large_response() -> None:
    payload = {"value": "x" * 5000}
    encoded = encode_response(Response(status="ok", data=payload))
    reader = _ChunkedReader([encoded[:3], encoded[3:4096], encoded[4096:]])
    writer = _Writer()

    result = await IntegrationTestBase()._send_command(
        reader,
        writer,
        Command(command=CommandType.PING),
    )

    assert result == payload
    assert writer.data
