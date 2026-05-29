import asyncio
import struct
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from keymasq.session.wayland_protocols import cosmic_toplevel_info_client as cosmic_client_module
from keymasq.session.wayland_protocols import registry_probe
from keymasq.session.wayland_protocols import wlr_foreign_toplevel_client as wlr_client_module
from keymasq.session.wayland_protocols.ext_foreign_toplevel_list import (
    ExtForeignToplevelListTracker,
)
from keymasq.session.wayland_protocols.ext_foreign_toplevel_list_client import (
    EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE,
    ExtForeignToplevelListWaylandClient,
)
from keymasq.session.wayland_protocols.wlr_foreign_toplevel_manager import (
    WLR_TOPLEVEL_STATE_ACTIVATED,
    WlrForeignToplevelManagerTracker,
)


class _FakeWaylandSocket:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def close(self) -> None:
        self.closed = True


class _SendallFailsSocket:
    def __init__(self) -> None:
        self.sendall_called = False

    def sendall(self, _data: bytes) -> None:
        self.sendall_called = True
        raise BlockingIOError


class _FakeSockSendallLoop:
    def __init__(self) -> None:
        self.sent: list[tuple[object, bytes]] = []

    async def sock_sendall(self, sock: object, data: bytes) -> None:
        self.sent.append((sock, data))


def _wl_message(object_id: int, opcode: int, payload: bytes = b"") -> bytes:
    size = 8 + len(payload)
    return struct.pack("<II", object_id, ((size & 0xFFFF) << 16) | (opcode & 0xFFFF)) + payload


def _fake_send_request_recorder(fake_socket: _FakeWaylandSocket) -> Any:
    async def send_request(object_id: int, opcode: int, payload: bytes) -> None:
        fake_socket.sendall(_wl_message(object_id, opcode, payload))

    return send_request


def _registry_payload(global_name: int, interface: str, version: int) -> bytes:
    return struct.pack("<I", global_name) + _encode_string(interface) + struct.pack("<I", version)


def _truncated_registry_payload(global_name: int, interface: str) -> bytes:
    raw = interface.encode("utf-8")
    return struct.pack("<II", global_name, len(raw) + 32) + raw


def _encode_string(value: str) -> bytes:
    encoded = value.encode("utf-8") + b"\x00"
    padded = (len(encoded) + 3) & ~3
    return struct.pack("<I", len(encoded)) + encoded + (b"\x00" * (padded - len(encoded)))


def _encode_array(values: list[int]) -> bytes:
    raw = b"".join(value.to_bytes(4, byteorder="little") for value in values)
    padded = (len(raw) + 3) & ~3
    return struct.pack("<I", len(raw)) + raw + (b"\x00" * (padded - len(raw)))


@contextmanager
def _short_socket_path(name: str) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="kmq-", dir="/tmp") as temp_dir:
        yield Path(temp_dir) / name


def test_ext_tracker_emits_on_activation() -> None:
    tracker = ExtForeignToplevelListTracker()
    tracker.add_toplevel("a")
    tracker.update_app_id("a", "org.kde.konsole")
    tracker.update_title("a", "Konsole")
    tracker.update_state("a", [2])

    assert tracker.get_active_window() == ("org.kde.konsole", "Konsole")
    assert asyncio.run(tracker.next_active_window(timeout=0.01)) == (
        "org.kde.konsole",
        "Konsole",
    )


def test_ext_tracker_emits_on_active_title_change() -> None:
    tracker = ExtForeignToplevelListTracker()
    tracker.add_toplevel("a")
    tracker.update_app_id("a", "firefox")
    tracker.update_title("a", "A")
    tracker.update_state("a", {"activated": True})
    asyncio.run(tracker.next_active_window(timeout=0.01))

    tracker.update_title("a", "B")
    assert asyncio.run(tracker.next_active_window(timeout=0.01)) == ("firefox", "B")


