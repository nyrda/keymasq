import asyncio
import json
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
async def test_hyprland_send_cmd_reads_a_reply_split_across_reads(monkeypatch) -> None:
    listener = HyprlandListener(_noop_callback)
    listener.cmd_socket_path = "/tmp/hypr.sock"

    class _ChunkedReader:
        def __init__(self) -> None:
            self.chunks = [b"keymasq-layers\n0x1 1 ", b"launcher", b""]

        async def read(self, _size: int) -> bytes:
            return self.chunks.pop(0)

    async def fake_open_unix_connection(path: str) -> tuple[_ChunkedReader, _FakeWriter]:
        assert path == "/tmp/hypr.sock"
        return _ChunkedReader(), _FakeWriter()

    monkeypatch.setattr(
        hyprland_module.asyncio,
        "open_unix_connection",
        fake_open_unix_connection,
    )

    assert await listener._send_cmd("repl return 1") == b"keymasq-layers\n0x1 1 launcher"


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


class _FakeHyprland:
    """Answer Hyprland socket commands from a model of mapped windows and layers."""

    def __init__(self, *, lua_config: bool = True) -> None:
        self.lua_config = lua_config
        self.active_window: dict[str, object] = {
            "address": "0x5a5a",
            "class": "kitty",
            "title": "Beta",
            "tags": [],
        }
        # Mapped layers by address, as (namespace, keyboard interactivity).
        self.layers: dict[str, tuple[str, int]] = {}
        self.commands: list[str] = []
        self.failing_layer_queries = 0
        self._next_address = 0x100

    def open_layer(self, namespace: str, interactivity: int) -> str:
        address = f"0x{self._next_address:x}"
        self._next_address += 1
        self.layers[address] = (namespace, interactivity)
        return address

    async def send_cmd(self, command: str, read_size: int = 8192) -> bytes | None:
        _ = read_size
        self.commands.append(command)
        if command == "j/activewindow":
            return json.dumps(self.active_window).encode()
        if not command.startswith("repl "):
            return None
        if not self.lua_config:
            return b"eval is only supported with the lua config manager"
        if self.failing_layer_queries:
            # A timed-out or failed command socket request.
            self.failing_layer_queries -= 1
            return None
        lines = ["keymasq-layers"]
        for address, (namespace, interactivity) in self.layers.items():
            in_query = "namespace =" not in command or f'"{namespace}"' in command
            if in_query:
                lines.append(f"{address} {interactivity} {namespace}")
        return "\n".join(lines).encode()


def _listener_with_fake_hyprland(
    hyprland: _FakeHyprland,
) -> tuple[HyprlandListener, list[tuple[str, str, list[str], str]]]:
    """Return a listener on the fake socket and the focus updates it reports."""
    focus_updates: list[tuple[str, str, list[str], str]] = []

    async def callback(window_class: str, window_title: str, tags: list[str]) -> None:
        focus_updates.append((window_class, window_title, tags, listener.active_layer))

    listener = HyprlandListener(callback)
    listener._send_cmd = AsyncMock(side_effect=hyprland.send_cmd)  # type: ignore[method-assign]
    return listener, focus_updates


def _start_without_sockets(monkeypatch: pytest.MonkeyPatch, hyprland: _FakeHyprland) -> None:
    async def open_event_socket(path: str) -> tuple[asyncio.StreamReader, _FakeWriter]:
        assert path == "/tmp/hypr-events.sock"
        hyprland.commands.append("<subscribe to events>")
        return make_stream_reader([]), _FakeWriter()

    monkeypatch.setattr(
        HyprlandListener,
        "_resolve_socket_paths",
        AsyncMock(return_value=("/tmp/hypr-events.sock", "/tmp/hypr.sock")),
    )
    monkeypatch.setattr(hyprland_module.asyncio, "open_unix_connection", open_event_socket)
    monkeypatch.setattr(HyprlandListener, "_listen", AsyncMock())


