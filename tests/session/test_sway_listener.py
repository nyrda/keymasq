import asyncio
import json
import os
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from keymasq.session.listeners import layer_shell as layer_shell_module
from keymasq.session.listeners.sway import (
    I3IpcError,
    I3IpcEvent,
    I3IpcMessage,
    SwayListener,
    find_i3_focused_window,
    find_i3_window_by_title,
    i3_window_info,
    pack_i3_message,
    parse_i3_command_results,
    read_i3_message,
)
from tests.async_fakes import make_stream_reader

SWAY_VERSION = {"human_readable": "1.12", "variant": "sway", "major": 1, "minor": 12}
I3_VERSION = {"human_readable": "4.25.1", "major": 4, "minor": 25, "patch": 1}


def _window(
    con_id: int,
    title: str,
    *,
    app_id: str | None = None,
    wm_class: str | None = None,
    focused: bool = False,
) -> dict[str, object]:
    node: dict[str, object] = {
        "id": con_id,
        "type": "con",
        "name": title,
        "focused": focused,
        "nodes": [],
        "floating_nodes": [],
        "focus": [],
    }
    if app_id is not None:
        node["app_id"] = app_id
    if wm_class is not None:
        node["window"] = con_id * 100
        node["window_properties"] = {"class": wm_class, "instance": wm_class.lower()}
    return node


def _tree(*windows: dict[str, object], workspace_focused: bool = False) -> dict[str, object]:
    workspace = {
        "id": 3,
        "type": "workspace",
        "name": "1",
        "focused": workspace_focused,
        "nodes": list(windows),
        "floating_nodes": [],
        "focus": [window["id"] for window in windows],
    }
    output = {"id": 2, "type": "output", "name": "Virtual-1", "focused": False}
    output["nodes"] = [workspace]
    output["focus"] = [3]
    return {"id": 1, "type": "root", "name": "root", "focused": False, "nodes": [output]}


class FakeI3Server:
    def __init__(self, socket_path: Path, version: dict[str, object]) -> None:
        self.socket_path = socket_path
        self.version = version
        self.tree: dict[str, object] = _tree()
        self.commands: list[str] = []
        self.command_reply: list[dict[str, object]] = [{"success": True}]
        self.subscriptions: list[list[str]] = []
        self._subscribers: list[asyncio.StreamWriter] = []
        self._server: asyncio.Server | None = None
        self.subscribed = asyncio.Event()

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path))

    async def close(self) -> None:
        for writer in self._subscribers:
            writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def emit(self, event_type: int, payload: dict[str, object]) -> None:
        for writer in self._subscribers:
            writer.write(pack_i3_message(event_type, json.dumps(payload)))
            await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                message_type, payload = await read_i3_message(reader)
                body = payload.decode("utf-8")
                if message_type == I3IpcMessage.GET_VERSION:
                    reply: object = self.version
                elif message_type == I3IpcMessage.GET_TREE:
                    reply = self.tree
                elif message_type == I3IpcMessage.RUN_COMMAND:
                    self.commands.append(body)
                    reply = self.command_reply
                elif message_type == I3IpcMessage.SUBSCRIBE:
                    self.subscriptions.append(json.loads(body))
                    self._subscribers.append(writer)
                    reply = {"success": True}
                else:
                    reply = {"success": False}
                writer.write(pack_i3_message(message_type, json.dumps(reply)))
                await writer.drain()
                if message_type == I3IpcMessage.SUBSCRIBE:
                    self.subscribed.set()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            # Server.wait_closed() waits for every connection to close.
            writer.close()


@pytest.fixture
def runtime_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # Keep AF_UNIX paths short and hide the host session's real sockets.
    with tempfile.TemporaryDirectory(prefix="kmq-sway-") as temp_dir:
        path = Path(temp_dir)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(path))
        monkeypatch.delenv("SWAYSOCK", raising=False)
        monkeypatch.delenv("I3SOCK", raising=False)

        async def _no_layer_shell() -> None:
            return None

        monkeypatch.setattr(layer_shell_module, "pick_layer_shell_cursor_socket", _no_layer_shell)
        yield path