def test_ext_tracker_emits_on_byte_state_activation() -> None:
    tracker = ExtForeignToplevelListTracker()
    tracker.add_toplevel("x")
    tracker.update_app_id("x", "com.system76.Cosmic")
    tracker.update_title("x", "COSMIC Settings")

    state = (2).to_bytes(4, byteorder="little")
    tracker.update_state("x", state)

    assert tracker.get_active_window() == ("com.system76.Cosmic", "COSMIC Settings")
    assert asyncio.run(tracker.next_active_window(timeout=0.01)) == (
        "com.system76.Cosmic",
        "COSMIC Settings",
    )


def test_wlr_tracker_emits_on_byte_state_activation() -> None:
    tracker = WlrForeignToplevelManagerTracker()
    tracker.add_toplevel("x")
    tracker.update_app_id("x", "Alacritty")
    tracker.update_title("x", "shell")

    state = WLR_TOPLEVEL_STATE_ACTIVATED.to_bytes(4, byteorder="little")
    tracker.update_state("x", state)

    assert tracker.get_active_window() == ("Alacritty", "shell")
    assert asyncio.run(tracker.next_active_window(timeout=0.01)) == ("Alacritty", "shell")


def test_wlr_tracker_switches_focus_between_handles() -> None:
    tracker = WlrForeignToplevelManagerTracker()
    tracker.add_toplevel("a")
    tracker.add_toplevel("b")
    tracker.update_app_id("a", "a-app")
    tracker.update_title("a", "A")
    tracker.update_app_id("b", "b-app")
    tracker.update_title("b", "B")

    tracker.update_state("a", [WLR_TOPLEVEL_STATE_ACTIVATED])
    assert asyncio.run(tracker.next_active_window(timeout=0.01)) == ("a-app", "A")

    tracker.update_state("a", [])
    tracker.update_state("b", [WLR_TOPLEVEL_STATE_ACTIVATED])
    assert asyncio.run(tracker.next_active_window(timeout=0.01)) == ("", "")
    assert asyncio.run(tracker.next_active_window(timeout=0.01)) == ("b-app", "B")


class _ProbeSocket:
    def __init__(self, *responses: bytes) -> None:
        self.responses = list(responses)
        self.sent: list[bytes] = []
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        return

    def connect(self, _path: str) -> None:
        return

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, size: int) -> bytes:
        if not self.responses:
            return b""
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def test_registry_probe_reads_globals_sync(monkeypatch) -> None:
    probe_socket = _ProbeSocket(
        _wl_message(
            2,
            0,
            _registry_payload(7, EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE, 1),
        )
        + _wl_message(2, 0, _registry_payload(8, "zwlr_foreign_toplevel_manager_v1", 3))
        + _wl_message(3, 0)
    )
    monkeypatch.setattr(
        registry_probe.socket,
        "socket",
        lambda _family, _type: probe_socket,
    )

    assert registry_probe.list_registry_globals_sync(Path("unused"), timeout_s=2.0) == {
        EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE,
        "zwlr_foreign_toplevel_manager_v1",
    }
    assert len(probe_socket.sent) == 2
    assert probe_socket.closed is True


def test_registry_probe_ignores_truncated_global_interface_sync(monkeypatch) -> None:
    probe_socket = _ProbeSocket(
        _wl_message(
            2,
            0,
            _truncated_registry_payload(
                7,
                wlr_client_module.WLR_FOREIGN_TOPLEVEL_MANAGER_INTERFACE,
            ),
        )
        + _wl_message(2, 0, _registry_payload(8, EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE, 1))
        + _wl_message(3, 0)
    )
    monkeypatch.setattr(
        registry_probe.socket,
        "socket",
        lambda _family, _type: probe_socket,
    )

    assert registry_probe.list_registry_globals_sync(Path("unused"), timeout_s=2.0) == {
        EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE
    }


def test_registry_probe_reads_globals_async() -> None:
    async def run_probe() -> set[str]:
        with _short_socket_path("wayland-async") as socket_path:

            async def handle_client(
                reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter,
            ) -> None:
                await reader.read(4096)
                writer.write(
                    _wl_message(2, 0, _registry_payload(1, "zcosmic_toplevel_info_v1", 3))
                    + _wl_message(3, 0)
                )
                await writer.drain()
                await asyncio.wait_for(reader.read(4096), timeout=1.0)
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(handle_client, path=str(socket_path))
            try:
                return await registry_probe.list_registry_globals(socket_path)
            finally:
                server.close()
                await server.wait_closed()

    assert asyncio.run(run_probe()) == {"zcosmic_toplevel_info_v1"}