@pytest.mark.asyncio
async def test_hyprland_exclusive_layer_reports_focused_layer_until_it_closes() -> None:
    hyprland = _FakeHyprland()
    listener, focus_updates = _listener_with_fake_hyprland(hyprland)

    await listener._handle_event("activewindow>>kitty,Beta")
    await listener._handle_event("activewindowv2>>5a5a")
    launcher = hyprland.open_layer("launcher", 1)
    await listener._handle_event("openlayer>>launcher")
    assert await listener.get_active_window() == ("", "", [])

    # Retitles of the window behind the launcher must not restore it.
    await listener._handle_event("windowtitlev2>>5a5a,Beta")
    await listener._handle_event("activewindow>>kitty,Beta")
    await listener._handle_event("activewindowv2>>5a5a")
    # A second focus layer on top takes over, and closing it hands focus back.
    polkit = hyprland.open_layer("polkit", 1)
    await listener._handle_event("openlayer>>polkit")
    del hyprland.layers[polkit]
    await listener._handle_event("closelayer>>polkit")
    del hyprland.layers[launcher]
    await listener._handle_event("closelayer>>launcher")

    assert focus_updates == [
        ("kitty", "Beta", [], ""),
        ("", "", [], "launcher"),
        ("", "", [], "polkit"),
        ("", "", [], "launcher"),
        ("kitty", "Beta", [], ""),
    ]
    assert await listener.get_active_window() == ("kitty", "Beta", [])


@pytest.mark.asyncio
async def test_hyprland_retries_failed_layer_queries() -> None:
    hyprland = _FakeHyprland()
    listener, focus_updates = _listener_with_fake_hyprland(hyprland)

    await listener._handle_event("activewindow>>kitty,Beta")
    hyprland.open_layer("launcher", 1)
    hyprland.failing_layer_queries = 1
    await listener._handle_event("openlayer>>launcher")
    second_launcher = hyprland.open_layer("launcher", 1)
    await listener._handle_event("openlayer>>launcher")

    # The first launcher stays open while the second closes.
    del hyprland.layers[second_launcher]
    hyprland.failing_layer_queries = 1
    await listener._handle_event("closelayer>>launcher")

    assert focus_updates == [("kitty", "Beta", [], ""), ("", "", [], "launcher")]
    assert listener.active_layer == "launcher"


@pytest.mark.asyncio
async def test_hyprland_layer_that_drops_interactivity_returns_focus_to_window() -> None:
    hyprland = _FakeHyprland()
    listener, focus_updates = _listener_with_fake_hyprland(hyprland)

    await listener._handle_event("activewindow>>kitty,Beta")
    await listener._handle_event("activewindowv2>>5a5a")
    panel = hyprland.open_layer("panel", 1)
    await listener._handle_event("openlayer>>panel")
    # The panel hides by dropping keyboard interactivity instead of closing,
    # and Hyprland refocuses the window.
    hyprland.layers[panel] = ("panel", 0)
    await listener._handle_event("activewindow>>kitty,Beta")
    # If the layers cannot be read, the window gets focus back as well.
    hyprland.open_layer("menu", 1)
    await listener._handle_event("openlayer>>menu")
    hyprland.failing_layer_queries = 2
    await listener._handle_event("activewindow>>kitty,Beta")

    assert focus_updates == [
        ("kitty", "Beta", [], ""),
        ("", "", [], "panel"),
        ("kitty", "Beta", [], ""),
        ("", "", [], "menu"),
        ("kitty", "Beta", [], ""),
    ]


