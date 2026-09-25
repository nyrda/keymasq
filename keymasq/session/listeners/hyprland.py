import asyncio
import json
import logging
import os
from pathlib import Path
from typing import cast

from keymasq.session.dbus import SessionDBus
from keymasq.session.listeners._socket_helpers import runtime_dir, unix_socket_connectable
from keymasq.session.listeners.base import WindowChangeCallback, WindowListener

log = logging.getLogger("keymasq-session.listeners.hyprland")
HYPRLAND_COMMAND_TIMEOUT_S = 1.0
HYPRLAND_LAYER_REPLY_MAX_BYTES = 64 * 1024
# How long startup on-demand layers wait for their queued openlayer events.
# Events queued while the listener reads its startup state arrive at once.
HYPRLAND_STARTUP_LAYER_WINDOW_S = 2.0

# zwlr_layer_surface_v1 keyboard_interactivity values.
LAYER_INTERACTIVITY_EXCLUSIVE = 1
LAYER_INTERACTIVITY_ON_DEMAND = 2


def _lua_string_literal(value: str) -> str:
    # Escape every byte that is not ASCII alphanumeric so a layer namespace
    # cannot break out of the string or add a "/" that hyprctl reads as flags.
    parts: list[str] = []
    for char in value:
        if char.isascii() and char.isalnum():
            parts.append(char)
        else:
            parts.extend(f"\\{byte:03d}" for byte in char.encode("utf-8"))
    return '"' + "".join(parts) + '"'


# Hyprland's IPC does not report layer keyboard interactivity or keyboard
# focus, so the listener asks its Lua config API. Replies start with this
# marker, which tells them apart from the error text Hyprland returns when the
# config is hyprlang and has no Lua API.
LAYER_QUERY_MARKER = "keymasq-layers"


def _layers_command(namespace: str | None = None) -> str:
    """List mapped layers, one per line.

    Each line holds the layer's address, keyboard interactivity, and namespace.
    Layers that take no keyboard focus are listed too, so every openlayer shows
    up as a new address.
    """
    query = f"{{ namespace = {_lua_string_literal(namespace)} }}" if namespace is not None else ""
    return (
        f'repl return (function() local lines = {{ "{LAYER_QUERY_MARKER}" }} '
        f"for _, layer in ipairs(hl.get_layers({query})) do "
        "if layer.mapped then "
        'lines[#lines + 1] = layer.address .. " " .. layer.interactivity .. " " .. layer.namespace '
        'end end return table.concat(lines, "\\n") end)()'
    )


def _parse_int_pair(args: str) -> tuple[int, int] | None:
    parts = args.split()
    if len(parts) != 2:
        return None
    try:
        return int(float(parts[0])), int(float(parts[1]))
    except ValueError:
        return None


def _movecursor_command(args: str) -> str | None:
    pair = _parse_int_pair(args)
    if pair is None:
        return None
    x, y = pair
    return f"dispatch hl.dsp.cursor.move({{ x = {x}, y = {y} }})"


def _strip_hyprland_dispatch_prefix(dispatcher: str) -> str:
    value = dispatcher.strip()
    lowered = value.lower()
    for prefix in ("hyprctl dispatch ", "dispatch "):
        if lowered.startswith(prefix):
            return value[len(prefix) :].strip()
    return value


def _build_hyprland_dispatch_command(dispatcher: str, args: str) -> tuple[bool, str, str | None]:
    raw_dispatcher = _strip_hyprland_dispatch_prefix(dispatcher)
    if not raw_dispatcher:
        return False, "missing dispatcher", None
    if args.strip():
        return False, "Hyprland 0.55 custom dispatch expects args to be empty", None
    if raw_dispatcher.startswith("hl.dsp."):
        return True, "", f"dispatch {raw_dispatcher}"
    return False, "Hyprland 0.55 custom dispatch expects an hl.dsp.* Lua expression", None