def test_registry_probe_ignores_truncated_global_interface_async() -> None:
    async def run_probe() -> set[str]:
        with _short_socket_path("wayland-async-truncated") as socket_path:

            async def handle_client(
                reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter,
            ) -> None:
                await reader.read(4096)
                writer.write(
                    _wl_message(
                        2,
                        0,
                        _truncated_registry_payload(
                            1,
                            wlr_client_module.WLR_FOREIGN_TOPLEVEL_MANAGER_INTERFACE,
                        ),
                    )
                    + _wl_message(
                        2,
                        0,
                        _registry_payload(2, EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE, 1),
                    )
                    + _wl_message(3, 0)
                )
                await writer.drain()
                await asyncio.wait_for(reader.read(4096), timeout=1.0)
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(handle_client, path=str(socket_path))
            try:
                return await registry_probe.list_registry_globals(socket_path)
            finally:
                server.close()
                await server.wait_closed()

    assert asyncio.run(run_probe()) == {EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE}


def test_wayland_clients_send_requests_with_loop_sock_sendall() -> None:
    async def send_requests() -> None:
        clients = (
            ExtForeignToplevelListWaylandClient(ExtForeignToplevelListTracker()),
            wlr_client_module.WlrForeignToplevelWaylandClient(
                WlrForeignToplevelManagerTracker()
            ),
            cosmic_client_module.CosmicToplevelInfoWaylandClient(
                ExtForeignToplevelListTracker()
            ),
        )
        for client in clients:
            fake_socket = _SendallFailsSocket()
            fake_loop = _FakeSockSendallLoop()
            client._socket = cast(Any, fake_socket)
            client._loop = cast(Any, fake_loop)

            await client._send_request(11, 7, b"payload")

            assert fake_socket.sendall_called is False
            assert fake_loop.sent == [(fake_socket, _wl_message(11, 7, b"payload"))]

    asyncio.run(send_requests())


def test_ext_wayland_client_dispatches_registry_and_toplevel_events() -> None:
    tracker = ExtForeignToplevelListTracker()
    client = ExtForeignToplevelListWaylandClient(tracker)
    fake_socket = _FakeWaylandSocket()
    client._socket = fake_socket
    client._send_request = _fake_send_request_recorder(fake_socket)
    client._registry_id = 2
    client._objects[2] = client._objects[1].__class__("wl_registry")

    asyncio.run(
        client._handle_registry_event(
            2,
            0,
            _registry_payload(12, EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE, 9),
        )
    )
    assert client._list_id is not None
    assert fake_socket.sent

    client._handle_list_event(client._list_id, 0, struct.pack("<I", 40))
    client._handle_toplevel_event(40, 2, _encode_string("Terminal"))
    client._handle_toplevel_event(40, 3, _encode_string("org.example.Terminal"))
    tracker.update_state("40", [2])
    assert tracker.get_active_window() == ("org.example.Terminal", "Terminal")

    client._handle_callback_event(99, 1)
    client._sync_waiters.add(99)
    client._handle_callback_event(99, 0)
    assert 99 not in client._sync_waiters

    client._handle_toplevel_event(40, 0, b"")
    assert "40" not in tracker._windows
    client._handle_display_event(1, struct.pack("<I", 40))
    asyncio.run(client.stop())
    assert fake_socket.closed is True