@pytest.fixture
async def sway_server(runtime_dir: Path, monkeypatch) -> AsyncIterator[FakeI3Server]:
    server = FakeI3Server(runtime_dir / "sway.sock", SWAY_VERSION)
    await server.start()
    monkeypatch.setenv("SWAYSOCK", str(server.socket_path))
    try:
        yield server
    finally:
        await server.close()


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list[str]]] = []
        self.changed = asyncio.Event()

    async def __call__(self, window_class: str, window_title: str, tags: list[str]) -> None:
        self.calls.append((window_class, window_title, tags))
        self.changed.set()

    async def wait_for(self, expected: tuple[str, str]) -> None:
        async def _wait() -> None:
            while not self.calls or self.calls[-1][:2] != expected:
                self.changed.clear()
                await self.changed.wait()

        await asyncio.wait_for(_wait(), timeout=2)


async def test_i3_message_round_trip() -> None:
    packed = pack_i3_message(I3IpcMessage.RUN_COMMAND, "focus left")
    message_type, payload = await read_i3_message(make_stream_reader([packed]))
    assert message_type == I3IpcMessage.RUN_COMMAND
    assert payload == b"focus left"


async def test_i3_message_rejects_bad_magic() -> None:
    packed = b"i4-ipc" + pack_i3_message(I3IpcMessage.GET_TREE)[6:]
    with pytest.raises(I3IpcError):
        await read_i3_message(make_stream_reader([packed]))


def test_parse_i3_command_results() -> None:
    assert parse_i3_command_results([{"success": True}, {"success": True}]) == (True, "ok")
    assert parse_i3_command_results([{"success": False, "error": "nope"}]) == (False, "nope")
    assert parse_i3_command_results([]) == (False, "empty command reply")
    assert parse_i3_command_results({"success": True}) == (False, "invalid command reply")


def test_find_i3_focused_window_follows_focus_stack() -> None:
    alpha = _window(10, "Alpha", app_id="lab")
    beta = _window(11, "Beta", app_id="lab", focused=True)
    assert find_i3_focused_window(_tree(alpha, beta)) is beta

    split = {
        "id": 20,
        "type": "con",
        "name": None,
        "focused": True,
        "nodes": [alpha, beta],
        "floating_nodes": [],
        "focus": [10, 11],
    }
    beta["focused"] = False
    assert find_i3_focused_window(_tree(split)) is alpha


def test_find_i3_focused_window_empty_workspace() -> None:
    assert find_i3_focused_window(_tree(workspace_focused=True)) is None


def test_i3_window_info_prefers_app_id_then_x11_class() -> None:
    assert i3_window_info(_window(1, "Term", app_id="foot")) == ("foot", "Term")
    assert i3_window_info(_window(2, "Browser", wm_class="Firefox")) == ("Firefox", "Browser")
    assert i3_window_info(None) == ("", "")


def test_find_i3_window_by_title_skips_containers() -> None:
    alpha = _window(10, "Alpha", wm_class="Lab")
    tree = _tree(alpha)
    assert find_i3_window_by_title(tree, "Alpha") is alpha
    assert find_i3_window_by_title(tree, "1") is None


async def test_sway_probe_rejects_i3_socket(runtime_dir: Path, monkeypatch) -> None:
    server = FakeI3Server(runtime_dir / "wm.sock", I3_VERSION)
    await server.start()
    try:
        # A real i3 socket in I3SOCK is left to the X11 listener.
        monkeypatch.setenv("I3SOCK", str(server.socket_path))
        assert await SwayListener.probe_available() is False

        server.version = SWAY_VERSION
        assert await SwayListener.probe_available() is True
    finally:
        await server.close()


async def test_sway_probe_finds_default_socket_without_env(runtime_dir: Path) -> None:
    server = FakeI3Server(runtime_dir / f"sway-ipc.{os.getuid()}.1234.sock", SWAY_VERSION)
    await server.start()
    try:
        assert await SwayListener.resolve_socket_path() == server.socket_path
    finally:
        await server.close()


async def test_probe_without_sockets(runtime_dir: Path) -> None:
    assert await SwayListener.probe_available() is False


