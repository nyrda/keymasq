"""Sway listener.

Active-window tracking and cursor reads use the wlroots foreign-toplevel and
layer-shell protocols, like the generic wlroots listener. Those protocols also
report focus moving to a layer-shell surface such as a launcher, which Sway's
IPC does not. Compositor actions, window activation, and Set Cursor go through
Sway's i3-compatible IPC socket, so the framing helpers keep the i3 names.
"""

import asyncio
import json
import logging
import os
import struct
from collections.abc import Iterator
from enum import IntEnum
from pathlib import Path
from typing import cast

from keymasq.common.coercion import coerce_int, json_object
from keymasq.common.types import JsonObject
from keymasq.session.dbus import SessionDBus
from keymasq.session.listeners._socket_helpers import unix_socket_connectable
from keymasq.session.listeners.base import WindowChangeCallback
from keymasq.session.listeners.wayland_wlr import WlrootsWaylandListener

log = logging.getLogger("keymasq-session.listeners.sway")

I3_IPC_MAGIC = b"i3-ipc"
_I3_IPC_HEADER = struct.Struct("=6sII")
I3_IPC_MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
I3_IPC_REQUEST_TIMEOUT_SECONDS = 1.5
I3_IPC_PROBE_TIMEOUT_SECONDS = 0.5


class I3IpcMessage(IntEnum):
    RUN_COMMAND = 0
    GET_TREE = 4
    GET_VERSION = 7


class I3IpcError(RuntimeError):
    pass


def pack_i3_message(message_type: int, payload: str = "") -> bytes:
    body = payload.encode("utf-8")
    return _I3_IPC_HEADER.pack(I3_IPC_MAGIC, len(body), int(message_type)) + body


async def read_i3_message(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(_I3_IPC_HEADER.size)
    magic, length, message_type = cast(tuple[bytes, int, int], _I3_IPC_HEADER.unpack(header))
    if magic != I3_IPC_MAGIC:
        raise I3IpcError("invalid i3 IPC magic")
    if length > I3_IPC_MAX_PAYLOAD_BYTES:
        raise I3IpcError(f"i3 IPC payload too large: {length} bytes")
    payload = await reader.readexactly(length) if length else b""
    return message_type, payload


def decode_i3_payload(payload: bytes) -> object:
    try:
        return cast(object, json.loads(payload.decode("utf-8", errors="replace")))
    except json.JSONDecodeError as exc:
        raise I3IpcError(f"invalid i3 IPC JSON payload: {exc}") from exc


async def i3_ipc_request(
    socket_path: str | Path,
    message_type: int,
    payload: str = "",
    *,
    timeout_s: float = I3_IPC_REQUEST_TIMEOUT_SECONDS,
) -> object:
    """Send one request on a fresh connection and return the decoded reply."""

    async def _request() -> object:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        try:
            writer.write(pack_i3_message(message_type, payload))
            await writer.drain()
            reply_type, reply = await read_i3_message(reader)
            if reply_type != int(message_type):
                raise I3IpcError(
                    f"unexpected i3 IPC reply type {reply_type} for request {int(message_type)}"
                )
            return decode_i3_payload(reply)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError as exc:
                log.debug("Failed while closing i3 IPC request connection: %s", exc)

    return await asyncio.wait_for(_request(), timeout=timeout_s)


def parse_i3_command_results(response: object) -> tuple[bool, str]:
    if not isinstance(response, list):
        return False, "invalid command reply"
    results = [
        item for raw in cast(list[object], response) if (item := json_object(raw)) is not None
    ]
    if not results:
        return False, "empty command reply"
    for result in results:
        if result.get("success") is not True:
            error = result.get("error")
            return False, str(error) if error else "command failed"
    return True, "ok"


def _node_children(node: JsonObject) -> Iterator[JsonObject]:
    for key in ("nodes", "floating_nodes"):
        children = node.get(key)
        if not isinstance(children, list):
            continue
        for raw in cast(list[object], children):
            child = json_object(raw)
            if child is not None:
                yield child


def iter_i3_nodes(tree: JsonObject) -> Iterator[JsonObject]:
    stack = [tree]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(list(_node_children(node))))


def sway_node_is_window(node: JsonObject) -> bool:
    if node.get("type") not in {"con", "floating_con"}:
        return False
    # Sway adds "shell" to every view. Native views may have a null app_id.
    return isinstance(node.get("shell"), str) or node.get("window") is not None


def find_sway_window_by_title(tree: JsonObject, title: str) -> JsonObject | None:
    for node in iter_i3_nodes(tree):
        if sway_node_is_window(node) and node.get("name") == title:
            return node
    return None


def sway_layout_origin(tree: JsonObject) -> tuple[int, int]:
    rect = json_object(tree.get("rect"))
    if rect is None:
        return 0, 0
    return coerce_int(rect.get("x"), 0), coerce_int(rect.get("y"), 0)


def _parse_int_pair(args: str) -> tuple[int, int] | None:
    parts = args.split()
    if len(parts) != 2:
        return None
    try:
        return int(float(parts[0])), int(float(parts[1]))
    except (OverflowError, ValueError):
        return None


