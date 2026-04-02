import ast
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path

from keyforge.common.paths import GNOME_BRIDGE_SOCKET_PATH
from keyforge.common.security import get_peer_credentials
from keyforge.session.dbus import SessionDBus, name_has_owner
from keyforge.session.listeners.base import WindowChangeCallback, WindowListener
from keyforge.session.wayland_protocols.registry_probe import list_registry_globals

log = logging.getLogger("keyforge-session.listeners.gnome")


class GnomeListener(WindowListener):
    _EXTENSION_UUID = "keyforge-bridge@keyforge"

    def __init__(
        self,
        callback: WindowChangeCallback,
        client=None,
        dbus: SessionDBus | None = None,
    ) -> None:
        super().__init__(callback, client, dbus=dbus)
        self._server: asyncio.AbstractServer | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._request_id = 0
        self._pending_pointer: dict[int, asyncio.Future] = {}
        self._pending_activate: dict[int, asyncio.Future] = {}
        self._pending_window: dict[int, asyncio.Future] = {}
        self._last_class = ""
        self._last_title = ""
        self._last_warn_no_bridge = 0.0
        self._bridge_connected = False

    @property
    def name(self) -> str:
        return "gnome"

    @classmethod
    def _has_runtime_prereqs(cls) -> bool:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        if not os.path.exists(os.path.join(runtime_dir, "bus")):
            return False
        return True

    @classmethod
    def _desktop_indicates_gnome(cls) -> bool:
        current = (os.environ.get("XDG_CURRENT_DESKTOP") or "").lower()
        session = (os.environ.get("XDG_SESSION_DESKTOP") or "").lower()
        desktop = (os.environ.get("DESKTOP_SESSION") or "").lower()
        values = (current, session, desktop)
        return any("gnome" in value for value in values)

    @classmethod
    def _extension_dirs(cls) -> list[Path]:
        dirs: list[Path] = [
            Path.home() / ".local/share/gnome-shell/extensions" / cls._EXTENSION_UUID,
            Path("/usr/share/gnome-shell/extensions") / cls._EXTENSION_UUID,
            Path("/run/current-system/sw/share/gnome-shell/extensions") / cls._EXTENSION_UUID,
        ]
        xdg_data_dirs = os.environ.get("XDG_DATA_DIRS", "")
        for entry in xdg_data_dirs.split(":"):
            if not entry:
                continue
            dirs.append(Path(entry) / "gnome-shell/extensions" / cls._EXTENSION_UUID)
        unique_dirs: list[Path] = []
        seen: set[Path] = set()
        for ext_dir in dirs:
            if ext_dir in seen:
                continue
            seen.add(ext_dir)
            unique_dirs.append(ext_dir)
        return unique_dirs

    @classmethod
    def _bridge_extension_available(cls) -> bool:
        for ext_dir in cls._extension_dirs():
            if (ext_dir / "metadata.json").exists() and (ext_dir / "extension.js").exists():
                return True
        return False

    @classmethod
    async def _gsettings_get(cls, schema: str, key: str) -> str | None:
        try:
            process = await asyncio.create_subprocess_exec(
                "gsettings",
                "get",
                schema,
                key,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception:
            return None

        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=0.8)
        except Exception:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.communicate()
            return None

        if process.returncode != 0:
            return None
        return stdout.decode("utf-8", errors="replace").strip()

    @classmethod
    def _parse_enabled_extensions(cls, raw_value: str | None) -> list[str] | None:
        if not raw_value:
            return None
        value = raw_value.strip()
        if value.startswith("@as "):
            value = value[4:].strip()
        try:
            parsed = ast.literal_eval(value)
        except Exception:
            return None
        if not isinstance(parsed, list):
            return None
        return [str(item) for item in parsed]

    @classmethod
    async def _user_extensions_globally_disabled(cls) -> bool | None:
        raw_value = await cls._gsettings_get("org.gnome.shell", "disable-user-extensions")
        if raw_value is None:
            return None
        lowered = raw_value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        return None

    @classmethod
    async def _bridge_extension_enabled(cls) -> bool | None:
        enabled_raw = await cls._gsettings_get("org.gnome.shell", "enabled-extensions")
        enabled_extensions = cls._parse_enabled_extensions(enabled_raw)
        if enabled_extensions is None:
            return None
        return cls._EXTENSION_UUID in enabled_extensions

    @classmethod
    def _candidate_wayland_sockets(cls) -> list[Path]:
        runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
        if not runtime_dir.exists():
            return []
        sockets: list[Path] = []
        for path in runtime_dir.glob("wayland-*"):
            if path.is_socket():
                sockets.append(path)
        sockets.sort(key=lambda p: p.name)
        return sockets

    @classmethod
    async def _probe_missing_native_toplevel_protocols(cls) -> bool:
        forbidden = {
            "zwlr_foreign_toplevel_manager_v1",
            "ext_foreign_toplevel_list_v1",
            "zcosmic_toplevel_info_v1",
        }
        for socket_path in cls._candidate_wayland_sockets():
            globals_found = await list_registry_globals(socket_path)
            if "xdg_wm_base" not in globals_found:
                continue
            if globals_found & forbidden:
                continue
            return True
        return False

    @classmethod
    async def probe_session(cls, dbus: SessionDBus | None = None) -> bool:
        if not cls._has_runtime_prereqs():
            return False
        if not await cls._probe_missing_native_toplevel_protocols():
            return False
        if cls._desktop_indicates_gnome():
            return True
        if await cls._probe_shell_owner(dbus):
            return True
        return await cls._probe_shell_process()

    @classmethod
    def _probe_shell_process_sync(cls) -> bool:
        uid = os.getuid()
        proc_dir = "/proc"
        try:
            entries = os.listdir(proc_dir)
        except Exception:
            return False

        for entry in entries:
            if not entry.isdigit():
                continue
            base = os.path.join(proc_dir, entry)
            try:
                if os.stat(base).st_uid != uid:
                    continue
            except Exception:
                continue

            comm_path = os.path.join(base, "comm")
            cmdline_path = os.path.join(base, "cmdline")
            try:
                with open(comm_path, encoding="utf-8", errors="replace") as handle:
                    comm = handle.read().strip().lower()
                if comm == "gnome-shell":
                    return True
            except Exception:
                pass

            try:
                with open(cmdline_path, "rb") as handle:
                    raw = handle.read().replace(b"\x00", b" ").decode("utf-8", errors="replace")
                if "gnome-shell" in raw.lower():
                    return True
            except Exception:
                pass
        return False

    @classmethod
    async def _probe_shell_process(cls) -> bool:
        return await asyncio.to_thread(cls._probe_shell_process_sync)

    @classmethod
    async def probe_available(cls, dbus: SessionDBus | None = None) -> bool:
        details = await cls.get_support_details(dbus)
        return bool(details["supported"])

    @classmethod
    async def get_support_details(cls, dbus: SessionDBus | None = None) -> dict[str, bool | str]:
        if not await cls.probe_session(dbus):
            return {
                "session_detected": False,
                "extension_installed": False,
                "extension_enabled": False,
                "extensions_globally_disabled": False,
                "supported": False,
                "warning": "",
            }

        installed = cls._bridge_extension_available()
        if not installed:
            return {
                "session_detected": True,
                "extension_installed": False,
                "extension_enabled": False,
                "extensions_globally_disabled": False,
                "supported": False,
                "warning": (
                    "GNOME Shell detected, but the Keyforge GNOME bridge extension is not "
                    "installed. Install 'keyforge-bridge@keyforge' so Keyforge can read the "
                    "focused window and cursor position needed for GNOME Wayland window rules."
                ),
            }

        extensions_disabled = await cls._user_extensions_globally_disabled()
        if extensions_disabled is True:
            return {
                "session_detected": True,
                "extension_installed": True,
                "extension_enabled": False,
                "extensions_globally_disabled": True,
                "supported": False,
                "warning": (
                    "GNOME Shell detected, but GNOME extensions are globally disabled for this "
                    "session. Re-enable shell extensions so the Keyforge GNOME bridge can report "
                    "focused windows and cursor position on GNOME Wayland."
                ),
            }

        enabled = await cls._bridge_extension_enabled()
        if enabled is False:
            return {
                "session_detected": True,
                "extension_installed": True,
                "extension_enabled": False,
                "extensions_globally_disabled": False,
                "supported": False,
                "warning": (
                    "GNOME Shell detected, but the Keyforge GNOME bridge extension is not "
                    "enabled. Enable 'keyforge-bridge@keyforge', then log out and log back in so "
                    "GNOME Shell loads it. Keyforge needs that bridge to receive focused window "
                    "and cursor updates required for GNOME Wayland window rules."
                ),
            }

        return {
            "session_detected": True,
            "extension_installed": True,
            "extension_enabled": True,
            "extensions_globally_disabled": False,
            "supported": True,
            "warning": "",
        }

    @classmethod
    async def _probe_shell_owner(cls, dbus: SessionDBus | None = None) -> bool:
        return await name_has_owner("org.gnome.Shell", dbus)

    async def start(self) -> None:
        details = await self.__class__.get_support_details(self.dbus)
        if not bool(details["session_detected"]):
            raise RuntimeError("GNOME Shell is not available")
        if not bool(details["supported"]):
            raise RuntimeError(
                str(details["warning"] or "GNOME Shell bridge support is unavailable")
            )

        GNOME_BRIDGE_SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(GNOME_BRIDGE_SOCKET_PATH.parent, 0o700)
        except OSError:
            pass

        if GNOME_BRIDGE_SOCKET_PATH.exists():
            try:
                GNOME_BRIDGE_SOCKET_PATH.unlink()
            except OSError:
                pass

        self._server = await asyncio.start_unix_server(
            self._handle_bridge_client,
            path=str(GNOME_BRIDGE_SOCKET_PATH),
        )
        try:
            os.chmod(GNOME_BRIDGE_SOCKET_PATH, 0o600)
        except OSError:
            pass

        self.running = True
        log.info("GNOME bridge listener started on %s", GNOME_BRIDGE_SOCKET_PATH)
        log.warning(
            "GNOME session detected but bridge extension is not connected yet; waiting on %s",
            GNOME_BRIDGE_SOCKET_PATH,
        )

    async def stop(self) -> None:
        self.running = False

        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
            self._reader_task = None

        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        for future in list(self._pending_pointer.values()):
            if not future.done():
                future.set_result(None)
        self._pending_pointer.clear()

        for future in list(self._pending_activate.values()):
            if not future.done():
                future.set_result(None)
        self._pending_activate.clear()

        for future in list(self._pending_window.values()):
            if not future.done():
                future.set_result(None)
        self._pending_window.clear()

        if GNOME_BRIDGE_SOCKET_PATH.exists():
            try:
                GNOME_BRIDGE_SOCKET_PATH.unlink()
            except OSError:
                pass

        log.info("GNOME bridge listener stopped")

    async def _handle_bridge_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = get_peer_credentials(writer.get_extra_info("socket"))
        if peer is None or int(peer.uid) != os.getuid():
            writer.close()
            await writer.wait_closed()
            return

        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass

        self._writer = writer
        if not self._bridge_connected:
            self._bridge_connected = True
            log.info("GNOME bridge connected")
        self._reader_task = asyncio.create_task(self._bridge_read_loop(reader, writer))
        await self._reader_task

    async def _bridge_read_loop(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while self.running:
                raw = await reader.readline()
                if not raw:
                    break
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except Exception:
                    continue
                await self._handle_bridge_message(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            if self._writer is writer:
                self._writer = None
            if self._bridge_connected:
                self._bridge_connected = False
                if self.running:
                    log.warning("GNOME bridge disconnected; waiting for extension reconnect")
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_bridge_message(self, payload: dict) -> None:
        msg_type = str(payload.get("type", "") or "")
        if msg_type == "focus_changed":
            window_class, window_title = self._window_info_from_payload(payload)
            log.info(
                "GNOME bridge focus update class=%r title=%r",
                window_class,
                window_title,
            )
            await self._emit_if_changed(window_class, window_title)
            return

        if msg_type == "pointer":
            request_id = int(payload.get("request_id", 0) or 0)
            future = self._pending_pointer.pop(request_id, None)
            if future is None or future.done():
                return
            x = int(payload.get("x", 0) or 0)
            y = int(payload.get("y", 0) or 0)
            future.set_result((x, y))
            return

        if msg_type == "activated":
            request_id = int(payload.get("request_id", 0) or 0)
            future = self._pending_activate.pop(request_id, None)
            if future is None or future.done():
                return
            future.set_result(payload)
            if payload.get("found"):
                window_class, window_title = self._window_info_from_payload(payload)
                if window_class or window_title:
                    await self._emit_if_changed(window_class, window_title)
            return

        if msg_type == "active_window":
            request_id = int(payload.get("request_id", 0) or 0)
            future = self._pending_window.pop(request_id, None)
            if future is None or future.done():
                return
            future.set_result(payload)

    async def _emit_if_changed(self, window_class: str, window_title: str) -> None:
        if window_class == self._last_class and window_title == self._last_title:
            return
        self._last_class = window_class
        self._last_title = window_title
        await self.callback(window_class, window_title, [])

    def _window_info_from_payload(self, payload: dict) -> tuple[str, str]:
        window_class = str(payload.get("app_id", "") or payload.get("wm_class", "") or "")
        window_title = str(payload.get("title", "") or "")
        return window_class, window_title

    async def _request_active_window(self) -> tuple[str, str]:
        if self._writer is None:
            return "", ""

        self._request_id += 1
        request_id = self._request_id
        future = asyncio.get_running_loop().create_future()
        self._pending_window[request_id] = future

        try:
            self._writer.write(
                (
                    json.dumps({"type": "get_active_window", "request_id": request_id}) + "\n"
                ).encode("utf-8")
            )
            await self._writer.drain()
            result = await asyncio.wait_for(future, timeout=0.6)
        except Exception:
            self._pending_window.pop(request_id, None)
            return "", ""

        if not isinstance(result, dict):
            return "", ""
        return self._window_info_from_payload(result)

    async def get_active_window(self) -> tuple[str, str, list[str]]:
        window_class, window_title = await self._request_active_window()
        if window_class or window_title:
            await self._emit_if_changed(window_class, window_title)
        return self._last_class, self._last_title, []

    async def activate_window_by_title(self, title: str) -> dict | None:
        """Ask the GNOME bridge extension to activate a window by title."""
        if self._writer is None:
            return None

        self._request_id += 1
        request_id = self._request_id
        future = asyncio.get_running_loop().create_future()
        self._pending_activate[request_id] = future

        try:
            self._writer.write(
                (
                    json.dumps(
                        {"type": "activate_title", "title": title, "request_id": request_id}
                    )
                    + "\n"
                ).encode("utf-8")
            )
            await self._writer.drain()
            result = await asyncio.wait_for(future, timeout=2.0)
            if isinstance(result, dict) and result.get("found"):
                window_class, window_title = await self._request_active_window()
                if window_class or window_title:
                    await self._emit_if_changed(window_class, window_title)
            return result
        except Exception:
            self._pending_activate.pop(request_id, None)
            return None

    async def get_cursor_position(self) -> tuple[int, int] | None:
        if self._writer is None:
            now = asyncio.get_running_loop().time()
            if now - self._last_warn_no_bridge > 30.0:
                log.debug("GNOME bridge not connected; cursor position unavailable")
                self._last_warn_no_bridge = now
            return None

        self._request_id += 1
        request_id = self._request_id
        future = asyncio.get_running_loop().create_future()
        self._pending_pointer[request_id] = future

        try:
            self._writer.write(
                (json.dumps({"type": "get_pointer", "request_id": request_id}) + "\n").encode(
                    "utf-8"
                )
            )
            await self._writer.drain()
            result = await asyncio.wait_for(future, timeout=0.6)
            return result
        except Exception:
            self._pending_pointer.pop(request_id, None)
            return None

    async def health_check(self) -> bool:
        if not self.running:
            return False
        if self._server is None:
            return False
        return await self.__class__.probe_available(self.dbus)
