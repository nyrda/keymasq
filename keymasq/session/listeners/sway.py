"""Sway listener over the i3 IPC protocol.

Sway implements i3's IPC protocol, so the framing and tree helpers keep the
i3 names from that protocol.
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
from keymasq.session.listeners.base import WindowChangeCallback, WindowListener
from keymasq.session.listeners.layer_shell import LayerShellCursorSupport

log = logging.getLogger("keymasq-session.listeners.sway")

I3_IPC_MAGIC = b"i3-ipc"
_I3_IPC_HEADER = struct.Struct("=6sII")
I3_IPC_MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
I3_IPC_REQUEST_TIMEOUT_SECONDS = 1.5
I3_IPC_PROBE_TIMEOUT_SECONDS = 0.5

I3_IPC_EVENT_FLAG = 0x80000000


class I3IpcMessage(IntEnum):
    RUN_COMMAND = 0
    GET_WORKSPACES = 1
    SUBSCRIBE = 2
    GET_TREE = 4
    GET_VERSION = 7


class I3IpcEvent(IntEnum):
    WORKSPACE = I3_IPC_EVENT_FLAG | 0
    WINDOW = I3_IPC_EVENT_FLAG | 3
    SHUTDOWN = I3_IPC_EVENT_FLAG | 6


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


def i3_node_is_window(node: JsonObject) -> bool:
    if node.get("type") not in {"con", "floating_con"}:
        return False
    if node.get("window") is not None or node.get("app_id") is not None:
        return True
    return json_object(node.get("window_properties")) is not None


def _focus_descendant(node: JsonObject) -> JsonObject:
    """Follow a container's focus stack down to the window it would focus."""
    current = node
    for _ in range(256):
        if i3_node_is_window(current):
            return current
        focus_order = current.get("focus")
        if not isinstance(focus_order, list) or not focus_order:
            return current
        children = {coerce_int(child.get("id"), None): child for child in _node_children(current)}
        next_node = children.get(coerce_int(cast(list[object], focus_order)[0], None))
        if next_node is None:
            return current
        current = next_node
    return current


def find_i3_focused_window(tree: JsonObject) -> JsonObject | None:
    for node in iter_i3_nodes(tree):
        if node.get("focused") is not True:
            continue
        if node.get("type") in {"root", "output", "workspace"}:
            return None
        window = _focus_descendant(node)
        return window if i3_node_is_window(window) else None
    return None


def find_i3_window_by_title(tree: JsonObject, title: str) -> JsonObject | None:
    for node in iter_i3_nodes(tree):
        if i3_node_is_window(node) and node.get("name") == title:
            return node
    return None


def i3_window_info(node: JsonObject | None) -> tuple[str, str]:
    if node is None:
        return "", ""
    title = node.get("name")
    window_title = title if isinstance(title, str) else ""
    app_id = node.get("app_id")
    if isinstance(app_id, str) and app_id:
        return app_id, window_title
    properties = json_object(node.get("window_properties"))
    if properties is not None:
        for key in ("class", "instance"):
            value = properties.get(key)
            if isinstance(value, str) and value:
                return value, window_title
    return "", window_title


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