async def test_sway_listener_tracks_focus_title_close_and_workspace(
    sway_server: FakeI3Server,
) -> None:
    alpha = _window(10, "Alpha", app_id="lab")
    beta = _window(11, "Beta", app_id="lab", focused=True)
    sway_server.tree = _tree(alpha, beta)
    recorder = _Recorder()
    listener = SwayListener(recorder)

    await listener.start()
    try:
        assert sway_server.subscriptions == [["window", "workspace", "shutdown"]]
        assert recorder.calls == [("lab", "Beta", [])]
        assert await listener.get_active_window() == ("lab", "Beta", [])
        assert listener.compositor_dispatch_available is True

        await sway_server.emit(I3IpcEvent.WINDOW, {"change": "focus", "container": alpha})
        await recorder.wait_for(("lab", "Alpha"))

        renamed_beta = dict(beta, name="BetaRenamed", focused=False)
        await sway_server.emit(I3IpcEvent.WINDOW, {"change": "title", "container": renamed_beta})
        renamed_alpha = dict(alpha, name="AlphaRenamed", focused=True)
        await sway_server.emit(I3IpcEvent.WINDOW, {"change": "title", "container": renamed_alpha})
        await recorder.wait_for(("lab", "AlphaRenamed"))
        assert ("lab", "BetaRenamed", []) not in recorder.calls

        sway_server.tree = _tree(beta)
        await sway_server.emit(I3IpcEvent.WINDOW, {"change": "close", "container": alpha})
        await recorder.wait_for(("lab", "Beta"))

        sway_server.tree = _tree(workspace_focused=True)
        await sway_server.emit(I3IpcEvent.WORKSPACE, {"change": "focus"})
        await recorder.wait_for(("", ""))
    finally:
        await listener.stop()
    assert listener.running is False


async def test_sway_listener_stops_on_shutdown_event(sway_server: FakeI3Server) -> None:
    listener = SwayListener(_Recorder())
    await listener.start()
    try:
        assert await listener.health_check() is True
        await sway_server.emit(I3IpcEvent.SHUTDOWN, {"change": "exit"})
        assert listener._task is not None
        await asyncio.wait_for(listener._task, timeout=2)
        assert await listener.health_check() is False
    finally:
        await listener.stop()


async def test_sway_dispatch_sends_commands_unchanged(sway_server: FakeI3Server) -> None:
    listener = SwayListener(_Recorder())
    await listener.start()
    try:
        assert await listener.dispatch("floating toggle") == (True, "ok")
        assert await listener.dispatch("workspace number", "2") == (True, "ok")
        assert await listener.dispatch("swaymsg focus left") == (True, "ok")
        assert sway_server.commands == ["floating toggle", "workspace number 2", "focus left"]

        assert await listener.dispatch("exec", "foot") == (True, "ok")
        assert sway_server.commands[-1] == "exec foot"

        sway_server.command_reply = [{"success": False, "error": "No matching node"}]
        assert await listener.dispatch("focus", "left") == (False, "No matching node")
    finally:
        await listener.stop()


async def test_sway_set_cursor_position_uses_current_seat(sway_server: FakeI3Server) -> None:
    listener = SwayListener(_Recorder())
    await listener.start()
    try:
        assert await listener.dispatch("set_cursor_position", "160 120") == (True, "ok")
        assert sway_server.commands == ["seat - cursor set 160 120"]
        assert await listener.dispatch("set_cursor_position", "oops") == (
            False,
            "set_cursor_position expects X Y",
        )
    finally:
        await listener.stop()


async def test_sway_activate_window_by_title(sway_server: FakeI3Server) -> None:
    sway_server.tree = _tree(_window(10, "Alpha", app_id="lab"), _window(11, "Beta", app_id="lab"))
    listener = SwayListener(_Recorder())
    await listener.start()
    try:
        assert await listener.activate_window_by_title("Alpha") == {
            "found": True,
            "id": 10,
            "title": "Alpha",
        }
        assert sway_server.commands == ["[con_id=10] focus"]
        assert await listener.activate_window_by_title("Gamma") == {"found": False}
    finally:
        await listener.stop()
