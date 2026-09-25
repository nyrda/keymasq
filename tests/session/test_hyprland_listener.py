import asyncio
import logging
from unittest.mock import AsyncMock, call

import pytest

import keymasq.session.listeners.hyprland as hyprland_module
from keymasq.session.listeners.hyprland import HyprlandListener
from tests.async_fakes import FakeStreamWriter as _FakeWriter
from tests.async_fakes import StallingStreamReader as _StallingReader
from tests.async_fakes import make_stream_reader


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

    async def fake_open_unix_connection(path: str) -> tuple[asyncio.StreamReader, _FakeWriter]:
        assert path == "/tmp/hypr.sock"
        return make_stream_reader([b"100,200"]), writer

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

    async def fake_open_unix_connection(path: str) -> tuple[asyncio.StreamReader, _FakeWriter]:
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

    async def fake_open_unix_connection(path: str) -> tuple[asyncio.StreamReader, _FakeWriter]:
        assert path == "/tmp/hypr.sock"
        return make_stream_reader([b"100,200"]), writer

    monkeypatch.setattr(
        hyprland_module.asyncio,
        "open_unix_connection",
        fake_open_unix_connection,
    )

    with caplog.at_level(logging.ERROR, logger="keymasq-session.listeners.hyprland"):
        assert await listener._send_cmd("cursorpos", read_size=256) == b"100,200"

    assert "Unexpected failure while closing Hyprland command writer" in caplog.text
    assert "close bug" in caplog.text


@pytest.mark.asyncio
async def test_hyprland_activewindow_event_emits_once_for_repeated_events() -> None:
    calls: list[tuple[str, str, list[str]]] = []

    async def callback(
        window_class: str, window_title: str, tags: list[str]
    ) -> None:
        calls.append((window_class, window_title, tags))

    listener = HyprlandListener(callback)
    get_window_tags = AsyncMock(return_value=[])
    listener._get_window_tags = get_window_tags  # type: ignore[method-assign]

    await listener._handle_event("activewindow>>firefox,tab one")
    await listener._handle_event("activewindow>>firefox,tab one")

    assert calls == [("firefox", "tab one", [])]
    assert get_window_tags.await_count == 2


@pytest.mark.asyncio
async def test_hyprland_activewindow_event_emits_on_tag_change_with_same_class_and_title() -> None:
    callback = AsyncMock()
    listener = HyprlandListener(callback)
    listener._get_window_tags = AsyncMock(  # type: ignore[method-assign]
        side_effect=[["work"], ["personal"], ["work"]]
    )

    for address in ("1111", "2222", "1111"):
        await listener._handle_event("activewindow>>kitty,zsh")
        await listener._handle_event(f"activewindowv2>>{address}")

    assert callback.await_args_list == [
        call("kitty", "zsh", ["work"]),
        call("kitty", "zsh", ["personal"]),
        call("kitty", "zsh", ["work"]),
    ]


@pytest.mark.asyncio
async def test_hyprland_activewindow_event_emits_on_title_change() -> None:
    calls: list[tuple[str, str, list[str]]] = []

    async def callback(
        window_class: str, window_title: str, tags: list[str]
    ) -> None:
        calls.append((window_class, window_title, tags))

    listener = HyprlandListener(callback)
    listener._get_window_tags = AsyncMock(return_value=[])  # type: ignore[method-assign]

    await listener._handle_event("activewindow>>firefox,tab one")
    await listener._handle_event("activewindow>>firefox,tab two")

    assert calls == [("firefox", "tab one", []), ("firefox", "tab two", [])]


def _fake_hyprland_commands(
    layer_interactivity: dict[str, bytes],
    active_window: bytes = b'{"class":"kitty","title":"Beta","tags":[]}',
) -> tuple[AsyncMock, list[str]]:
    """Answer Hyprland socket commands for the active window and layer queries."""
    commands: list[str] = []

    async def send_cmd(command: str, read_size: int = 8192) -> bytes | None:
        _ = read_size
        commands.append(command)
        if command == "j/activewindow":
            return active_window
        if command.startswith("repl "):
            for namespace, reply in layer_interactivity.items():
                if f'"{namespace}"' in command:
                    return reply
            return b"0"
        return None

    return AsyncMock(side_effect=send_cmd), commands