def _sway_socket_candidates() -> list[Path]:
    # Only trust the session environment. Scanning the runtime directory could
    # pick another Sway session owned by the same user.
    candidates: list[Path] = []
    for variable in ("SWAYSOCK", "I3SOCK"):
        value = os.environ.get(variable, "").strip()
        if value:
            candidates.append(Path(value))
    return candidates


def _accepts_sway_version(version: JsonObject) -> bool:
    # Sway and its forks report a "variant"; i3 does not.
    variant = version.get("variant")
    return isinstance(variant, str) and bool(variant) and variant != "i3"


class SwayListener(WlrootsWaylandListener):
    _logger = log
    _listener_error_label = "Sway listener"
    _started_log_message = "Sway listener started"
    _stopped_log_message = "Sway listener stopped"

    def __init__(
        self,
        callback: WindowChangeCallback,
        client: object | None = None,
        dbus: SessionDBus | None = None,
    ) -> None:
        super().__init__(callback, client, dbus=dbus)
        self.ipc_socket_path: Path | None = None

    @property
    def name(self) -> str:
        return "sway"

    @property
    def supports_compositor_dispatch(self) -> bool:
        return True

    @classmethod
    async def resolve_socket_path(cls) -> Path | None:
        seen: set[Path] = set()
        for path in _sway_socket_candidates():
            if path in seen:
                continue
            seen.add(path)
            if not path.exists() or not await unix_socket_connectable(path):
                continue
            try:
                version = json_object(
                    await i3_ipc_request(
                        path,
                        I3IpcMessage.GET_VERSION,
                        timeout_s=I3_IPC_PROBE_TIMEOUT_SECONDS,
                    )
                )
            except (OSError, TimeoutError, asyncio.IncompleteReadError, I3IpcError) as exc:
                log.debug("Sway version probe failed for %s: %s", path, exc)
                continue
            if version is not None and _accepts_sway_version(version):
                return path
        return None

    @classmethod
    async def probe_available(cls, dbus: SessionDBus | None = None) -> bool:
        if await cls.resolve_socket_path() is None:
            return False
        return await super().probe_available(dbus)

    async def start(self) -> None:
        self.ipc_socket_path = await self.resolve_socket_path()
        if self.ipc_socket_path is None:
            raise RuntimeError("Sway socket not available")
        await super().start()
        log.info("Sway IPC socket: %s", self.ipc_socket_path)

    async def health_check(self) -> bool:
        if not await super().health_check():
            return False
        return bool(self.ipc_socket_path and self.ipc_socket_path.exists())

    async def _request(self, message_type: int, payload: str = "") -> object | None:
        if self.ipc_socket_path is None:
            return None
        try:
            return await i3_ipc_request(self.ipc_socket_path, message_type, payload)
        except (OSError, TimeoutError, asyncio.IncompleteReadError, I3IpcError) as exc:
            log.debug("Sway request %s failed: %s", int(message_type), exc)
            return None

    async def _get_tree(self) -> JsonObject | None:
        return json_object(await self._request(I3IpcMessage.GET_TREE))

    async def _run_command(self, command: str) -> tuple[bool, str]:
        response = await self._request(I3IpcMessage.RUN_COMMAND, command)
        if response is None:
            return False, "no response from Sway"
        return parse_i3_command_results(response)

    async def activate_window_by_title(self, title: str) -> JsonObject | None:
        expected_title = str(title or "").strip()
        if not expected_title:
            return {"found": False, "message": "title parameter required"}
        tree = await self._get_tree()
        if tree is None:
            return {"found": False, "message": "Sway tree unavailable"}
        window = find_sway_window_by_title(tree, expected_title)
        window_id = coerce_int(window.get("id"), None) if window is not None else None
        if window_id is None:
            return {"found": False}
        ok, message = await self._run_command(f"[con_id={window_id}] focus")
        if not ok:
            return {"found": False, "message": message}
        return {"found": True, "id": window_id, "title": expected_title}

    async def set_cursor_position(self, x: int, y: int) -> tuple[bool, str]:
        # "cursor set" takes coordinates relative to the layout's top-left
        # corner, while Keymasq uses global layout coordinates. They differ
        # when an output sits at a negative position.
        tree = await self._get_tree()
        if tree is None:
            return False, "Sway tree unavailable"
        origin_x, origin_y = sway_layout_origin(tree)
        # "-" is Sway's alias for the seat that handles the IPC command.
        return await self._run_command(
            f"seat - cursor set {int(x) - origin_x} {int(y) - origin_y}"
        )

    def _strip_command_prefix(self, dispatcher: str) -> str:
        value = dispatcher.strip()
        if value.lower().startswith("swaymsg "):
            return value[len("swaymsg ") :].strip()
        return value

    async def dispatch(self, dispatcher: str, args: str = "") -> tuple[bool, str]:
        dispatcher_name = self._strip_command_prefix(str(dispatcher or ""))
        dispatcher_args = str(args or "").strip()
        if not dispatcher_name:
            return False, "dispatcher parameter required"

        if dispatcher_name == "set_cursor_position":
            pair = _parse_int_pair(dispatcher_args)
            if pair is None:
                return False, "set_cursor_position expects X Y"
            return await self.set_cursor_position(*pair)

        command = f"{dispatcher_name} {dispatcher_args}" if dispatcher_args else dispatcher_name
        return await self._run_command(command)