def test_wlr_wayland_client_dispatches_manager_and_toplevel_events() -> None:
    tracker = WlrForeignToplevelManagerTracker()
    client = wlr_client_module.WlrForeignToplevelWaylandClient(tracker)
    fake_socket = _FakeWaylandSocket()
    client._socket = fake_socket
    client._send_request = _fake_send_request_recorder(fake_socket)
    client._registry_id = 2
    client._objects[2] = client._objects[1].__class__("wl_registry")

    asyncio.run(
        client._handle_registry_event(
            2,
            0,
            _registry_payload(3, wlr_client_module.WLR_FOREIGN_TOPLEVEL_MANAGER_INTERFACE, 6),
        )
    )
    assert client._manager_id is not None

    client._handle_manager_event(client._manager_id, 0, struct.pack("<I", 50))
    client._handle_toplevel_event(50, 0, _encode_string("Editor"))
    client._handle_toplevel_event(50, 1, _encode_string("code"))
    client._handle_toplevel_event(50, 4, _encode_array([WLR_TOPLEVEL_STATE_ACTIVATED]))
    assert tracker.get_active_window() == ("code", "Editor")

    client._handle_toplevel_event(50, 6, b"")
    assert tracker.get_active_window() == ("", "")
    asyncio.run(client.stop())
    assert fake_socket.closed is True


def test_cosmic_wayland_client_links_ext_handles_to_cosmic_state() -> None:
    tracker = ExtForeignToplevelListTracker()
    client = cosmic_client_module.CosmicToplevelInfoWaylandClient(tracker)
    fake_socket = _FakeWaylandSocket()
    client._socket = fake_socket
    client._send_request = _fake_send_request_recorder(fake_socket)
    client._registry_id = 2
    client._objects[2] = client._objects[1].__class__("wl_registry")

    asyncio.run(
        client._handle_registry_event(
            2,
            0,
            _registry_payload(4, cosmic_client_module.EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE, 1),
        )
    )
    asyncio.run(
        client._handle_registry_event(
            2,
            0,
            _registry_payload(5, cosmic_client_module.COSMIC_TOPLEVEL_INFO_INTERFACE, 99),
        )
    )
    assert client._list_id is not None
    assert client._cosmic_info_id is not None

    asyncio.run(client._handle_list_event(client._list_id, 0, struct.pack("<I", 70)))
    cosmic_handle = client._ext_to_cosmic[70]
    client._handle_toplevel_event(70, 2, _encode_string("Settings"))
    client._handle_toplevel_event(70, 3, _encode_string("com.system76.Settings"))
    client._handle_cosmic_toplevel_event(cosmic_handle, 8, _encode_array([2]))
    assert tracker.get_active_window() == ("com.system76.Settings", "Settings")

    client._handle_cosmic_info_event(2)
    assert asyncio.run(tracker.next_active_window(timeout=0.01)) == (
        "com.system76.Settings",
        "Settings",
    )

    client._handle_display_event(1, struct.pack("<I", 70))
    assert 70 not in client._toplevel_handles
    assert cosmic_handle not in client._cosmic_handles
    asyncio.run(client.stop())
    assert fake_socket.closed is True


