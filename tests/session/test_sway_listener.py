import asyncio
import json
import os
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from keymasq.session.listeners.sway import (
    I3IpcError,
    I3IpcMessage,
    SwayListener,
    find_sway_window_by_title,
    pack_i3_message,
    parse_i3_command_results,
    read_i3_message,
    sway_layout_origin,
    sway_node_is_window,
)
from keymasq.session.listeners.wayland_wlr import WlrootsWaylandListener
from tests.async_fakes import make_stream_reader

SWAY_VERSION = {"human_readable": "1.12", "variant": "sway", "major": 1, "minor": 12}
I3_VERSION = {"human_readable": "4.25.1", "major": 4, "minor": 25, "patch": 1}


def _view(
    con_id: int,
    title: str,
    *,
    app_id: str | None = None,
    wm_class: str | None = None,
) -> dict[str, object]:
    # Mirrors Sway's view serializer: every view has "shell"; "app_id" may be
    # null; only Xwayland views carry "window" and "window_properties".
    node: dict[str, object] = {
        "id": con_id,
        "type": "con",
        "name": title,
        "app_id": app_id,
        "shell": "xwayland" if wm_class else "xdg_shell",
        "window": None,
        "nodes": [],
        "floating_nodes": [],
    }
    if wm_class is not None:
        node["window"] = con_id * 100
        node["window_properties"] = {"class": wm_class, "instance": wm_class.lower()}
    return node


def _tree(*nodes: dict[str, object], origin: tuple[int, int] = (0, 0)) -> dict[str, object]:
    workspace = {"id": 3, "type": "workspace", "name": "1", "nodes": list(nodes)}
    output = {"id": 2, "type": "output", "name": "Virtual-1", "nodes": [workspace]}
    return {
        "id": 1,
        "type": "root",
        "name": "root",
        "rect": {"x": origin[0], "y": origin[1], "width": 3840, "height": 1080},
        "nodes": [output],
    }


class FakeSwayServer:
    def __init__(self, socket_path: Path, version: dict[str, object]) -> None:
        self.socket_path = socket_path
        self.version = version
        self.tree: dict[str, object] = _tree()
        self.commands: list[str] = []
        self.command_reply: list[dict[str, object]] = [{"success": True}]
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path))

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                message_type, payload = await read_i3_message(reader)
                if message_type == I3IpcMessage.GET_VERSION:
                    reply: object = self.version
                elif message_type == I3IpcMessage.GET_TREE:
                    reply = self.tree
                elif message_type == I3IpcMessage.RUN_COMMAND:
                    self.commands.append(payload.decode("utf-8"))
                    reply = self.command_reply
                else:
                    reply = {"success": False}
                writer.write(pack_i3_message(message_type, json.dumps(reply)))
                await writer.drain()
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
        yield path


@pytest.fixture
async def sway_server(runtime_dir: Path, monkeypatch) -> AsyncIterator[FakeSwayServer]:
    server = FakeSwayServer(runtime_dir / "sway.sock", SWAY_VERSION)
    await server.start()
    monkeypatch.setenv("SWAYSOCK", str(server.socket_path))
    try:
        yield server
    finally:
        await server.close()


def _set_wayland_probe(monkeypatch: pytest.MonkeyPatch, available: bool) -> None:
    async def _probe(_cls, _dbus=None) -> bool:
        return available

    monkeypatch.setattr(WlrootsWaylandListener, "probe_available", classmethod(_probe))


async def _noop_callback(_window_class: str, _window_title: str, _tags: list[str]) -> None:
    return None


def _ipc_listener(server: FakeSwayServer) -> SwayListener:
    listener = SwayListener(_noop_callback)
    listener.ipc_socket_path = server.socket_path
    return listener


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


def test_sway_node_is_window_accepts_views_without_app_id() -> None:
    assert sway_node_is_window(_view(1, "Untitled"))
    assert sway_node_is_window(_view(2, "Term", app_id="foot"))
    assert sway_node_is_window(_view(3, "Browser", wm_class="Firefox"))
    split = {"id": 4, "type": "con", "name": None, "app_id": None, "window": None}
    assert not sway_node_is_window(split)
    assert not sway_node_is_window({"id": 5, "type": "workspace", "name": "1"})


