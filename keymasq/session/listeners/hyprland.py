import asyncio
import json
import logging
import os
from pathlib import Path
from typing import cast

from keymasq.session.dbus import SessionDBus
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
        env_dir = os.environ.get("XDG_RUNTIME_DIR")
        if env_dir:
            return Path(env_dir)
        return Path(f"/run/user/{os.getuid()}")

    @classmethod
    async def _connectable(cls, path: Path, timeout_s: float = 0.2) -> bool:
        try:
            connect_coro = asyncio.open_unix_connection(path=str(path))
            _reader, writer = await asyncio.wait_for(connect_coro, timeout=timeout_s)
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False

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

    @property
    def supports_native_cursor_position_set(self) -> bool:
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
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass

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
        except Exception as e:
            log.error(f"Hyprland listener error: {e}")

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

    async def _get_window_tags(self) -> list[str]:
        try:
            response = await self._send_cmd("j/activewindow", read_size=8192)
            if response is None:
                return []

            data = json.loads(response.decode())
            tags = data.get("tags", [])
            if not isinstance(tags, list):
                return []
            tag_items = cast(list[object], tags)
            return [str(tag) for tag in tag_items]

        except Exception as e:
            log.debug(f"Failed to get window tags: {e}")
            return []

    async def get_active_window(self) -> tuple[str, str, list[str]]:
        try:
            response = await self._send_cmd("j/activewindow", read_size=8192)
            if response is None:
                return "", "", []

            data = json.loads(response.decode())
            return (
                data.get("class", ""),
                data.get("title", ""),
                data.get("tags", []),
            )

        except Exception as e:
            log.error(f"Failed to get active window: {e}")
            return "", "", []

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
        except Exception:
            return None

    async def set_cursor_position(self, x: int, y: int) -> tuple[bool, str]:
        return await self.dispatch("movecursor", f"{int(x)} {int(y)}")

    async def dispatch(self, dispatcher: str, args: str = "") -> tuple[bool, str]:
        dispatcher_name = " ".join(str(dispatcher or "").strip().split())
        dispatcher_args = " ".join(str(args or "").strip().splitlines())
        if not dispatcher_name:
            return False, "missing dispatcher"

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
            except Exception as exc:
                log.debug("Hyprland command failed: %s", exc)
                return None
            finally:
                if cmd_writer is not None:
                    try:
                        cmd_writer.close()
                        await cmd_writer.wait_closed()
                    except Exception:
                        pass

    async def health_check(self) -> bool:
        if not await super().health_check():
            return False
        return await self.__class__.probe_available()
