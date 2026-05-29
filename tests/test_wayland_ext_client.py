import asyncio

from keymasq.session.wayland_protocols import ExtForeignToplevelListTracker
from keymasq.session.wayland_protocols.ext_foreign_toplevel_list_client import (
    EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE,
    ExtForeignToplevelListWaylandClient,
    _encode_string,
    _pack_uint,
)


def test_ext_wayland_client_registry_bind() -> None:
    tracker = ExtForeignToplevelListTracker()
    client = ExtForeignToplevelListWaylandClient(tracker, socket_path="/tmp/nonexistent")

    async def ignore_send(_obj: int, _opcode: int, _payload: bytes) -> None:
        pass

    client._send_request = ignore_send  # type: ignore[method-assign]

    registry_id = client._allocate_object_id("wl_registry")
    client._registry_id = registry_id

    registry_payload = (
        _pack_uint(3) + _encode_string(EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE) + _pack_uint(1)
    )
    asyncio.run(client._handle_registry_event(registry_id, 0, registry_payload))
    assert client._list_id is not None


def test_ext_wayland_client_updates_tracker_metadata() -> None:
    tracker = ExtForeignToplevelListTracker()
    client = ExtForeignToplevelListWaylandClient(tracker, socket_path="/tmp/nonexistent")

    list_id = client._allocate_object_id(EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE)
    client._list_id = list_id

    handle_object_id = client._allocate_object_id("ext_foreign_toplevel_handle_v1")
    client._handle_list_event(list_id, 0, _pack_uint(handle_object_id))
    client._handle_toplevel_event(handle_object_id, 3, _encode_string("Alacritty"))
    client._handle_toplevel_event(handle_object_id, 2, _encode_string("terminal"))

    tracker.update_state(str(handle_object_id), {"activated": True})
    assert tracker.get_active_window() == ("Alacritty", "terminal")


def test_ext_wayland_client_close_clears_active_window() -> None:
    tracker = ExtForeignToplevelListTracker()
    client = ExtForeignToplevelListWaylandClient(tracker, socket_path="/tmp/nonexistent")

    list_id = client._allocate_object_id(EXT_FOREIGN_TOPLEVEL_LIST_INTERFACE)
    client._list_id = list_id

    handle_object_id = client._allocate_object_id("ext_foreign_toplevel_handle_v1")
    client._handle_list_event(list_id, 0, _pack_uint(handle_object_id))
    client._handle_toplevel_event(handle_object_id, 3, _encode_string("firefox"))
    client._handle_toplevel_event(handle_object_id, 2, _encode_string("Mozilla"))
    tracker.update_state(str(handle_object_id), {"activated": True})
    assert tracker.get_active_window() == ("firefox", "Mozilla")

    client._handle_toplevel_event(handle_object_id, 0, b"")
    assert tracker.get_active_window() == ("", "")