@pytest.mark.asyncio
async def test_hyprland_exclusive_layer_reports_focused_layer_until_it_closes() -> None:
    focus_updates: list[tuple[str, str, list[str], str]] = []
    listener: HyprlandListener

    async def callback(window_class: str, window_title: str, tags: list[str]) -> None:
        focus_updates.append((window_class, window_title, tags, listener.active_layer))

    listener = HyprlandListener(callback)
    listener._send_cmd, _commands = _fake_hyprland_commands(  # type: ignore[method-assign]
        {"launcher": b"1", "polkit": b"1"}
    )

    await listener._handle_event("activewindow>>kitty,Beta")
    await listener._handle_event("activewindowv2>>5a5a")
    await listener._handle_event("openlayer>>launcher")
    assert await listener.get_active_window() == ("", "", [])

    # Retitles of the window behind the launcher must not restore it.
    await listener._handle_event("windowtitlev2>>5a5a,Beta")
    await listener._handle_event("activewindow>>kitty,Beta")
    await listener._handle_event("activewindowv2>>5a5a")
    # A second focus layer on top takes over, and closing it hands focus back.
    await listener._handle_event("openlayer>>polkit")
    await listener._handle_event("closelayer>>polkit")
    await listener._handle_event("closelayer>>launcher")

    assert focus_updates == [
        ("kitty", "Beta", [], ""),
        ("", "", [], "launcher"),
        ("", "", [], "polkit"),
        ("", "", [], "launcher"),
        ("kitty", "Beta", [], ""),
    ]
    assert await listener.get_active_window() == ("kitty", "Beta", [])
    assert listener.active_layer == ""


@pytest.mark.asyncio
async def test_hyprland_on_demand_layer_yields_to_window_focus_but_not_retitles() -> None:
    callback = AsyncMock()
    listener = HyprlandListener(callback)
    listener._send_cmd, _commands = _fake_hyprland_commands(  # type: ignore[method-assign]
        {"walker": b"2"}
    )

    await listener._handle_event("activewindow>>kitty,Beta")
    await listener._handle_event("activewindowv2>>5a5a")
    await listener._handle_event("openlayer>>walker")
    await listener._handle_event("windowtitlev2>>5a5a,Beta 2")
    await listener._handle_event("activewindow>>kitty,Beta 2")
    await listener._handle_event("activewindowv2>>5a5a")
    assert await listener.get_active_window() == ("", "", [])

    # Clicking a window moves focus off the on-demand layer.
    await listener._handle_event("activewindow>>firefox,Docs")
    await listener._handle_event("activewindowv2>>6b6b")
    await listener._handle_event("closelayer>>walker")

    assert callback.await_args_list == [
        call("kitty", "Beta", []),
        call("", "", []),
        call("firefox", "Docs", []),
    ]


@pytest.mark.parametrize(
    ("namespace", "reply"),
    [
        ("waybar", b"0"),
        ("launcher", b"eval is only supported with the lua config manager"),
        ('x") os.exit() --/', b"0"),
    ],
    ids=["no-keyboard-interactivity", "hyprlang-config", "hostile-namespace"],
)
@pytest.mark.asyncio
async def test_hyprland_layer_without_keyboard_focus_keeps_active_window(
    namespace: str,
    reply: bytes,
) -> None:
    callback = AsyncMock()
    listener = HyprlandListener(callback)
    listener._send_cmd, commands = _fake_hyprland_commands(  # type: ignore[method-assign]
        {namespace: reply}
    )

    await listener._handle_event("activewindow>>kitty,Beta")
    await listener._handle_event(f"openlayer>>{namespace}")
    await listener._handle_event(f"closelayer>>{namespace}")

    assert callback.await_args_list == [call("kitty", "Beta", [])]
    assert await listener.get_active_window() == ("kitty", "Beta", [])
    layer_query = next(command for command in commands if command.startswith("repl "))
    # hyprctl reads text before a "/" as flags, and the namespace must stay a Lua string.
    assert "/" not in layer_query
    assert "os.exit" not in layer_query