def test_wayland_clients_require_display_environment(monkeypatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    async def start_clients() -> None:
        ext = ExtForeignToplevelListWaylandClient(ExtForeignToplevelListTracker())
        wlr = wlr_client_module.WlrForeignToplevelWaylandClient(
            WlrForeignToplevelManagerTracker()
        )
        cosmic = cosmic_client_module.CosmicToplevelInfoWaylandClient(
            ExtForeignToplevelListTracker()
        )
        for client in (ext, wlr, cosmic):
            try:
                await client.start()
            except RuntimeError as exc:
                assert "WAYLAND_DISPLAY" in str(exc)
            else:
                raise AssertionError("client.start() unexpectedly succeeded")

    asyncio.run(start_clients())


def test_ext_wayland_client_start_and_run_against_minimal_socket() -> None:
    async def run_client() -> tuple[str, str]:
        with _short_socket_path("ext-wayland") as socket_path:

            async def handle_client(
                reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter,
            ) -> None:
                await reader.read(4096)
                writer.write(
                    _wl_message(
                        2,
                        0,
                        _registry_payload(1, EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE, 1),
                    )
                    + _wl_message(3, 0)
                )
                await writer.drain()

                await reader.read(4096)
                writer.write(_wl_message(5, 0))
                await writer.drain()

                await asyncio.sleep(0.01)
                writer.write(
                    _wl_message(4, 0, struct.pack("<I", 80))
                    + _wl_message(80, 2, _encode_string("Live Window"))
                    + _wl_message(80, 3, _encode_string("live.app"))
                    + _wl_message(4, 1)
                )
                await writer.drain()
                await asyncio.wait_for(reader.read(4096), timeout=1.0)
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(handle_client, path=str(socket_path))
            tracker = ExtForeignToplevelListTracker()
            client = ExtForeignToplevelListWaylandClient(tracker, socket_path=str(socket_path))
            try:
                await client.start()
                await client.run()
                tracker.update_state("80", [2])
                return tracker.get_active_window()
            finally:
                await client.stop()
                server.close()
                await server.wait_closed()

    assert asyncio.run(run_client()) == ("live.app", "Live Window")


def test_wlr_wayland_client_start_and_run_against_minimal_socket() -> None:
    async def run_client() -> tuple[str, str]:
        with _short_socket_path("wlr-wayland") as socket_path:

            async def handle_client(
                reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter,
            ) -> None:
                await reader.read(4096)
                writer.write(
                    _wl_message(
                        2,
                        0,
                        _registry_payload(
                            1,
                            wlr_client_module.WLR_FOREIGN_TOPLEVEL_MANAGER_INTERFACE,
                            3,
                        ),
                    )
                    + _wl_message(3, 0)
                )
                await writer.drain()

                await reader.read(4096)
                writer.write(_wl_message(5, 0))
                await writer.drain()

                await asyncio.sleep(0.01)
                writer.write(
                    _wl_message(4, 0, struct.pack("<I", 90))
                    + _wl_message(90, 0, _encode_string("WLR Window"))
                    + _wl_message(90, 1, _encode_string("wlr.app"))
                    + _wl_message(90, 4, _encode_array([WLR_TOPLEVEL_STATE_ACTIVATED]))
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(handle_client, path=str(socket_path))
            tracker = WlrForeignToplevelManagerTracker()
            client = wlr_client_module.WlrForeignToplevelWaylandClient(
                tracker,
                socket_path=str(socket_path),
            )
            try:
                await client.start()
                await client.run()
                return tracker.get_active_window()
            finally:
                await client.stop()
                server.close()
                await server.wait_closed()

    assert asyncio.run(run_client()) == ("wlr.app", "WLR Window")


def test_cosmic_wayland_client_start_and_run_against_minimal_socket() -> None:
    async def run_client() -> tuple[str, str]:
        with _short_socket_path("cosmic-wayland") as socket_path:

            async def handle_client(
                reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter,
            ) -> None:
                await reader.read(4096)
                writer.write(
                    _wl_message(
                        2,
                        0,
                        _registry_payload(
                            1,
                            cosmic_client_module.EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE,
                            1,
                        ),
                    )
                    + _wl_message(
                        2,
                        0,
                        _registry_payload(
                            2,
                            cosmic_client_module.COSMIC_TOPLEVEL_INFO_INTERFACE,
                            3,
                        ),
                    )
                    + _wl_message(3, 0)
                )
                await writer.drain()

                await reader.read(4096)
                writer.write(_wl_message(6, 0))
                await writer.drain()

                await asyncio.sleep(0.01)
                writer.write(
                    _wl_message(4, 0, struct.pack("<I", 100))
                    + _wl_message(5, 2)
                    + _wl_message(100, 2, _encode_string("COSMIC Window"))
                    + _wl_message(100, 3, _encode_string("cosmic.app"))
                    + _wl_message(7, 8, _encode_array([2]))
                    + _wl_message(4, 1)
                )
                await writer.drain()
                await asyncio.wait_for(reader.read(4096), timeout=1.0)
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(handle_client, path=str(socket_path))
            tracker = ExtForeignToplevelListTracker()
            client = cosmic_client_module.CosmicToplevelInfoWaylandClient(
                tracker,
                socket_path=str(socket_path),
            )
            try:
                await client.start()
                await client.run()
                return tracker.get_active_window()
            finally:
                await client.stop()
                server.close()
                await server.wait_closed()

    assert asyncio.run(run_client()) == ("cosmic.app", "COSMIC Window")
