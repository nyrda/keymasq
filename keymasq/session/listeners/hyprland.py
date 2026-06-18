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


class HyprlandListener(WindowListener):
    def __init__(
        self,
        callback: WindowChangeCallback,
        client: object | None = None,
        dbus: SessionDBus | None = None,
    ) -> None:
        super().__init__(callback, client, dbus=dbus)
        self.socket_path: str | None = None
        self.cmd_socket_path: str | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._cmd_lock = asyncio.Lock()

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
    def supports_tags(self) -> bool:
        return True

    @property
    def supports_compositor_dispatch(self) -> bool:
        return True

    async def start(self) -> None:
        event_socket, cmd_socket = await self.__class__._resolve_socket_paths()
        if not event_socket or not cmd_socket:
            raise RuntimeError("Hyprland socket not available")

        self.socket_path = event_socket
        self.cmd_socket_path = cmd_socket

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
        try:
            self.reader, self.writer = await asyncio.open_unix_connection(self.socket_path)

            buffer = ""
            while self.running:
                data = await self.reader.read(4096)
                if not data:
                    break

                buffer += data.decode("utf-8")

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if line.strip():
                        await self._handle_event(line.strip())

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

            tags = await self._get_window_tags()
            log.debug(
                f"Active window changed: class={window_class}, title={window_title}, tags={tags}"
            )
            await self.callback(window_class, window_title, tags)

        elif event_type == "activewindowv2":
            log.debug(f"Active window address: {data}")

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
        dispatcher_name = " ".join(str(dispatcher or "").strip().split())
        dispatcher_args = " ".join(str(args or "").strip().splitlines())
        if not dispatcher_name:
            return False, "missing dispatcher"
        if dispatcher_name == "set_cursor_position":
            parts = dispatcher_args.split()
            if len(parts) != 2:
                return False, "set_cursor_position expects X Y"
            try:
                x = int(float(parts[0]))
                y = int(float(parts[1]))
            except ValueError:
                return False, "set_cursor_position expects numeric X Y"
            return await self.set_cursor_position(x, y)

        command = f"dispatch {dispatcher_name}"
        command = f"{command} {dispatcher_args}" if dispatcher_args else f"{command} _"
        response = await self._send_cmd(command, read_size=4096)
        if response is None:
            return False, "no response from Hyprland"

        text = response.decode("utf-8", errors="replace").strip()
        if not text:
            return False, "empty response from Hyprland"
        if text.lower() == "ok":
            return True, text
        return False, text

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
                    cmd_reader.read(read_size),
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