class SwayListener(WindowListener):
    def __init__(
        self,
        callback: WindowChangeCallback,
        client: object | None = None,
        dbus: SessionDBus | None = None,
    ) -> None:
        super().__init__(callback, client, dbus=dbus)
        self.socket_path: str | None = None
        self._event_writer: asyncio.StreamWriter | None = None
        self._focused_window_id: int | None = None
        self._window_class = ""
        self._window_title = ""
        self._last_emitted: tuple[str, str] | None = None
        self._cursor = LayerShellCursorSupport(self.client, "Sway")

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
        _ = dbus
        return await cls.resolve_socket_path() is not None

    async def start(self) -> None:
        socket_path = await self.resolve_socket_path()
        if socket_path is None:
            raise RuntimeError("Sway socket not available")

        self.socket_path = str(socket_path)
        reader, writer = await asyncio.open_unix_connection(self.socket_path)
        self._event_writer = writer
        try:
            await self._subscribe(reader, writer)
        except BaseException:
            await self._close_event_writer()
            raise

        try:
            await self._cursor.start()
            self.running = True
            self._task = asyncio.create_task(
                self._listen(reader),
                name=f"keymasq-session:{self.name}-events",
            )
            await self._refresh_from_tree()
        except BaseException:
            # The session manager drops a listener whose start fails without
            # calling stop, so release the event task and connections here.
            await self.stop()
            raise
        log.info("Sway listener started on %s", self.socket_path)

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._close_event_writer()
        await self._cursor.stop()
        log.info("Sway listener stopped")

    async def health_check(self) -> bool:
        if not await super().health_check():
            return False
        return bool(self.socket_path and Path(self.socket_path).exists())

    async def _close_event_writer(self) -> None:
        writer = self._event_writer
        self._event_writer = None
        if writer is None:
            return
        writer.close()
        try:
            await writer.wait_closed()
        except OSError as exc:
            log.debug("Failed while closing Sway event connection: %s", exc)

    async def _subscribe(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        writer.write(
            pack_i3_message(
                I3IpcMessage.SUBSCRIBE,
                json.dumps(["window", "workspace", "shutdown"]),
            )
        )
        await writer.drain()
        reply_type, reply = await asyncio.wait_for(
            read_i3_message(reader),
            timeout=I3_IPC_REQUEST_TIMEOUT_SECONDS,
        )
        response = json_object(decode_i3_payload(reply))
        if reply_type != I3IpcMessage.SUBSCRIBE or response is None:
            raise RuntimeError("Sway did not acknowledge event subscription")
        if response.get("success") is not True:
            raise RuntimeError("Sway rejected event subscription")

    async def _listen(self, reader: asyncio.StreamReader) -> None:
        try:
            while self.running:
                message_type, payload = await read_i3_message(reader)
                if message_type == I3IpcEvent.SHUTDOWN:
                    log.info("Sway reported shutdown")
                    break
                event = json_object(decode_i3_payload(payload))
                if event is None:
                    continue
                if message_type == I3IpcEvent.WINDOW:
                    await self._handle_window_event(event)
                elif message_type == I3IpcEvent.WORKSPACE:
                    await self._handle_workspace_event(event)
        except asyncio.CancelledError:
            pass
        except asyncio.IncompleteReadError:
            log.debug("Sway event connection closed")
        except (OSError, I3IpcError) as exc:
            log.debug("Sway listener stopped after error: %s", exc)
        except Exception:
            log.exception("Unexpected Sway listener error")

    async def _handle_window_event(self, event: JsonObject) -> None:
        change = event.get("change")
        container = json_object(event.get("container"))
        if container is None:
            return
        container_id = coerce_int(container.get("id"), None)

        if change == "focus":
            self._set_focused_window(container)
            await self._emit_if_changed()
            return

        if change == "title":
            if container.get("focused") is True or container_id == self._focused_window_id:
                self._set_focused_window(container)
                await self._emit_if_changed()
            return

        if change == "close" and container_id == self._focused_window_id:
            await self._refresh_from_tree()

    async def _handle_workspace_event(self, event: JsonObject) -> None:
        if event.get("change") == "focus":
            await self._refresh_from_tree()

    def _set_focused_window(self, node: JsonObject | None) -> None:
        if node is None or not i3_node_is_window(node):
            self._focused_window_id = None
            self._window_class = ""
            self._window_title = ""
            return
        self._focused_window_id = coerce_int(node.get("id"), None)
        self._window_class, self._window_title = i3_window_info(node)

    async def _emit_if_changed(self) -> None:
        window = (self._window_class, self._window_title)
        if window == self._last_emitted:
            return
        self._last_emitted = window
        log.debug("Active window changed: class=%s title=%s", *window)
        await self.callback(self._window_class, self._window_title, [])

    async def _request(self, message_type: int, payload: str = "") -> object | None:
        if not self.socket_path:
            return None
        try:
            return await i3_ipc_request(self.socket_path, message_type, payload)
        except (OSError, TimeoutError, asyncio.IncompleteReadError, I3IpcError) as exc:
            log.debug("Sway request %s failed: %s", int(message_type), exc)
            return None

    async def _get_tree(self) -> JsonObject | None:
        return json_object(await self._request(I3IpcMessage.GET_TREE))

    async def _refresh_from_tree(self) -> None:
        tree = await self._get_tree()
        if tree is None:
            return
        self._set_focused_window(find_i3_focused_window(tree))
        await self._emit_if_changed()

    async def get_active_window(self) -> tuple[str, str, list[str]]:
        if self.running and self._last_emitted is None:
            await self._refresh_from_tree()
        return self._window_class, self._window_title, []

    async def activate_window_by_title(self, title: str) -> JsonObject | None:
        expected_title = str(title or "").strip()
        if not expected_title:
            return {"found": False, "message": "title parameter required"}
        tree = await self._get_tree()
        if tree is None:
            return {"found": False, "message": "Sway tree unavailable"}
        window = find_i3_window_by_title(tree, expected_title)
        window_id = coerce_int(window.get("id"), None) if window is not None else None
        if window_id is None:
            return {"found": False}
        ok, message = await self._run_command(f"[con_id={window_id}] focus")
        if not ok:
            return {"found": False, "message": message}
        return {"found": True, "id": window_id, "title": expected_title}

    async def _run_command(self, command: str) -> tuple[bool, str]:
        response = await self._request(I3IpcMessage.RUN_COMMAND, command)
        if response is None:
            return False, "no response from Sway"
        return parse_i3_command_results(response)

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

    @property
    def supports_realtime_cursor_position(self) -> bool:
        return self._cursor.supports_cursor_tracking

    async def get_cursor_position(self) -> tuple[int, int] | None:
        return await self._cursor.get_cursor_position()

    async def prepare_cursor_position_tracking(self, duration_ms: int) -> None:
        await self._cursor.prepare_cursor_position_tracking(duration_ms)

    async def stop_cursor_position_tracking(self) -> None:
        await self._cursor.stop_cursor_position_tracking()

    async def set_cursor_position(self, x: int, y: int) -> tuple[bool, str]:
        # "-" is Sway's alias for the seat that handles the IPC command.
        return await self._run_command(f"seat - cursor set {int(x)} {int(y)}")