@pytest.mark.asyncio
async def test_hyprland_on_demand_layer_yields_to_window_focus_but_not_retitles() -> None:
    hyprland = _FakeHyprland()
    listener, focus_updates = _listener_with_fake_hyprland(hyprland)

    await listener._handle_event("activewindow>>kitty,Beta")
    await listener._handle_event("activewindowv2>>5a5a")
    walker = hyprland.open_layer("walker", 2)
    await listener._handle_event("openlayer>>walker")
    await listener._handle_event("windowtitlev2>>5a5a,Beta 2")
    await listener._handle_event("activewindow>>kitty,Beta 2")
    await listener._handle_event("activewindowv2>>5a5a")
    assert await listener.get_active_window() == ("", "", [])

    # Clicking a window moves focus off the on-demand layer.
    await listener._handle_event("activewindow>>firefox,Docs")
    await listener._handle_event("activewindowv2>>6b6b")
    # A non-interactive sibling opening does not hand focus back to the layer.
    hyprland.open_layer("walker", 0)
    await listener._handle_event("openlayer>>walker")
    assert listener.active_layer == ""
    del hyprland.layers[walker]
    await listener._handle_event("closelayer>>walker")

    assert focus_updates == [
        ("kitty", "Beta", [], ""),
        ("", "", [], "walker"),
        ("firefox", "Docs", [], ""),
    ]


@pytest.mark.asyncio
async def test_hyprland_tracks_each_layer_that_shares_a_namespace() -> None:
    hyprland = _FakeHyprland()
    listener, focus_updates = _listener_with_fake_hyprland(hyprland)

    await listener._handle_event("activewindow>>kitty,Beta")
    first_launcher = hyprland.open_layer("launcher", 1)
    await listener._handle_event("openlayer>>launcher")
    # A non-interactive surface in the launcher's namespace takes no focus.
    hyprland.open_layer("launcher", 0)
    await listener._handle_event("openlayer>>launcher")
    polkit = hyprland.open_layer("polkit", 1)
    await listener._handle_event("openlayer>>polkit")
    second_launcher = hyprland.open_layer("launcher", 1)
    await listener._handle_event("openlayer>>launcher")

    # The older launcher closes; the newest one keeps focus.
    del hyprland.layers[first_launcher]
    await listener._handle_event("closelayer>>launcher")
    assert listener.active_layer == "launcher"
    del hyprland.layers[second_launcher]
    await listener._handle_event("closelayer>>launcher")
    assert listener.active_layer == "polkit"
    del hyprland.layers[polkit]
    await listener._handle_event("closelayer>>polkit")

    assert focus_updates == [
        ("kitty", "Beta", [], ""),
        ("", "", [], "launcher"),
        ("", "", [], "polkit"),
        ("", "", [], "launcher"),
        ("", "", [], "polkit"),
        ("kitty", "Beta", [], ""),
    ]


@pytest.mark.asyncio
async def test_hyprland_start_picks_up_focus_state_from_before_it_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hyprland = _FakeHyprland()
    launcher = hyprland.open_layer("launcher", 1)
    # An on-demand layer may already have lost focus to a window.
    hyprland.open_layer("walker", 2)
    # The first read of the open layers fails, and the listener asks again.
    hyprland.failing_layer_queries = 1
    listener, focus_updates = _listener_with_fake_hyprland(hyprland)
    _start_without_sockets(monkeypatch, hyprland)

    await listener.start()

    # Events from while the state is read must queue up, not get lost.
    assert hyprland.commands[0] == "<subscribe to events>"
    assert listener.active_layer == "launcher"
    assert await listener.get_active_window() == ("", "", [])
    assert listener.unavailable_capabilities == frozenset()

    # The seeded window address marks this as a retitle, not a focus change.
    await listener._handle_event("windowtitlev2>>5a5a,Beta 2")
    await listener._handle_event("activewindow>>kitty,Beta 2")
    assert listener.active_layer == "launcher"

    del hyprland.layers[launcher]
    await listener._handle_event("closelayer>>launcher")
    assert focus_updates == [("kitty", "Beta", [], "")]
    await listener.stop()