def test_find_sway_window_by_title_skips_containers() -> None:
    untitled_app = _view(10, "Alpha")
    tree = _tree({"id": 9, "type": "con", "name": "Alpha", "nodes": [untitled_app]})
    assert find_sway_window_by_title(tree, "Alpha") is untitled_app
    assert find_sway_window_by_title(tree, "1") is None


def test_sway_layout_origin_reads_root_rect() -> None:
    assert sway_layout_origin(_tree(origin=(-1920, -200))) == (-1920, -200)
    assert sway_layout_origin({"type": "root"}) == (0, 0)


async def test_sway_probe_rejects_i3_socket(runtime_dir: Path, monkeypatch) -> None:
    _set_wayland_probe(monkeypatch, True)
    server = FakeSwayServer(runtime_dir / "wm.sock", I3_VERSION)
    await server.start()
    try:
        # A real i3 socket in I3SOCK is left to the X11 listener.
        monkeypatch.setenv("I3SOCK", str(server.socket_path))
        assert await SwayListener.probe_available() is False

        server.version = SWAY_VERSION
        assert await SwayListener.probe_available() is True
    finally:
        await server.close()


async def test_sway_probe_requires_foreign_toplevel_support(
    sway_server: FakeSwayServer,
    monkeypatch,
) -> None:
    _set_wayland_probe(monkeypatch, False)
    assert await SwayListener.probe_available() is False


async def test_sway_probe_ignores_default_socket_without_env(
    runtime_dir: Path,
    monkeypatch,
) -> None:
    # Another Sway session of the same user must not be picked up.
    _set_wayland_probe(monkeypatch, True)
    server = FakeSwayServer(runtime_dir / f"sway-ipc.{os.getuid()}.1234.sock", SWAY_VERSION)
    await server.start()
    try:
        assert await SwayListener.probe_available() is False
    finally:
        await server.close()


async def test_sway_start_requires_ipc_socket(runtime_dir: Path) -> None:
    listener = SwayListener(_noop_callback)
    with pytest.raises(RuntimeError, match="Sway socket not available"):
        await listener.start()
    assert listener.running is False


async def test_sway_dispatch_sends_commands_unchanged(sway_server: FakeSwayServer) -> None:
    listener = _ipc_listener(sway_server)
    assert listener.supports_compositor_dispatch is True
    assert await listener.dispatch("floating toggle") == (True, "ok")
    assert await listener.dispatch("workspace number", "2") == (True, "ok")
    assert await listener.dispatch("swaymsg focus left") == (True, "ok")
    assert await listener.dispatch("exec", "foot") == (True, "ok")
    assert sway_server.commands == [
        "floating toggle",
        "workspace number 2",
        "focus left",
        "exec foot",
    ]

    sway_server.command_reply = [{"success": False, "error": "No matching node"}]
    assert await listener.dispatch("focus", "left") == (False, "No matching node")


async def test_sway_set_cursor_position_translates_layout_origin(
    sway_server: FakeSwayServer,
) -> None:
    listener = _ipc_listener(sway_server)
    assert await listener.dispatch("set_cursor_position", "160 120") == (True, "ok")

    # With a monitor at (-1920, -200), global (160, 120) is (2080, 320) from
    # the layout's top-left corner.
    sway_server.tree = _tree(origin=(-1920, -200))
    assert await listener.dispatch("set_cursor_position", "160 120") == (True, "ok")
    assert sway_server.commands == ["seat - cursor set 160 120", "seat - cursor set 2080 320"]

    for bad_args in ("oops", "inf 10", "1e309 10"):
        assert await listener.dispatch("set_cursor_position", bad_args) == (
            False,
            "set_cursor_position expects X Y",
        )
    assert len(sway_server.commands) == 2


async def test_sway_activate_window_by_title_finds_views_without_app_id(
    sway_server: FakeSwayServer,
) -> None:
    sway_server.tree = _tree(_view(10, "Alpha"), _view(11, "Beta", app_id="lab"))
    listener = _ipc_listener(sway_server)
    assert await listener.activate_window_by_title("Alpha") == {
        "found": True,
        "id": 10,
        "title": "Alpha",
    }
    assert sway_server.commands == ["[con_id=10] focus"]
    assert await listener.activate_window_by_title("Gamma") == {"found": False}
