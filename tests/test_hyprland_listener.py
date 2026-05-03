import asyncio
from unittest.mock import AsyncMock

import pytest

import keymasq.session.listeners.hyprland as hyprland_module
from keymasq.session.listeners.hyprland import HyprlandListener


async def _noop_callback(_window_class: str, _window_title: str, _tags: list[str]) -> None:
    return


@pytest.mark.asyncio
async def test_hyprland_set_cursor_position_uses_movecursor_dispatcher() -> None:
    listener = HyprlandListener(_noop_callback)
    listener.dispatch = AsyncMock(return_value=(True, "ok"))  # type: ignore[method-assign]

    assert await listener.set_cursor_position(123, 456) == (True, "ok")

    listener.dispatch.assert_awaited_once_with("movecursor", "123 456")


class _FakeWriter:
    def __init__(self) -> None:
        self.closed = False
        self.payloads: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.payloads.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _FakeReader:
    def __init__(self, responses: list[bytes]) -> None:
        self._responses = list(responses)

    async def read(self, _size: int) -> bytes:
        if not self._responses:
            return b""
        return self._responses.pop(0)


class _StallingReader:
    async def read(self, _size: int) -> bytes:
        await asyncio.Event().wait()
        return b""


@pytest.mark.asyncio
async def test_hyprland_send_cmd_retries_after_eof(monkeypatch) -> None:
    listener = HyprlandListener(_noop_callback)
    pairs = [
        (_FakeReader([b""]), _FakeWriter()),
        (_FakeReader([b"100,200"]), _FakeWriter()),
    ]

    async def fake_ensure() -> bool:
        if listener._cmd_reader is None or listener._cmd_writer is None:
            if not pairs:
                return False
            listener._cmd_reader, listener._cmd_writer = pairs.pop(0)  # type: ignore[assignment]
        return True

    monkeypatch.setattr(listener, "_ensure_cmd_connection", fake_ensure)

    response = await listener._send_cmd("cursorpos", read_size=256)

    assert response == b"100,200"


@pytest.mark.asyncio
async def test_hyprland_send_cmd_times_out_stalled_read(monkeypatch) -> None:
    listener = HyprlandListener(_noop_callback)
    writer = _FakeWriter()
    listener.cmd_socket_path = "/tmp/hypr.sock"
    listener._cmd_reader = _StallingReader()  # type: ignore[assignment]
    listener._cmd_writer = writer  # type: ignore[assignment]

    monkeypatch.setattr(hyprland_module, "HYPRLAND_COMMAND_TIMEOUT_S", 0.01)

    response = await listener._send_cmd("cursorpos", read_size=256)

    assert response is None
    assert writer.closed is True