@pytest.mark.asyncio
async def test_hyprland_start_leaves_on_demand_layers_to_queued_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hyprland = _FakeHyprland()
    # An older walker surface gave focus to a window before startup.
    older = hyprland.open_layer("walker", 2)
    # The newer one opens after the event socket connects, so its openlayer
    # event waits in the socket while the listener reads the current state.
    hyprland.open_layer("walker", 2)
    listener, _focus_updates = _listener_with_fake_hyprland(hyprland)
    _start_without_sockets(monkeypatch, hyprland)

    await listener.start()
    assert listener.active_layer == ""
    await listener._handle_event("openlayer>>walker")
    assert listener.active_layer == "walker"

    # The older surface closing leaves the newer one focused.
    del hyprland.layers[older]
    await listener._handle_event("closelayer>>walker")
    assert listener.active_layer == "walker"
    await listener.stop()


@pytest.mark.asyncio
async def test_hyprland_start_remembers_on_demand_layers_a_sibling_does_not_refocus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hyprland = _FakeHyprland()
    # The layer gave focus to a window before the listener started.
    hyprland.open_layer("walker", 2)
    listener, _focus_updates = _listener_with_fake_hyprland(hyprland)
    _start_without_sockets(monkeypatch, hyprland)

    await listener.start()
    hyprland.open_layer("walker", 0)
    await listener._handle_event("openlayer>>walker")

    assert listener.active_layer == ""
    await listener.stop()


@pytest.mark.asyncio
async def test_hyprland_start_stops_waiting_for_queued_layer_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hyprland = _FakeHyprland()
    hyprland.open_layer("walker", 2)
    listener, _focus_updates = _listener_with_fake_hyprland(hyprland)
    _start_without_sockets(monkeypatch, hyprland)
    monkeypatch.setattr(hyprland_module, "HYPRLAND_STARTUP_LAYER_WINDOW_S", 0.0)

    await listener.start()
    # A later layer in the namespace opened and closed before the query saw it.
    await listener._handle_event("openlayer>>walker")

    assert listener.active_layer == ""
    await listener.stop()


@pytest.mark.asyncio
async def test_hyprland_without_lua_config_keeps_windows_and_drops_layer_focus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hyprland = _FakeHyprland(lua_config=False)
    listener, focus_updates = _listener_with_fake_hyprland(hyprland)
    _start_without_sockets(monkeypatch, hyprland)

    await listener.start()
    assert listener.unavailable_capabilities == frozenset({"layer_focus"})

    await listener._handle_event("activewindow>>kitty,Beta")
    hyprland.open_layer("launcher", 1)
    await listener._handle_event("openlayer>>launcher")

    assert focus_updates == [("kitty", "Beta", [], "")]
    assert await listener.get_active_window() == ("kitty", "Beta", [])
    await listener.stop()


@pytest.mark.asyncio
async def test_hyprland_tracks_a_focus_layer_without_a_namespace() -> None:
    hyprland = _FakeHyprland()
    listener, focus_updates = _listener_with_fake_hyprland(hyprland)

    await listener._handle_event("activewindow>>kitty,Beta")
    hyprland.open_layer("", 1)
    await listener._handle_event("openlayer>>")

    assert focus_updates == [("kitty", "Beta", [], ""), ("", "", [], "")]
    assert await listener.get_active_window() == ("", "", [])


@pytest.mark.parametrize(
    "namespace",
    ["waybar", 'x") os.exit() --/'],
    ids=["no-keyboard-interactivity", "hostile-namespace"],
)
@pytest.mark.asyncio
async def test_hyprland_layer_without_keyboard_focus_keeps_active_window(namespace: str) -> None:
    hyprland = _FakeHyprland()
    listener, focus_updates = _listener_with_fake_hyprland(hyprland)

    await listener._handle_event("activewindow>>kitty,Beta")
    hyprland.open_layer(namespace, 0)
    await listener._handle_event(f"openlayer>>{namespace}")
    hyprland.layers.clear()
    await listener._handle_event(f"closelayer>>{namespace}")

    assert focus_updates == [("kitty", "Beta", [], "")]
    assert await listener.get_active_window() == ("kitty", "Beta", [])
    layer_query = next(command for command in hyprland.commands if command.startswith("repl "))
    # hyprctl reads text before a "/" as flags, and the namespace must stay a Lua string.
    assert "/" not in layer_query
    assert "os.exit" not in layer_query
