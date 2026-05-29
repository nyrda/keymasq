import asyncio

from keymasq.session.wayland_protocols import WlrForeignToplevelManagerTracker
from keymasq.session.wayland_protocols.wlr_foreign_toplevel_client import (
    WlrForeignToplevelWaylandClient,
    _encode_string,
    _pack_uint,
)


def _encode_array(value: bytes) -> bytes:
    size = len(value)
    padded = (size + 3) & ~3
    return _pack_uint(size) + value + (b"\x00" * (padded - size))


def test_wlr_wayland_client_tracks_active_toplevel() -> None:
    tracker = WlrForeignToplevelManagerTracker()
    client = WlrForeignToplevelWaylandClient(tracker, socket_path="/tmp/nonexistent")

    async def ignore_send(_obj: int, _opcode: int, _payload: bytes) -> None:
        pass

    client._send_request = ignore_send  # type: ignore[method-assign]

    registry_id = client._allocate_object_id("wl_registry")
    client._registry_id = registry_id

    registry_payload = (
        _pack_uint(9) + _encode_string("zwlr_foreign_toplevel_manager_v1") + _pack_uint(3)
    )
    asyncio.run(client._handle_registry_event(registry_id, 0, registry_payload))

    manager_id = client._manager_id
    assert manager_id is not None

    handle_object_id = client._allocate_object_id("zwlr_foreign_toplevel_handle_v1")
    client._handle_manager_event(manager_id, 0, _pack_uint(handle_object_id))
    client._handle_toplevel_event(handle_object_id, 1, _encode_string("firefox"))
    client._handle_toplevel_event(handle_object_id, 0, _encode_string("Mozilla Firefox"))
    client._handle_toplevel_event(handle_object_id, 4, _encode_array((2).to_bytes(4, "little")))

    assert tracker.get_active_window() == ("firefox", "Mozilla Firefox")


def test_wlr_wayland_client_close_clears_active_window() -> None:
    tracker = WlrForeignToplevelManagerTracker()
    client = WlrForeignToplevelWaylandClient(tracker, socket_path="/tmp/nonexistent")

    handle_object_id = client._allocate_object_id("zwlr_foreign_toplevel_handle_v1")
    tracker.add_toplevel(str(handle_object_id))
    tracker.update_app_id(str(handle_object_id), "Alacritty")
    tracker.update_title(str(handle_object_id), "term")
    tracker.update_state(str(handle_object_id), (2).to_bytes(4, "little"))
    assert tracker.get_active_window() == ("Alacritty", "term")

    client._handle_toplevel_event(handle_object_id, 6, b"")
    assert tracker.get_active_window() == ("", "")
