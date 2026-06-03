import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path

import keymasq.session.gnome_shell as gnome_shell
from keymasq.common.coercion import int_value as _int_value
from keymasq.common.coercion import json_object as _json_object
from keymasq.common.coercion import str_value as _str_value
from keymasq.common.paths import GNOME_BRIDGE_SOCKET_PATH
from keymasq.common.security import get_peer_credentials
from keymasq.common.types import JsonObject
from keymasq.session.dbus import SessionDBus, name_has_owner
from keymasq.session.gnome_shell import GnomeShellDBusError
from keymasq.session.listeners._socket_helpers import (
    candidate_wayland_sockets,
    runtime_dir,
)
from keymasq.session.listeners.base import WindowChangeCallback, WindowListener
from keymasq.session.wayland_protocols.registry_probe import list_registry_globals

log = logging.getLogger("keymasq-session.listeners.gnome")


class GnomeListener(WindowListener):
    _EXTENSION_UUID = "gnome-bridge@keymasq.tools"
    _BRIDGE_PROTOCOL_VERSION = 1
    _NO_ARG_DISPATCHERS = frozenset({"close_active"})
    _TOGGLE_DISPATCHERS = frozenset({"fullscreen", "maximize"})
    _WORKSPACE_DISPATCHERS = frozenset({"workspace", "move_to_workspace"})
    _BRIDGE_STATE_READY = "ready"
    _BRIDGE_STATE_NOT_GNOME = "not_gnome"
    _BRIDGE_STATE_MISSING_FILES = "missing_files"
    _BRIDGE_STATE_SHELL_NOT_RESCANNED = "shell_not_rescanned"
    _BRIDGE_STATE_EXTENSIONS_DISABLED = "extensions_disabled"
    _BRIDGE_STATE_BRIDGE_DISABLED = "bridge_disabled"
    _BRIDGE_STATE_SHELL_DBUS_UNAVAILABLE = "shell_dbus_unavailable"
    _BRIDGE_STATE_PROTOCOL_STALE = "protocol_stale"
    _BRIDGE_STATE_PROTOCOL_NEWER = "protocol_newer"

    def __init__(
        self,
        callback: WindowChangeCallback,
        client: object | None = None,
        dbus: SessionDBus | None = None,
    ) -> None:
        super().__init__(callback, client, dbus=dbus)
        self._server: asyncio.AbstractServer | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._request_id = 0
        self._pending_pointer: dict[int, asyncio.Future[JsonObject | None]] = {}
        self._pending_pointer_set: dict[int, asyncio.Future[JsonObject | None]] = {}
        self._pending_activate: dict[int, asyncio.Future[JsonObject | None]] = {}
        self._pending_window: dict[int, asyncio.Future[JsonObject | None]] = {}
        self._pending_dispatch: dict[int, asyncio.Future[JsonObject | None]] = {}
        self._last_class = ""
        self._last_title = ""
        self._last_warn_no_bridge = 0.0
        self._bridge_connected = False
        self._bridge_protocol: int | None = None
        self._bridge_protocol_compatible = False

    @property
    def name(self) -> str:
        return "gnome"

    @property
    def supports_compositor_dispatch(self) -> bool:
        return True

    @property
    def compositor_dispatch_available(self) -> bool:
        return bool(
            self.running
            and self._writer is not None
            and self._bridge_connected
            and self._bridge_protocol_compatible
            and self.supports_compositor_dispatch
        )

    @classmethod
    def _has_runtime_prereqs(cls) -> bool:
        if not (runtime_dir() / "bus").exists():
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
    async def _bridge_extension_visible_to_shell(
        cls,
        dbus: SessionDBus | None = None,
    ) -> bool | None:
        try:
            return await gnome_shell.extension_visible(cls._EXTENSION_UUID, dbus)
        except Exception:
            return None

    @classmethod
    async def _user_extensions_globally_disabled(
        cls,
        dbus: SessionDBus | None = None,
    ) -> bool | None:
        try:
            return not await gnome_shell.user_extensions_enabled(dbus)
        except Exception:
            return None

    @classmethod
    async def _bridge_extension_enabled(cls, dbus: SessionDBus | None = None) -> bool | None:
        try:
            return await gnome_shell.extension_enabled(cls._EXTENSION_UUID, dbus)
        except Exception:
            return None

    @classmethod
    async def run_setup_action(
        cls,
        action: str,
        dbus: SessionDBus | None = None,
    ) -> tuple[bool, str]:
        action = str(action or "").strip()
        try:
            if action == "enable_extensions":
                await gnome_shell.set_user_extensions_enabled(True, dbus)
                return True, "GNOME Shell extensions are enabled. Enable the Keymasq bridge next."

            if action == "enable_bridge":
                ok = await gnome_shell.set_extension_enabled(cls._EXTENSION_UUID, True, dbus)
                if not ok:
                    return False, "GNOME Shell could not enable the Keymasq bridge extension."
                return True, "GNOME bridge enabled. Waiting for Keymasq to connect."

            if action == "logout":
                await gnome_shell.request_logout(dbus)
                return True, "GNOME logout requested."

            if action == "restart_session":
                await gnome_shell.request_user_service_restart("keymasq-session.service", dbus)
                return True, "keymasq-session restart requested."

            if action == "refresh":
                return True, "GNOME bridge status refreshed."

        except GnomeShellDBusError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, str(exc)

        return False, "Unsupported GNOME setup action."

    @classmethod
    async def _candidate_wayland_sockets(cls) -> list[Path]:
        return await candidate_wayland_sockets()

    @classmethod
    async def _probe_missing_native_toplevel_protocols(cls) -> bool:
        forbidden = {
            "zwlr_foreign_toplevel_manager_v1",
            "ext_foreign_toplevel_list_v1",
            "zcosmic_toplevel_info_v1",
        }
        for socket_path in await cls._candidate_wayland_sockets():
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
                "gnome_bridge_state": cls._BRIDGE_STATE_NOT_GNOME,
                "gnome_bridge_action": "",
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
                "gnome_bridge_state": cls._BRIDGE_STATE_MISSING_FILES,
                "gnome_bridge_action": "reinstall",
                "warning": (
                    "GNOME Shell detected, but the Keymasq GNOME bridge extension is not "
                    "installed. Reinstall Keymasq or follow the GNOME setup guide to install "
                    "'gnome-bridge@keymasq.tools'."
                ),
            }

        visible = await cls._bridge_extension_visible_to_shell(dbus)
        if visible is None:
            return {
                "session_detected": True,
                "extension_installed": True,
                "extension_enabled": False,
                "extensions_globally_disabled": False,
                "supported": False,
                "gnome_bridge_state": cls._BRIDGE_STATE_SHELL_DBUS_UNAVAILABLE,
                "gnome_bridge_action": "refresh",
                "warning": (
                    "GNOME Shell detected, but Keymasq could not query the GNOME Shell "
                    "extension service over DBus. Refresh GNOME support once Shell is ready."
                ),
            }
        if visible is False:
            return {
                "session_detected": True,
                "extension_installed": True,
                "extension_enabled": False,
                "extensions_globally_disabled": False,
                "supported": False,
                "gnome_bridge_state": cls._BRIDGE_STATE_SHELL_NOT_RESCANNED,
                "gnome_bridge_action": "logout",
                "warning": (
                    "GNOME Shell detected, and the Keymasq GNOME bridge extension files are "
                    "installed, but GNOME Shell does not see the extension yet. Log out and "
                    "back in so GNOME Shell rescans installed extensions."
                ),
            }

        extensions_disabled = await cls._user_extensions_globally_disabled(dbus)
        if extensions_disabled is None:
            return {
                "session_detected": True,
                "extension_installed": True,
                "extension_enabled": False,
                "extensions_globally_disabled": False,
                "supported": False,
                "gnome_bridge_state": cls._BRIDGE_STATE_SHELL_DBUS_UNAVAILABLE,
                "gnome_bridge_action": "refresh",
                "warning": (
                    "GNOME Shell detected, but Keymasq could not read GNOME Shell extension "
                    "settings over DBus. Refresh GNOME support once Shell is ready."
                ),
            }
        if extensions_disabled is True:
            return {
                "session_detected": True,
                "extension_installed": True,
                "extension_enabled": False,
                "extensions_globally_disabled": True,
                "supported": False,
                "gnome_bridge_state": cls._BRIDGE_STATE_EXTENSIONS_DISABLED,
                "gnome_bridge_action": "enable_extensions",
                "warning": (
                    "GNOME Shell detected, but GNOME extensions are globally disabled for this "
                    "session. Re-enable shell extensions before enabling the Keymasq GNOME "
                    "bridge."
                ),
            }

        enabled = await cls._bridge_extension_enabled(dbus)
        if enabled is None:
            return {
                "session_detected": True,
                "extension_installed": True,
                "extension_enabled": False,
                "extensions_globally_disabled": False,
                "supported": False,
                "gnome_bridge_state": cls._BRIDGE_STATE_SHELL_DBUS_UNAVAILABLE,
                "gnome_bridge_action": "refresh",
                "warning": (
                    "GNOME Shell detected, but Keymasq could not read the bridge extension "
                    "state over DBus. Refresh GNOME support once Shell is ready."
                ),
            }
        if enabled is False:
            return {
                "session_detected": True,
                "extension_installed": True,
                "extension_enabled": False,
                "extensions_globally_disabled": False,
                "supported": False,
                "gnome_bridge_state": cls._BRIDGE_STATE_BRIDGE_DISABLED,
                "gnome_bridge_action": "enable_bridge",
                "warning": (
                    "GNOME Shell detected, but the Keymasq GNOME bridge extension is not "
                    "enabled. Enable 'gnome-bridge@keymasq.tools' so Keymasq can use window-aware "
                    "profiles, GNOME window actions, and native pointer positioning."
                ),
            }

        return {
            "session_detected": True,
            "extension_installed": True,
            "extension_enabled": True,
            "extensions_globally_disabled": False,
            "supported": True,
            "gnome_bridge_state": cls._BRIDGE_STATE_READY,
            "gnome_bridge_action": "",
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
        self._bridge_connected = False
        self._bridge_protocol = None
        self._bridge_protocol_compatible = False

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

        for future in list(self._pending_pointer_set.values()):
            if not future.done():
                future.set_result(None)
        self._pending_pointer_set.clear()

        for future in list(self._pending_activate.values()):
            if not future.done():
                future.set_result(None)
        self._pending_activate.clear()

        for future in list(self._pending_window.values()):
            if not future.done():
                future.set_result(None)
        self._pending_window.clear()

        for future in list(self._pending_dispatch.values()):
            if not future.done():
                future.set_result(None)
        self._pending_dispatch.clear()

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
        self._bridge_protocol = None
        self._bridge_protocol_compatible = False
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
                    payload = _json_object(json.loads(raw.decode("utf-8")))
                except Exception:
                    continue
                if payload is None:
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
                self._bridge_protocol = None
                self._bridge_protocol_compatible = False
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_bridge_message(self, payload: JsonObject) -> None:
        msg_type = _str_value(payload.get("type"), "")
        if msg_type == "hello":
            protocol = _int_value(payload.get("protocol"), 0)
            self._bridge_protocol = protocol
            self._bridge_protocol_compatible = protocol == self._BRIDGE_PROTOCOL_VERSION
            if self._bridge_protocol_compatible:
                log.info("GNOME bridge protocol ready: %s", protocol)
            elif protocol < self._BRIDGE_PROTOCOL_VERSION:
                log.warning(
                    (
                        "GNOME bridge protocol mismatch: connected=%s expected=%s. "
                        "Log out and back in to reload the updated GNOME Shell extension."
                    ),
                    protocol,
                    self._BRIDGE_PROTOCOL_VERSION,
                )
            else:
                log.warning(
                    "GNOME bridge protocol mismatch: connected=%s expected=%s.",
                    protocol,
                    self._BRIDGE_PROTOCOL_VERSION,
                )
            return

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
            request_id = _int_value(payload.get("request_id"), 0)
            future = self._pending_pointer.pop(request_id, None)
            if future is None or future.done():
                return
            future.set_result(payload)
            return

        if msg_type == "pointer_set_result":
            request_id = _int_value(payload.get("request_id"), 0)
            future = self._pending_pointer_set.pop(request_id, None)
            if future is None or future.done():
                return
            future.set_result(payload)
            return

        if msg_type == "activated":
            request_id = _int_value(payload.get("request_id"), 0)
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
            request_id = _int_value(payload.get("request_id"), 0)
            future = self._pending_window.pop(request_id, None)
            if future is None or future.done():
                return
            future.set_result(payload)
            return

        if msg_type == "dispatch_result":
            request_id = _int_value(payload.get("request_id"), 0)
            future = self._pending_dispatch.pop(request_id, None)
            if future is None or future.done():
                return
            future.set_result(payload)
            if payload.get("ok"):
                window_class, window_title = self._window_info_from_payload(payload)
                if window_class or window_title:
                    await self._emit_if_changed(window_class, window_title)
            return

    async def _emit_if_changed(self, window_class: str, window_title: str) -> None:
        if window_class == self._last_class and window_title == self._last_title:
            return
        self._last_class = window_class
        self._last_title = window_title
        await self.callback(window_class, window_title, [])

    def _window_info_from_payload(self, payload: JsonObject) -> tuple[str, str]:
        window_class = _str_value(payload.get("app_id") or payload.get("wm_class"), "")
        window_title = _str_value(payload.get("title"), "")
        return window_class, window_title

    def _validate_dispatch(self, dispatcher: str, args: str) -> tuple[bool, str]:
        dispatcher_name = str(dispatcher or "").strip()
        dispatcher_args = str(args or "").strip()
        if not dispatcher_name:
            return False, "missing dispatcher"

        if dispatcher_name in self._NO_ARG_DISPATCHERS:
            if dispatcher_args:
                return False, f"{dispatcher_name} does not accept arguments"
            return True, ""

        if dispatcher_name in self._TOGGLE_DISPATCHERS:
            if dispatcher_args not in {"toggle", "on", "off"}:
                return False, f"{dispatcher_name} expects toggle, on, or off"
            return True, ""

        if dispatcher_name in self._WORKSPACE_DISPATCHERS:
            if dispatcher_args in {"next", "prev"}:
                return True, ""
            try:
                index = int(dispatcher_args)
            except ValueError:
                return False, f"{dispatcher_name} expects next, prev, or a workspace number"
            if index < 1:
                return False, "workspace number must be >= 1"
            return True, ""

        return False, f"unsupported GNOME dispatcher: {dispatcher_name}"

    def runtime_support_details(self) -> dict[str, bool | str | int]:
        details: dict[str, bool | str | int] = {
            "bridge_connected": self._bridge_connected,
            "bridge_protocol_expected": self._BRIDGE_PROTOCOL_VERSION,
        }
        if self._bridge_protocol is not None:
            details["bridge_protocol"] = self._bridge_protocol
        if self._bridge_connected and self._bridge_protocol is not None:
            if self._bridge_protocol == self._BRIDGE_PROTOCOL_VERSION:
                details["warning"] = ""
                details["gnome_bridge_state"] = self._BRIDGE_STATE_READY
                details["gnome_bridge_action"] = ""
            elif self._bridge_protocol < self._BRIDGE_PROTOCOL_VERSION:
                details["gnome_bridge_state"] = self._BRIDGE_STATE_PROTOCOL_STALE
                details["gnome_bridge_action"] = "logout"
                details["warning"] = (
                    "GNOME bridge update detected. Log out and back in to reload the "
                    "updated GNOME Shell extension and enable new GNOME bridge features."
                )
            else:
                details["gnome_bridge_state"] = self._BRIDGE_STATE_PROTOCOL_NEWER
                details["gnome_bridge_action"] = "restart_session"
                details["warning"] = (
                    "GNOME bridge is newer than keymasq-session. Restart Keymasq and "
                    "ensure both sides are updated together."
                )
        return details

    async def _send_request(self, payload: JsonObject, timeout: float) -> JsonObject | None:
        if self._writer is None:
            return None

        self._request_id += 1
        request_id = self._request_id
        request_payload = dict(payload)
        request_payload["request_id"] = request_id
        future: asyncio.Future[JsonObject | None] = asyncio.get_running_loop().create_future()
        msg_type = _str_value(request_payload.get("type"), "")

        if msg_type == "get_active_window":
            self._pending_window[request_id] = future
        elif msg_type == "get_pointer":
            self._pending_pointer[request_id] = future
        elif msg_type == "set_pointer":
            self._pending_pointer_set[request_id] = future
        elif msg_type == "activate_title":
            self._pending_activate[request_id] = future
        elif msg_type == "dispatch":
            self._pending_dispatch[request_id] = future
        else:
            return None

        try:
            self._writer.write((json.dumps(request_payload) + "\n").encode("utf-8"))
            await self._writer.drain()
            result = await asyncio.wait_for(future, timeout=timeout)
        except Exception:
            self._pending_window.pop(request_id, None)
            self._pending_pointer.pop(request_id, None)
            self._pending_pointer_set.pop(request_id, None)
            self._pending_activate.pop(request_id, None)
            self._pending_dispatch.pop(request_id, None)
            return None

        return result

    async def _request_active_window(self) -> tuple[str, str]:
        result = await self._send_request({"type": "get_active_window"}, timeout=0.6)
        if result is None:
            return "", ""
        return self._window_info_from_payload(result)

    async def get_active_window(self) -> tuple[str, str, list[str]]:
        window_class, window_title = await self._request_active_window()
        if window_class or window_title:
            await self._emit_if_changed(window_class, window_title)
        return self._last_class, self._last_title, []

    async def activate_window_by_title(self, title: str) -> JsonObject | None:
        """Ask the GNOME bridge extension to activate a window by title."""
        result = await self._send_request(
            {"type": "activate_title", "title": title},
            timeout=2.0,
        )
        if result and result.get("found"):
            window_class, window_title = await self._request_active_window()
            if window_class or window_title:
                await self._emit_if_changed(window_class, window_title)
        return result

    async def dispatch(self, dispatcher: str, args: str = "") -> tuple[bool, str]:
        dispatcher_name = " ".join(str(dispatcher or "").strip().split())
        dispatcher_args = " ".join(str(args or "").strip().split())
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
        ok, message = self._validate_dispatch(dispatcher_name, dispatcher_args)
        if not ok:
            return False, message
        if self._writer is None or not self._bridge_connected:
            return False, "GNOME bridge not connected"
        if not self._bridge_protocol_compatible:
            warning = str(self.runtime_support_details().get("warning", "") or "").strip()
            return False, warning or "GNOME bridge protocol is not ready"

        result = await self._send_request(
            {
                "type": "dispatch",
                "dispatcher": dispatcher_name,
                "args": dispatcher_args,
            },
            timeout=1.5,
        )
        if result is None:
            return False, "GNOME bridge not connected"
        return bool(result.get("ok")), _str_value(result.get("message"), "")

    async def get_cursor_position(self) -> tuple[int, int] | None:
        if self._writer is None:
            now = asyncio.get_running_loop().time()
            if now - self._last_warn_no_bridge > 30.0:
                log.debug("GNOME bridge not connected; cursor position unavailable")
                self._last_warn_no_bridge = now
            return None

        result = await self._send_request({"type": "get_pointer"}, timeout=0.6)
        if result is None:
            return None
        x = _int_value(result.get("x"), 0)
        y = _int_value(result.get("y"), 0)
        return x, y

    async def set_cursor_position(self, x: int, y: int) -> tuple[bool, str]:
        if self._writer is None or not self._bridge_connected:
            return False, "GNOME bridge not connected"
        if not self._bridge_protocol_compatible:
            warning = str(self.runtime_support_details().get("warning", "") or "").strip()
            return False, warning or "GNOME bridge protocol is not ready"

        result = await self._send_request(
            {
                "type": "set_pointer",
                "x": int(x),
                "y": int(y),
            },
            timeout=0.6,
        )
        if result is None:
            return False, "GNOME bridge not connected"
        return bool(result.get("ok")), _str_value(result.get("message"), "")

    async def health_check(self) -> bool:
        if not self.running:
            return False
        if self._server is None:
            return False
        return await self.__class__.probe_available(self.dbus)