class HyprlandListener(WindowListener):
    def __init__(
        self,
        callback: WindowChangeCallback,
        client: object | None = None,
        dbus: SessionDBus | None = None,
    ) -> None:
        super().__init__(callback, client, dbus=dbus)
        self._last_window: tuple[str, str, tuple[str, ...], str] | None = None
        self.socket_path: str | None = None
        self.cmd_socket_path: str | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._cmd_lock = asyncio.Lock()
        # Layers that took keyboard focus when they opened, as
        # (address, namespace, keyboard interactivity) in opening order.
        self._focused_layers: list[tuple[str, str, int]] = []
        # Namespaces of every mapped layer already accounted for, by address,
        # including on-demand layers that gave focus to a window.
        self._known_layers: dict[str, str] = {}
        # On-demand layers from the startup snapshot. Each one either opened
        # before the event socket connected or has an openlayer event queued.
        self._startup_on_demand_layers: dict[str, tuple[str, str, int]] = {}
        self._startup_layers_expire_at = 0.0
        self._active_address = ""
        self._active_window_retitled = False
        # False once Hyprland has shown that its config has no Lua API.
        self._layer_queries_supported = True

    @property
    def name(self) -> str:
        return "hyprland"

    @classmethod
    def _runtime_dir(cls) -> Path:
        return runtime_dir()

    @classmethod
    async def _connectable(cls, path: Path, timeout_s: float = 0.2) -> bool:
        return await unix_socket_connectable(path, timeout_s=timeout_s)

    @classmethod
    async def _resolve_socket_paths(cls) -> tuple[str | None, str | None]:
        xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
        hyprland_instance = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        if xdg_runtime and hyprland_instance:
            base = Path(xdg_runtime) / "hypr" / hyprland_instance
            event_socket = base / ".socket2.sock"
            cmd_socket = base / ".socket.sock"
            if (
                event_socket.exists()
                and cmd_socket.exists()
                and await cls._connectable(event_socket)
            ):
                return str(event_socket), str(cmd_socket)

        hypr_root = cls._runtime_dir() / "hypr"
        if not hypr_root.exists():
            return None, None
        for instance_dir in sorted(hypr_root.iterdir()):
            if not instance_dir.is_dir():
                continue
            event_socket = instance_dir / ".socket2.sock"
            cmd_socket = instance_dir / ".socket.sock"
            if (
                event_socket.exists()
                and cmd_socket.exists()
                and await cls._connectable(event_socket)
            ):
                return str(event_socket), str(cmd_socket)
        return None, None

    @classmethod
    async def probe_available(cls, dbus: SessionDBus | None = None) -> bool:
        _ = dbus
        event_socket, _cmd_socket = await cls._resolve_socket_paths()
        return bool(event_socket)

    @property
    def supports_compositor_dispatch(self) -> bool:
        return True

    async def start(self) -> None:
        event_socket, cmd_socket = await self.__class__._resolve_socket_paths()
        if not event_socket or not cmd_socket:
            raise RuntimeError("Hyprland socket not available")

        self.socket_path = event_socket
        self.cmd_socket_path = cmd_socket
        # Subscribe before reading the current state, so events from while
        # the state is read wait in the socket instead of getting lost.
        self.reader, self.writer = await asyncio.open_unix_connection(self.socket_path)
        await self._seed_focus_state()

        self.running = True
        self._task = asyncio.create_task(self._listen())
        log.info("Hyprland listener started")

    async def stop(self) -> None:
        self.running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except OSError as exc:
                log.debug("Failed while closing Hyprland listener writer: %s", exc)
            except Exception:
                log.exception("Unexpected failure while closing Hyprland listener writer")

        log.info("Hyprland listener stopped")

    async def _listen(self) -> None:
        if self.reader is None:
            return
        try:
            buffer = ""
            while self.running:
                data = await self.reader.read(4096)
                if not data:
                    break

                buffer += data.decode("utf-8")

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    # Keep trailing spaces: they can belong to a layer namespace.
                    line = line.rstrip("\r")
                    if line.strip():
                        await self._handle_event(line)

        except asyncio.CancelledError:
            pass
        except UnicodeDecodeError as exc:
            log.debug("Hyprland listener received invalid UTF-8 event data: %s", exc)
        except OSError as exc:
            log.debug("Hyprland listener stopped after I/O error: %s", exc)
        except Exception:
            log.exception("Unexpected Hyprland listener error")

    async def _handle_event(self, event_line: str) -> None:
        if ">>" not in event_line:
            return

        event_type, data = event_line.split(">>", 1)

        if event_type == "activewindow":
            parts = data.split(",", 1)
            window_class = parts[0] if parts else ""
            window_title = parts[1] if len(parts) > 1 else ""
            retitled = self._active_window_retitled
            self._active_window_retitled = False

            if self._focused_layers:
                # Hyprland keeps windows from taking focus while an exclusive
                # layer is open, and repeats activewindow when the focused
                # window's title changes. Any other activewindow means a
                # window took focus from the on-demand layers.
                if not retitled and self._exclusive_layer_focused():
                    # A mapped layer can drop keyboard interactivity without
                    # closing, which hands focus back to a window.
                    await self._refresh_focused_layer_interactivity()
                if retitled or self._exclusive_layer_focused():
                    return
                log.debug("Window took focus from layers: %s", self._focused_layers)
                self._focused_layers.clear()

            await self._emit_window(window_class, window_title, await self._get_window_tags())

        elif event_type == "activewindowv2":
            self._active_address = data
            log.debug(f"Active window address: {data}")

        elif event_type == "windowtitlev2":
            address = data.split(",", 1)[0]
            # Hyprland follows this with an activewindow event for the
            # focused window, which is a title update rather than a focus change.
            self._active_window_retitled = bool(address) and address == self._active_address

        elif event_type == "openlayer":
            await self._handle_layer_opened(data)

        elif event_type == "closelayer":
            await self._handle_layer_closed(data)

    async def _emit_window(self, window_class: str, window_title: str, tags: list[str]) -> None:
        window = (window_class, window_title, tuple(tags), self.active_layer)
        if window == self._last_window:
            return

        self._last_window = window
        log.debug(
            f"Active window changed: class={window_class}, title={window_title}, tags={tags}, "
            f"layer={self.active_layer}"
        )
        await self.callback(window_class, window_title, tags)

    @property
    def active_layer(self) -> str:
        # The most recently opened layer holds keyboard focus.
        return self._focused_layers[-1][1] if self._focused_layers else ""

    def _exclusive_layer_focused(self) -> bool:
        return any(
            interactivity == LAYER_INTERACTIVITY_EXCLUSIVE
            for _address, _namespace, interactivity in self._focused_layers
        )

    @property
    def unavailable_capabilities(self) -> frozenset[str]:
        if self._layer_queries_supported:
            return frozenset()
        return frozenset({"layer_focus"})

    async def _handle_layer_opened(self, namespace: str) -> None:
        # Hyprland moves keyboard focus to a new layer that asks for it, but
        # sends no activewindow event and keeps reporting the previous window.
        # openlayer names only the namespace, so look for its layers that the
        # listener has not seen yet.
        layers = await self._get_layers_with_retry(namespace)
        if layers is None:
            return
        new_layers = [layer for layer in layers if layer[0] not in self._known_layers]
        self._known_layers.update((address, name) for address, name, _ in layers)
        if not new_layers and self._startup_layers_pending():
            # No new address means this is the queued event of an on-demand
            # layer that the startup snapshot already listed.
            mapped = {layer[0] for layer in layers}
            pending = next(
                (
                    address
                    for address, layer in self._startup_on_demand_layers.items()
                    if layer[1] == namespace and address in mapped
                ),
                None,
            )
            if pending is not None:
                new_layers = [self._startup_on_demand_layers.pop(pending)]
        focus_layers = [layer for layer in new_layers if layer[2] != 0]
        if not focus_layers:
            return
        self._focused_layers.extend(focus_layers)
        log.debug("Layer %r took keyboard focus", namespace)
        await self._emit_window("", "", [])

    def _startup_layers_pending(self) -> bool:
        if not self._startup_on_demand_layers:
            return False
        if asyncio.get_running_loop().time() < self._startup_layers_expire_at:
            return True
        self._startup_on_demand_layers.clear()
        return False

    async def _refresh_focused_layer_interactivity(self) -> None:
        layers = await self._get_layers_with_retry()
        if layers is None:
            return
        interactivity = {address: value for address, _name, value in layers}
        self._focused_layers = [
            (address, name, interactivity[address])
            for address, name, _value in self._focused_layers
            if interactivity.get(address, 0) != 0
        ]

    async def _handle_layer_closed(self, namespace: str) -> None:
        layers = await self._get_layers_with_retry(namespace)
        # Without an answer, forget the namespace's layers. A closed exclusive
        # layer kept by mistake would hide every later window focus change.
        remaining = {address for address, _namespace, _interactivity in layers or []}
        self._known_layers = {
            address: name
            for address, name in self._known_layers.items()
            if name != namespace or address in remaining
        }
        self._startup_on_demand_layers = {
            address: layer
            for address, layer in self._startup_on_demand_layers.items()
            if layer[1] != namespace or address in remaining
        }
        focused_layers = [
            layer
            for layer in self._focused_layers
            if layer[1] != namespace or layer[0] in remaining
        ]
        if len(focused_layers) == len(self._focused_layers):
            return
        self._focused_layers = focused_layers
        if self._focused_layers:
            await self._emit_window("", "", [])
            return
        # Hyprland refocuses a window while unmapping the layer, before it
        # answers this query.
        await self._emit_window(*await self._query_active_window())

    async def _get_layers_with_retry(
        self,
        namespace: str | None = None,
    ) -> list[tuple[str, str, int]] | None:
        layers = await self._get_layers(namespace)
        if layers is None and self._layer_queries_supported:
            # Hyprland does not replay openlayer or closelayer, and the startup
            # state is read only once, so try a failed request once more.
            layers = await self._get_layers(namespace)
        return layers

    async def _query_layers(self, command: str) -> list[str] | None:
        if not self._layer_queries_supported:
            return None
        response = await self._send_cmd(command, read_size=HYPRLAND_LAYER_REPLY_MAX_BYTES)
        if response is None or len(response) >= HYPRLAND_LAYER_REPLY_MAX_BYTES:
            # A reply that fills the limit may be cut off.
            return None
        # Strip only the line end. A namespace can be empty or end in spaces.
        lines = response.decode("utf-8", errors="replace").rstrip("\r\n").split("\n")
        if lines[0] != LAYER_QUERY_MARKER:
            # Hyprland answers with an error when it runs a hyprlang config.
            log.info("Hyprland layer focus tracking needs a Lua config: %s", lines[0])
            self._layer_queries_supported = False
            return None
        return lines

    async def _get_layers(
        self,
        namespace: str | None = None,
    ) -> list[tuple[str, str, int]] | None:
        lines = await self._query_layers(_layers_command(namespace))
        if lines is None:
            return None
        layers: list[tuple[str, str, int]] = []
        for line in lines[1:]:
            parts = line.split(" ", 2)
            try:
                address, interactivity, layer_namespace = parts[0], int(parts[1]), parts[2]
            except (IndexError, ValueError):
                log.debug("Hyprland layer query returned %r", line)
                continue
            layers.append((address, layer_namespace, interactivity))
        return layers

    async def _seed_focus_state(self) -> None:
        # The event socket does not replay earlier events, so read the focused
        # window's address and any layer that already holds the keyboard.
        response = await self._send_cmd("j/activewindow", read_size=8192)
        if response is not None:
            self._active_address = self._parse_active_window_address(response)
        layers = await self._get_layers_with_retry()
        if layers is None:
            return
        # Only exclusive layers are known to hold focus. An on-demand layer
        # may have lost it to a window already, or may have opened after the
        # event socket connected, so wait for a queued openlayer event.
        self._focused_layers = [
            layer for layer in layers if layer[2] == LAYER_INTERACTIVITY_EXCLUSIVE
        ]
        self._startup_on_demand_layers = {
            layer[0]: layer for layer in layers if layer[2] == LAYER_INTERACTIVITY_ON_DEMAND
        }
        self._startup_layers_expire_at = (
            asyncio.get_running_loop().time() + HYPRLAND_STARTUP_LAYER_WINDOW_S
        )
        self._known_layers = {address: name for address, name, _ in layers}
        if self._focused_layers:
            log.debug("Layers already hold keyboard focus: %s", self._focused_layers)

    @staticmethod
    def _parse_active_window_address(response: bytes) -> str:
        try:
            payload = cast(object, json.loads(response.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ""
        if not isinstance(payload, dict):
            return ""
        address = cast(dict[str, object], payload).get("address")
        # JSON addresses carry a 0x prefix that event data leaves out.
        return str(address).removeprefix("0x") if isinstance(address, str) else ""

    @staticmethod
    def _parse_active_window_response(
        response: bytes,
        *,
        context: str,
    ) -> tuple[str, str, list[str]] | None:
        try:
            payload = json.loads(response.decode("utf-8"))
        except UnicodeDecodeError as exc:
            log.debug("Hyprland %s response was not UTF-8: %s", context, exc)
            return None
        except json.JSONDecodeError as exc:
            log.debug("Hyprland %s response was malformed JSON: %s", context, exc)
            return None

        if not isinstance(payload, dict):
            log.debug("Hyprland %s response was not a JSON object", context)
            return None

        data = cast(dict[str, object], payload)
        tags = data.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        tag_items = cast(list[object], tags)
        window_class = data.get("class", "")
        window_title = data.get("title", "")
        return (
            str(window_class) if window_class is not None else "",
            str(window_title) if window_title is not None else "",
            [str(tag) for tag in tag_items],
        )

    async def _get_window_tags(self) -> list[str]:
        response = await self._send_cmd("j/activewindow", read_size=8192)
        if response is None:
            return []

        parsed = self._parse_active_window_response(response, context="active window tags")
        return parsed[2] if parsed is not None else []

    async def get_active_window(self) -> tuple[str, str, list[str]]:
        if self._focused_layers:
            return "", "", []
        return await self._query_active_window()

    async def _query_active_window(self) -> tuple[str, str, list[str]]:
        response = await self._send_cmd("j/activewindow", read_size=8192)
        if response is None:
            return "", "", []

        parsed = self._parse_active_window_response(response, context="active window")
        return parsed if parsed is not None else ("", "", [])

    async def get_cursor_position(self) -> tuple[int, int] | None:
        try:
            response = await self._send_cmd("cursorpos", read_size=256)
            if not response:
                return None
            text = response.decode().strip()
            parts = [p.strip() for p in text.split(",")]
            if len(parts) != 2:
                return None
            return int(float(parts[0])), int(float(parts[1]))
        except (OSError, UnicodeDecodeError, ValueError):
            return None

    @property
    def supports_realtime_cursor_position(self) -> bool:
        return True

    async def set_cursor_position(self, x: int, y: int) -> tuple[bool, str]:
        return await self.dispatch("movecursor", f"{int(x)} {int(y)}")

    async def dispatch(self, dispatcher: str, args: str = "") -> tuple[bool, str]:
        dispatcher_name = str(dispatcher or "").strip()
        dispatcher_args = " ".join(str(args or "").strip().splitlines())
        if not dispatcher_name:
            return False, "missing dispatcher"
        if dispatcher_name.strip() == "set_cursor_position":
            parts = dispatcher_args.split()
            if len(parts) != 2:
                return False, "set_cursor_position expects X Y"
            try:
                x = int(float(parts[0]))
                y = int(float(parts[1]))
            except ValueError:
                return False, "set_cursor_position expects numeric X Y"
            return await self.set_cursor_position(x, y)
        if dispatcher_name.strip() == "movecursor":
            command = _movecursor_command(dispatcher_args)
            if command is None:
                return False, "movecursor expects X Y"
        else:
            ok, message, command = _build_hyprland_dispatch_command(
                dispatcher_name,
                dispatcher_args,
            )
            if not ok or command is None:
                return False, message
        response = await self._send_cmd(command, read_size=4096)
        if response is None:
            return False, "no response from Hyprland"

        text = response.decode("utf-8", errors="replace").strip()
        if not text:
            return False, "empty response from Hyprland"
        if text.lower() == "ok":
            return True, text
        return False, text

    @staticmethod
    async def _read_reply(reader: asyncio.StreamReader, limit: int) -> bytes:
        # Hyprland closes the command socket after the reply, which can
        # arrive in several reads.
        response = b""
        while len(response) < limit:
            chunk = await reader.read(limit - len(response))
            if not chunk:
                break
            response += chunk
        return response

    async def _send_cmd(self, command: str, read_size: int = 8192) -> bytes | None:
        async with self._cmd_lock:
            if not self.cmd_socket_path:
                return None

            cmd_writer: asyncio.StreamWriter | None = None
            try:
                cmd_reader, cmd_writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(self.cmd_socket_path),
                    timeout=HYPRLAND_COMMAND_TIMEOUT_S,
                )
                cmd_writer.write(command.encode())
                await asyncio.wait_for(
                    cmd_writer.drain(),
                    timeout=HYPRLAND_COMMAND_TIMEOUT_S,
                )
                response = await asyncio.wait_for(
                    self._read_reply(cmd_reader, read_size),
                    timeout=HYPRLAND_COMMAND_TIMEOUT_S,
                )
                if not response:
                    return None
                return response
            except TimeoutError as exc:
                log.debug("Hyprland command timed out: %s", exc)
                return None
            except OSError as exc:
                log.debug("Hyprland command failed: %s", exc)
                return None
            except Exception:
                log.exception("Unexpected Hyprland command failure")
                return None
            finally:
                if cmd_writer is not None:
                    try:
                        cmd_writer.close()
                        await cmd_writer.wait_closed()
                    except OSError as exc:
                        log.debug("Failed while closing Hyprland command writer: %s", exc)
                    except Exception:
                        log.exception("Unexpected failure while closing Hyprland command writer")

    async def health_check(self) -> bool:
        if not await super().health_check():
            return False
        return await self.__class__.probe_available()
