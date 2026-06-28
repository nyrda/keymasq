import logging
from unittest.mock import AsyncMock

import pytest

import keymasq.session.listeners.hyprland as hyprland_module
from keymasq.session.listeners.hyprland import HyprlandListener
from tests.async_fakes import FakeStreamReader as _FakeReader
from tests.async_fakes import FakeStreamWriter as _FakeWriter
from tests.async_fakes import StallingStreamReader as _StallingReader


async def _noop_callback(_window_class: str, _window_title: str, _tags: list[str]) -> None:
    return


@pytest.mark.asyncio
async def test_hyprland_set_cursor_position_uses_movecursor_dispatcher() -> None:
    listener = HyprlandListener(_noop_callback)
    listener.dispatch = AsyncMock(return_value=(True, "ok"))  # type: ignore[method-assign]

    assert await listener.set_cursor_position(123, 456) == (True, "ok")

    listener.dispatch.assert_awaited_once_with("movecursor", "123 456")


@pytest.mark.asyncio
async def test_hyprland_dispatch_set_cursor_position_uses_special_dispatcher() -> None:
    listener = HyprlandListener(_noop_callback)
    listener.set_cursor_position = AsyncMock(  # type: ignore[method-assign]
        return_value=(True, "ok")
    )

    assert await listener.dispatch("set_cursor_position", "123 456") == (True, "ok")

    listener.set_cursor_position.assert_awaited_once_with(123, 456)


@pytest.mark.asyncio
async def test_hyprland_dispatch_movecursor_uses_lua_cursor_dispatcher() -> None:
    listener = HyprlandListener(_noop_callback)
    listener._send_cmd = AsyncMock(return_value=b"ok")  # type: ignore[method-assign]

    assert await listener.dispatch("movecursor", "123 456") == (True, "ok")

    listener._send_cmd.assert_awaited_once_with(
        "dispatch hl.dsp.cursor.move({ x = 123, y = 456 })",
        read_size=4096,
    )


@pytest.mark.asyncio
async def test_hyprland_dispatch_movecursor_rejects_invalid_args() -> None:
    listener = HyprlandListener(_noop_callback)
    listener._send_cmd = AsyncMock(return_value=b"ok")  # type: ignore[method-assign]

    assert await listener.dispatch("movecursor", "123") == (False, "movecursor expects X Y")
    listener._send_cmd.assert_not_awaited()


@pytest.mark.asyncio
async def test_hyprland_dispatch_accepts_raw_lua_dispatchers() -> None:
    listener = HyprlandListener(_noop_callback)
    listener._send_cmd = AsyncMock(return_value=b"ok")  # type: ignore[method-assign]

    command = 'hl.dsp.focus({ workspace = "3" })'
    assert await listener.dispatch(command) == (True, "ok")

    listener._send_cmd.assert_awaited_once_with(f"dispatch {command}", read_size=4096)


@pytest.mark.asyncio
async def test_hyprland_dispatch_accepts_prefixed_raw_lua_dispatchers() -> None:
    listener = HyprlandListener(_noop_callback)
    listener._send_cmd = AsyncMock(return_value=b"ok")  # type: ignore[method-assign]

    command = 'dispatch hl.dsp.focus({ workspace = "3" })'
    assert await listener.dispatch(command) == (True, "ok")

    listener._send_cmd.assert_awaited_once_with(
        'dispatch hl.dsp.focus({ workspace = "3" })',
        read_size=4096,
    )


@pytest.mark.asyncio
async def test_hyprland_dispatch_rejects_raw_lua_args_field() -> None:
    listener = HyprlandListener(_noop_callback)
    listener._send_cmd = AsyncMock(return_value=b"ok")  # type: ignore[method-assign]

    assert await listener.dispatch("hl.dsp.focus", "3") == (
        False,
        "Hyprland 0.55 custom dispatch expects args to be empty",
    )
    listener._send_cmd.assert_not_awaited()


@pytest.mark.asyncio
async def test_hyprland_dispatch_rejects_legacy_dispatcher() -> None:
    listener = HyprlandListener(_noop_callback)
    listener._send_cmd = AsyncMock(return_value=b"ok")  # type: ignore[method-assign]

    assert await listener.dispatch("workspace") == (
        False,
        "Hyprland 0.55 custom dispatch expects an hl.dsp.* Lua expression",
    )
    listener._send_cmd.assert_not_awaited()


@pytest.mark.asyncio
async def test_hyprland_get_active_window_normalizes_tags() -> None:
    listener = HyprlandListener(_noop_callback)
    listener._send_cmd = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            b'{"class":"term","title":"Shell","tags":["one",2,true,null]}',
            b'{"class":"term","title":"Shell","tags":"not-a-list"}',
        ]
    )

    assert await listener.get_active_window() == (
        "term",
        "Shell",
        ["one", "2", "True", "None"],
    )
    assert await listener.get_active_window() == ("term", "Shell", [])


@pytest.mark.asyncio
async def test_hyprland_active_window_logs_malformed_responses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    listener = HyprlandListener(_noop_callback)
    listener._send_cmd = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            b"{not-json",
            b"[]",
        ]
    )

    with caplog.at_level(logging.DEBUG, logger="keymasq-session.listeners.hyprland"):
        assert await listener.get_active_window() == ("", "", [])
        assert await listener._get_window_tags() == []

    assert "Hyprland active window response was malformed JSON" in caplog.text
    assert "Hyprland active window tags response was not a JSON object" in caplog.text


@pytest.mark.asyncio
async def test_hyprland_send_cmd_opens_one_shot_connection(monkeypatch) -> None:
    listener = HyprlandListener(_noop_callback)
    writer = _FakeWriter()
    listener.cmd_socket_path = "/tmp/hypr.sock"

    async def fake_open_unix_connection(path: str) -> tuple[_FakeReader, _FakeWriter]:
        assert path == "/tmp/hypr.sock"
        return _FakeReader([b"100,200"]), writer

    monkeypatch.setattr(
        hyprland_module.asyncio,
        "open_unix_connection",
        fake_open_unix_connection,
    )

    response = await listener._send_cmd("cursorpos", read_size=256)

    assert response == b"100,200"
    assert writer.payloads == [b"cursorpos"]
    assert writer.closed is True


@pytest.mark.asyncio
async def test_hyprland_send_cmd_times_out_stalled_read(monkeypatch) -> None:
    listener = HyprlandListener(_noop_callback)
    writer = _FakeWriter()
    listener.cmd_socket_path = "/tmp/hypr.sock"

    async def fake_open_unix_connection(path: str) -> tuple[_StallingReader, _FakeWriter]:
        assert path == "/tmp/hypr.sock"
        return _StallingReader(), writer

    monkeypatch.setattr(
        hyprland_module.asyncio,
        "open_unix_connection",
        fake_open_unix_connection,
    )

    monkeypatch.setattr(hyprland_module, "HYPRLAND_COMMAND_TIMEOUT_S", 0.01)

    response = await listener._send_cmd("cursorpos", read_size=256)

    assert response is None
    assert writer.closed is True


@pytest.mark.asyncio
async def test_hyprland_send_cmd_logs_unexpected_command_errors(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    listener = HyprlandListener(_noop_callback)
    listener.cmd_socket_path = "/tmp/hypr.sock"

    async def fake_open_unix_connection(path: str) -> tuple[_FakeReader, _FakeWriter]:
        assert path == "/tmp/hypr.sock"
        raise RuntimeError("connect bug")

    monkeypatch.setattr(
        hyprland_module.asyncio,
        "open_unix_connection",
        fake_open_unix_connection,
    )

    with caplog.at_level(logging.ERROR, logger="keymasq-session.listeners.hyprland"):
        assert await listener._send_cmd("cursorpos", read_size=256) is None

    assert "Unexpected Hyprland command failure" in caplog.text
    assert "connect bug" in caplog.text


@pytest.mark.asyncio
async def test_hyprland_send_cmd_logs_unexpected_close_errors(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    listener = HyprlandListener(_noop_callback)
    writer = _FakeWriter(wait_closed_error=RuntimeError("close bug"))
    listener.cmd_socket_path = "/tmp/hypr.sock"

    async def fake_open_unix_connection(path: str) -> tuple[_FakeReader, _FakeWriter]:
        assert path == "/tmp/hypr.sock"
        return _FakeReader([b"100,200"]), writer

    monkeypatch.setattr(
        hyprland_module.asyncio,
        "open_unix_connection",
        fake_open_unix_connection,
    )

    with caplog.at_level(logging.ERROR, logger="keymasq-session.listeners.hyprland"):
        assert await listener._send_cmd("cursorpos", read_size=256) == b"100,200"

    assert "Unexpected failure while closing Hyprland command writer" in caplog.text
    assert "close bug" in caplog.text
