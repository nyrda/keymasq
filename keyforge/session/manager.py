import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import signal
import traceback
from datetime import datetime

from keyforge.common.devices import normalize_input_classes
from keyforge.common.ipc import Command, CommandType
from keyforge.common.models import MappingAction
from keyforge.common.paths import (
    CONFIG_DIR,
    SECURITY_POLICY_PATH,
    SESSION_SOCKET_PATH,
    SOCKET_PATH,
    ensure_session_socket_dir,
)
from keyforge.common.recording_guard import resolve_unlock_status
from keyforge.common.security import (
    PeerCredentials,
    SecurityPolicy,
    command_allowed,
    get_peer_credentials,
    load_security_policy,
    uid_allowed,
)
from keyforge.session.action_handler import ActionHandler
from keyforge.session.client import KeyforgedClient
from keyforge.session.compositor import (
    detect_compositor,
    get_compositor_capabilities,
    get_compositor_name,
    get_compositor_support_details,
    get_listener_class,
    is_compositor_supported,
)
from keyforge.session.dbus import SessionDBus
from keyforge.session.hardware import HardwareManager
from keyforge.session.listeners.base import WindowListener
from keyforge.session.profiles import ProfileManager, ResolvedCombo, ResolvedDeviceProfile
from keyforge.session.superkeys import SuperkeyManager

log = logging.getLogger("keyforge-session")
GRAB_DEVICE_TIMEOUT_S = 330.0
GRAB_RETRY_DELAY_S = 5.0


class SessionManager:
    _RECORDING_SETTINGS_PATH = CONFIG_DIR / "recording_settings.json"
    _MAX_SESSION_CLIENT_BUFFER_BYTES = 16 * 1024 * 1024

    def __init__(self, verbosity: int = 0) -> None:
        self.client = KeyforgedClient(self._handle_event)
        self.superkeys = SuperkeyManager()
        self.profiles = ProfileManager(superkey_manager=self.superkeys)
        self.hardware = HardwareManager()
        self.action_handler: ActionHandler | None = None
        self.running = False
        self._shutdown_event = asyncio.Event()
        self._retry_event = asyncio.Event()
        self._connected = False
        self._reload_task: asyncio.Task | None = None
        self._reload_pending = False
        self.verbosity = verbosity

        self._grabbed_devices: set[str] = set()
        self._grabbed_interfaces: dict[str, dict[str, str]] = {}
        self._grab_waiting_devices: set[str] = set()
        self._grab_retry_tasks: dict[str, asyncio.Task] = {}
        self._last_sent_mapping_signatures: dict[str, str] = {}
        self._last_sent_combo_signature: str = ""
        self._active_profile_names: list[str] = []
        self._resolved_devices: dict[str, ResolvedDeviceProfile] = {}
        self._current_window: dict = {}
        self._window_listener: WindowListener | None = None
        self._compositor_id: str | None = None
        self._compositor_capabilities: list[str] = []
        self._compositor_supervisor_task: asyncio.Task | None = None
        self._connect_task: asyncio.Task | None = None
        self._compositor_candidate: str | None = None
        self._compositor_candidate_hits: int = 0
        self._compositor_probe_fast_s: float = 1.0
        self._compositor_probe_slow_s: float = 5.0
        self._listener_retry_after: dict[str, float] = {}
        self._listener_last_error: dict[str, str] = {}
        self._listener_last_log_at: dict[str, float] = {}
        self._listener_retry_interval_s: float = 30.0
        self._listener_log_interval_s: float = 60.0
        self._last_listener_start_error: str = ""
        self._session_server: asyncio.Server | None = None
        self._session_clients: set[asyncio.StreamWriter] = set()
        self._session_client_peers: dict[asyncio.StreamWriter, PeerCredentials] = {}
        self._capture_locks: set[str] = set()
        self._capture_resume_profiles: dict[str, list[str]] = {}
        self._capture_tokens: dict[str, str] = {}

        self._exec_refs: dict[int, str] = {}
        self._next_exec_ref: int = 1
        self._device_exec_refs: dict[str, set[int]] = {}
        self._combo_exec_refs: set[int] = set()

        self._superkey_exec_refs: dict[int, tuple[str, str]] = {}
        self._next_superkey_exec_ref: int = 10000
        self._recording_active: bool = False
        self._pending_recording_data: dict | None = None
        self._recording_start_cursor: tuple[int, int] | None = None
        self._recording_settings: dict = {
            "include_mouse_movement": False,
            "include_mouse_clicks": False,
            "record_start_position": False,
            "record_keyboard": True,
            "record_mouse": False,
            "record_gamepad": True,
            "device_overrides": {},
        }
        self._recording_settings_pending_save: dict | None = None
        self._recording_settings_save_task: asyncio.Task | None = None
        self._load_recording_settings_from_disk()
        self._recording_devices_cache: list[dict] = []
        self._recording_refresh_owner: dict | None = None
        self._runtime_refresh_claim_consumed_until: dict[int, int] = {}
        self._recording_refresh_ttl_s = 60
        self._security_policy: SecurityPolicy = load_security_policy(SECURITY_POLICY_PATH)
        self.dbus = SessionDBus()

        self.action_handler = ActionHandler()

    async def start(self) -> None:
        self.running = True

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._signal_handler)
        for sig in (signal.SIGHUP,):
            loop.add_signal_handler(sig, self._reload_handler)

        log.info("Starting keyforge-session")

        await self._start_session_server()

        self._connect_task = asyncio.create_task(self._connect_loop())
        self._compositor_supervisor_task = asyncio.create_task(self._compositor_supervisor_loop())

        try:
            await self._shutdown_event.wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        if not self.running:
            return

        log.info("Stopping keyforge-session")
        self.running = False

        self._shutdown_event.set()

        if self._compositor_supervisor_task:
            self._compositor_supervisor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._compositor_supervisor_task
            self._compositor_supervisor_task = None

        if self._recording_settings_save_task:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._recording_settings_save_task
            self._recording_settings_save_task = None

        await self._stop_window_listener()
        await self.dbus.disconnect()

        if self._session_server:
            self._session_server.close()
            await self._session_server.wait_closed()

        for writer in list(self._session_clients):
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        await self._wait_for_session_clients_to_close()

        for token in list(self._capture_tokens.values()):
            try:
                await self.client.send_command(
                    Command(command=CommandType.CAPTURE_END, data={"token": token})
                )
            except Exception:
                pass
        self._capture_tokens.clear()

        await self.client.disconnect()

        if self._connect_task:
            self._connect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._connect_task
            self._connect_task = None

        if SESSION_SOCKET_PATH.exists():
            try:
                SESSION_SOCKET_PATH.unlink()
            except Exception:
                pass

    async def _start_session_server(self) -> None:
        ensure_session_socket_dir()

        if SESSION_SOCKET_PATH.exists():
            try:
                SESSION_SOCKET_PATH.unlink()
            except Exception:
                pass

        self._session_server = await asyncio.start_unix_server(
            self._handle_session_client,
            path=str(SESSION_SOCKET_PATH),
        )
        try:
            os.chmod(SESSION_SOCKET_PATH, 0o600)
        except OSError:
            log.warning(
                "Failed to set session socket permissions to 0600 on %s; "
                "socket may be accessible to other users",
                SESSION_SOCKET_PATH,
            )
        log.info(f"Session server listening on {SESSION_SOCKET_PATH}")
        log.info(
            "Session security policy loaded from %s",
            SECURITY_POLICY_PATH,
        )

    async def _handle_session_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = get_peer_credentials(writer.get_extra_info("socket"))
        if peer is None:
            writer.close()
            await writer.wait_closed()
            return

        if not uid_allowed(peer.uid, self._security_policy.session_allowed_uids):
            log.warning(
                "Denied session client pid=%s uid=%s reason=%s",
                peer.pid,
                peer.uid,
                f"uid {peer.uid} is not allowed by session policy",
            )
            writer.close()
            await writer.wait_closed()
            return

        client_class = "client"

        log.debug(
            "Session client connected pid=%s uid=%s class=%s",
            peer.pid,
            peer.uid,
            client_class,
        )
        self._session_clients.add(writer)
        self._session_client_peers[writer] = peer
        buffer = b""

        try:
            while self.running:
                data = await reader.read(4096)
                if not data:
                    break

                buffer += data
                if len(buffer) > self._MAX_SESSION_CLIENT_BUFFER_BYTES:
                    log.warning(
                        "Session client exceeded max buffered request size pid=%s uid=%s bytes=%s",
                        peer.pid,
                        peer.uid,
                        len(buffer),
                    )
                    break

                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.strip():
                        try:
                            request = json.loads(line.decode())
                            response = await self._handle_session_request(
                                request,
                                client_class,
                                peer,
                                writer,
                            )
                            writer.write(json.dumps(response).encode() + b"\n")
                            await writer.drain()
                        except json.JSONDecodeError:
                            writer.write(json.dumps({"error": "invalid json"}).encode() + b"\n")
                            await writer.drain()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.debug(f"Session client error: {e}")
        finally:
            await self._clear_recording_refresh_owner_if_writer(peer, writer)
            self._session_clients.discard(writer)
            self._session_client_peers.pop(writer, None)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_session_request(
        self,
        request: dict,
        client_class: str,
        peer: PeerCredentials,
        writer: asyncio.StreamWriter,
    ) -> dict:
        command = request.get("command", "")
        policy = self._security_policy

        if not command_allowed(command, policy.session_command_acl, client_class):
            return {
                "status": "error",
                "message": f"{client_class} is not allowed to call '{command}'",
            }

        if self._is_sensitive_session_command(
            command, policy
        ) and not self._is_refresh_owner_request(peer, writer):
            return {
                "status": "error",
                "error_code": "sensitive_command_denied",
                "message": "Sensitive command denied: caller is not active GUI owner",
            }

        if command == "get_active_profiles":
            return self._build_active_profiles_payload()

        if command == "list_profiles":
            return self._build_profile_overview()

        if command in {"enable_profile", "disable_profile", "toggle_profile"}:
            profile_name = str(request.get("profile_name", "") or "")
            if not profile_name:
                return {"status": "error", "message": "missing profile_name"}

            enabled: bool | None = None
            if command == "enable_profile":
                enabled = True
            elif command == "disable_profile":
                enabled = False

            result = await self._set_profile_enabled(profile_name, enabled)
            return result

        if command == "get_compositor":
            details = await get_compositor_support_details(self._compositor_id, self.dbus)
            return {
                "compositor_id": self._compositor_id,
                "compositor_name": get_compositor_name(self._compositor_id),
                "supported": bool(details.get("supported", False)),
                "capabilities": get_compositor_capabilities(self._compositor_id),
                "details": details,
                "listener_active": self._window_listener is not None,
                "listener_name": (
                    getattr(self._window_listener, "name", "")
                    if self._window_listener is not None
                    else ""
                ),
                "compositor_dispatch_available": self._compositor_dispatch_available(),
            }

        if command == "get_active_window":
            return await self._get_active_window_payload()

        if command == "activate_title":
            title = str(request.get("title", "") or "").strip()
            if not title:
                return {"status": "error", "message": "title parameter required"}
            listener = self._window_listener
            if listener is None or not hasattr(listener, "activate_window_by_title"):
                return {
                    "status": "error",
                    "message": "Window activation not supported on this compositor",
                }
            try:
                result = await listener.activate_window_by_title(title)
                if result and result.get("found"):
                    return {"status": "ok", "title": title, "found": True}
                return {
                    "status": "error",
                    "message": f"Window with title {title!r} not found",
                    "details": result,
                }
            except Exception as exc:
                return {"status": "error", "message": str(exc)}

        if command == "get_cursor_position":
            pos = None

            if self._window_listener:
                try:
                    pos = await self._window_listener.get_cursor_position()
                except Exception as e:
                    log.debug(
                        "Cursor query failed (compositor_id=%s listener=%s): %s",
                        self._compositor_id,
                        getattr(self._window_listener, "name", "unknown"),
                        e,
                    )

            if pos is None:
                return {
                    "status": "error",
                    "message": "Cursor position is unavailable on this compositor",
                }
            return {"status": "ok", "x": int(pos[0]), "y": int(pos[1])}

        if command == "reload":
            await self._reload_profiles()
            return {"status": "ok"}

        if command in {"reevaluate_profiles", "reevaluate_hardware"}:
            log.info("Global profile reevaluate requested")
            await asyncio.to_thread(self._reload_config_from_disk)
            await self._reevaluate_profiles()
            return {"status": "ok"}

        if command == "ping":
            return {"status": "ok"}

        if command == "get_status":
            unlock_status = await self._resolve_unlock_status_async(peer.uid)
            compositor_details = await get_compositor_support_details(
                self._compositor_id, self.dbus
            )
            policy = self._security_policy
            return {
                "status": "ok",
                "keyforged_connected": self._connected,
                "compositor_id": self._compositor_id,
                "compositor_name": get_compositor_name(self._compositor_id),
                "compositor_supported": bool(compositor_details.get("supported", False)),
                "compositor_details": compositor_details,
                "listener_active": self._window_listener is not None,
                "listener_name": (
                    getattr(self._window_listener, "name", "")
                    if self._window_listener is not None
                    else ""
                ),
                "compositor_dispatch_available": self._compositor_dispatch_available(),
                "active_profiles": list(self._active_profile_names),
                "recording_active": self._recording_active,
                "macro_exec_timeout_max_ms": int(policy.macro_exec_timeout_max_ms),
                "gui_allow_left_right_click_remap": bool(
                    policy.gui_allow_left_right_click_remap
                ),
                **self._serialize_recording_unlock_state(
                    unlock_status,
                    refresh_owner=self._is_refresh_owner_request(peer, writer),
                ),
            }

        if command == "start_recording":
            self._update_recording_settings(request)
            start_result = await self._start_recording(reset_if_active=False)
            self._notify_recording_unlock_required(start_result)
            return start_result

        if command == "set_recording_settings":
            self._update_recording_settings(request)
            return {"status": "ok", **self._recording_settings}

        if command == "get_recording_settings":
            unlock_status = await self._resolve_unlock_status_async(peer.uid)
            return {
                "status": "ok",
                **self._serialize_recording_unlock_state(
                    unlock_status,
                    refresh_owner=self._is_refresh_owner_request(peer, writer),
                ),
                **self._recording_settings,
            }

        if command == "claim_recording_unlock_refresh":
            return await self._claim_recording_unlock_refresh(peer, writer)

        if command == "refresh_recording_unlock":
            lease_id = str(request.get("lease_id", "") or "").strip()
            return await self._refresh_recording_unlock(peer, writer, lease_id)

        if command == "lock_recording_unlock":
            lease_id = str(request.get("lease_id", "") or "").strip()
            return await self._lock_recording_unlock(peer, writer, lease_id)

        if command == "stop_recording":
            if not self._recording_active:
                return {"status": "error", "message": "No recording in progress"}
            try:
                result = await self.client.send_command(Command(command=CommandType.STOP_RECORDING))
            except Exception:
                return {"status": "error", "message": "Daemon unavailable"}
            if result.status == "ok":
                if isinstance(result.data, dict):
                    self._pending_recording_data = result.data
                    self._recording_active = False
                    return {"status": "ok", **result.data}
                self._recording_active = False
                return {"status": "ok"}
            return {"status": "error", "message": result.error or "Failed to stop recording"}

        if command == "save_recording":
            name = request.get("name", "").strip()
            if not name:
                return {"status": "error", "message": "Name required"}
            if not self._pending_recording_data:
                return {"status": "error", "message": "No pending recording"}
            save_result = await self._save_recording(
                name,
                move_to_start=bool(request.get("move_to_start", False)),
                start_x=int(request.get("start_x", 0)),
                start_y=int(request.get("start_y", 0)),
                block_mouse_movement=bool(request.get("block_mouse_movement", False)),
            )
            if save_result.get("status") != "ok":
                return save_result
            return {"status": "ok", "name": save_result.get("name", name)}

        if command == "discard_recording":
            self._pending_recording_data = None
            return {"status": "ok"}

        if command == "list_macros":
            try:
                result = await self.client.send_command(
                    Command(command=CommandType.MACRO_LIST_META)
                )
            except Exception:
                return {"status": "error", "message": "Daemon unavailable"}
            if result.status == "ok" and isinstance(result.data, dict):
                return {"status": "ok", "macros": result.data.get("macros", [])}
            return {"status": "error", "message": result.error or "Failed to list macros"}

        if command == "get_macro":
            name = request.get("name", "")
            try:
                result = await self.client.send_command(
                    Command(command=CommandType.MACRO_GET, data={"name": name})
                )
            except Exception:
                return {"status": "error", "message": "Daemon unavailable"}
            if result.status == "ok" and isinstance(result.data, dict):
                return {"status": "ok", "macro": result.data.get("macro")}
            return {"status": "error", "message": result.error or "Macro not found"}

        if command == "create_macro":
            macro = request.get("macro")
            if not isinstance(macro, dict):
                return {"status": "error", "message": "macro payload required"}
            try:
                result = await self.client.send_command(
                    Command(command=CommandType.MACRO_CREATE, data={"macro": macro})
                )
            except Exception:
                return {"status": "error", "message": "Daemon unavailable"}
            if result.status == "ok" and isinstance(result.data, dict):
                created = result.data.get("macro", {})
                self._broadcast_to_session_clients(
                    {"event": "macro_saved", "name": created.get("name", "")}
                )
                return {"status": "ok", "macro": created}
            return {"status": "error", "message": result.error or "Failed to create macro"}

        if command == "update_macro":
            name = request.get("name", "")
            macro = request.get("macro")
            if not isinstance(macro, dict):
                return {"status": "error", "message": "macro payload required"}
            payload = {"name": name, "macro": macro}
            if "expected_revision" in request:
                payload["expected_revision"] = request.get("expected_revision")
            try:
                result = await self.client.send_command(
                    Command(command=CommandType.MACRO_UPDATE, data=payload)
                )
            except Exception:
                return {"status": "error", "message": "Daemon unavailable"}
            if result.status == "ok" and isinstance(result.data, dict):
                updated = result.data.get("macro", {})
                self._broadcast_to_session_clients(
                    {"event": "macro_saved", "name": updated.get("name", name)}
                )
                return {"status": "ok", "macro": updated}
            return {"status": "error", "message": result.error or "Failed to update macro"}

        if command == "delete_macro":
            name = request.get("name", "")
            payload = {"name": name}
            if "expected_revision" in request:
                payload["expected_revision"] = request.get("expected_revision")
            try:
                result = await self.client.send_command(
                    Command(command=CommandType.MACRO_DELETE, data=payload)
                )
            except Exception:
                return {"status": "error", "message": "Daemon unavailable"}
            if result.status != "ok":
                return {"status": "error", "message": result.error or "Failed to delete macro"}
            await self._reload_profiles()
            return {"status": "ok"}

        if command == "rename_macro":
            payload = {
                "old_name": request.get("old", ""),
                "new_name": request.get("new", ""),
            }
            if "expected_revision" in request:
                payload["expected_revision"] = request.get("expected_revision")
            try:
                result = await self.client.send_command(
                    Command(command=CommandType.MACRO_RENAME, data=payload)
                )
            except Exception:
                return {"status": "error", "message": "Daemon unavailable"}
            if result.status != "ok":
                return {"status": "error", "message": result.error or "Failed to rename macro"}
            await self._reload_profiles()
            if isinstance(result.data, dict):
                return {"status": "ok", "macro": result.data.get("macro")}
            return {"status": "ok"}

        if command == "play_macro":
            name = request.get("name", "")
            try:
                get_result = await self.client.send_command(
                    Command(command=CommandType.MACRO_GET, data={"name": name})
                )
            except Exception:
                return {"status": "error", "message": "Daemon unavailable"}

            if get_result.status != "ok" or not isinstance(get_result.data, dict):
                return {"status": "error", "message": get_result.error or "Macro not found"}

            macro = get_result.data.get("macro")
            if not isinstance(macro, dict):
                return {"status": "error", "message": "Macro not found"}

            macro = self._sanitize_macro_for_policy(macro)
            payload = {
                "macro_name": str(macro.get("name", name) or name),
                "macro_events": macro.get("events", []),
                "replay_mouse_movement": request.get("replay_mouse_movement", True),
                "replay_mouse_clicks": request.get("replay_mouse_clicks", True),
                "speed": float(request.get("speed", 1.0)),
                "loop_mode": str(macro.get("loop_mode", "none") or "none"),
                "loop_count": int(macro.get("loop_count", 1) or 1),
                "move_to_start": bool(macro.get("move_to_start", False)),
                "start_x": int(macro.get("start_x", 0) or 0),
                "start_y": int(macro.get("start_y", 0) or 0),
                "block_mouse_movement": bool(macro.get("block_mouse_movement", False)),
            }

            try:
                result = await self.client.send_command(
                    Command(command=CommandType.PLAY_MACRO, data=payload)
                )
            except Exception:
                return {"status": "error", "message": "Daemon unavailable"}
            if result.status == "ok":
                return result.data or {"status": "ok"}
            return {"status": "error", "message": result.error or "playback failed"}

        if command == "cancel_macro_playback":
            try:
                result = await self.client.send_command(
                    Command(command=CommandType.CANCEL_MACRO_PLAYBACK)
                )
            except Exception:
                return {"status": "error", "message": "Daemon unavailable"}
            if result.status == "ok":
                return result.data or {"status": "ok", "cancelled": True}
            return {"status": "error", "message": result.error or "cancel failed"}

        if command == "list_devices_for_recording":
            devices = await self._get_devices_for_recording(
                ["keyboard", "gamepad", "mouse"],
                include_grabbed=True,
            )
            self._recording_devices_cache = [d for d in devices if not d.get("grabbed_by_keyforge")]
            return {"status": "ok", "devices": devices}

        if command == "begin_capture":
            hardware_id = request.get("hardware_id", "")
            if not hardware_id:
                return {"error": "missing hardware_id"}
            return await self._capture_begin(hardware_id)

        if command == "capture_read":
            hardware_id = request.get("hardware_id", "")
            if not hardware_id:
                return {"error": "missing hardware_id"}
            return await self._capture_read(hardware_id)

        if command == "end_capture":
            hardware_id = request.get("hardware_id", "")
            if not hardware_id:
                return {"error": "missing hardware_id"}
            return await self._capture_end(hardware_id)

        if command == "capture_combo":
            profile_name = str(request.get("profile_name", "") or "")
            if not profile_name:
                return {"error": "missing profile_name"}
            timeout_s = float(request.get("timeout_s", 15.0) or 15.0)
            return await self._capture_combo(profile_name, timeout_s)

        if command == "set_diagnostics":
            enabled = bool(request.get("enabled", False))
            interval = float(request.get("interval", 5.0))
            try:
                result = await self.client.send_command(
                    Command(
                        command=CommandType.SET_DIAGNOSTICS,
                        data={"enabled": enabled, "interval": interval},
                    )
                )
            except Exception:
                return {"status": "error", "message": "Daemon unavailable"}

            if result.status == "ok":
                return {"status": "ok", "data": result.data or {}}
            return {"status": "error", "message": result.error or "Failed to update diagnostics"}

        return {"error": f"Unknown command: {command}"}

    def _is_sensitive_session_command(
        self,
        command: str,
        policy: SecurityPolicy | None = None,
    ) -> bool:
        if policy is None:
            policy = self._security_policy

        if command == "lock_recording_unlock":
            return True

        if policy.recording_unlock_required and command in {
            "start_recording",
            "begin_capture",
            "capture_read",
            "end_capture",
            "capture_combo",
        }:
            return True

        if policy.macro_edit_requires_unlock and command in {
            "get_macro",
            "create_macro",
            "update_macro",
        }:
            return True

        return False

    def _has_active_gui_recording_owner(self) -> bool:
        owner = self._recording_refresh_owner
        if owner is None:
            return False
        return bool(str(owner.get("lease_id", "") or "").strip())

    async def _resolve_unlock_status_async(self, uid: int) -> dict[str, bool | int | str]:
        return await asyncio.to_thread(resolve_unlock_status, uid)

    def _serialize_recording_unlock_state(
        self,
        unlock_status: dict[str, bool | int | str],
        *,
        refresh_owner: bool,
    ) -> dict[str, bool | int | str]:
        unlock_required = bool(self._security_policy.recording_unlock_required)
        raw_unlocked = bool(unlock_status.get("unlocked", False))
        return {
            "recording_unlock_required": unlock_required,
            "recording_unlocked": raw_unlocked,
            "recording_unlock_source": str(unlock_status.get("source", "none") or "none"),
            "recording_unlock_expires_at": int(unlock_status.get("expires_at", 0) or 0),
            "recording_refresh_owner": bool(refresh_owner),
        }

    def _is_refresh_owner_request(
        self,
        peer: PeerCredentials,
        writer: asyncio.StreamWriter,
    ) -> bool:
        owner = self._recording_refresh_owner
        if owner is None:
            return False
        return (
            owner.get("uid") == int(peer.uid)
            and owner.get("pid") == int(peer.pid)
            and owner.get("writer_id") == id(writer)
        )

    def _has_other_session_client_for_uid(
        self,
        uid: int,
        *,
        excluding: asyncio.StreamWriter | None = None,
    ) -> bool:
        for current_writer, peer in self._session_client_peers.items():
            if current_writer is excluding:
                continue
            if int(peer.uid) == int(uid):
                return True
        return False

    async def _cleanup_runtime_unlock_for_uid(self, uid: int, *, reason: str) -> None:
        try:
            result = await self.client.send_command(
                Command(
                    command=CommandType.LOCK_RECORDING_UNLOCK,
                    data={"uid": int(uid), "cleanup": True},
                )
            )
            if result.status == "ok":
                log.info("Runtime unlock cleaned up uid=%s reason=%s", uid, reason)
            else:
                log.debug(
                    "Runtime unlock cleanup failed uid=%s reason=%s error=%s",
                    uid,
                    reason,
                    result.error,
                )
        except Exception as e:
            log.debug(
                "Runtime unlock cleanup failed uid=%s reason=%s error=%s",
                uid,
                reason,
                e,
            )

    async def _clear_recording_refresh_owner_if_writer(
        self,
        peer: PeerCredentials,
        writer: asyncio.StreamWriter,
    ) -> None:
        owner = self._recording_refresh_owner
        uid = int(peer.uid)

        if owner is not None and owner.get("writer_id") == id(writer):
            self._recording_refresh_owner = None
            self._runtime_refresh_claim_consumed_until.pop(uid, None)
            await self._cleanup_runtime_unlock_for_uid(uid, reason="refresh_owner_disconnect")
            return

        if owner is not None and owner.get("uid") == uid:
            return

        if self._has_other_session_client_for_uid(uid, excluding=writer):
            return

        unlock_status = await self._resolve_unlock_status_async(uid)
        if not bool(unlock_status.get("unlocked", False)):
            return

        if str(unlock_status.get("source", "none") or "none") != "runtime":
            return

        await self._cleanup_runtime_unlock_for_uid(uid, reason="last_client_disconnect")

    async def _claim_recording_unlock_refresh(
        self,
        peer: PeerCredentials,
        writer: asyncio.StreamWriter,
    ) -> dict:
        unlock_status = await self._resolve_unlock_status_async(peer.uid)
        if not bool(unlock_status.get("unlocked", False)):
            return {
                "status": "error",
                "error_code": "recording_locked",
                "message": "recording_locked: unlock required before claiming refresh",
            }

        source = str(unlock_status.get("source", "none") or "none")
        expires_at = int(unlock_status.get("expires_at", 0) or 0)

        if source == "runtime":
            consumed_until = int(
                self._runtime_refresh_claim_consumed_until.get(int(peer.uid), 0) or 0
            )
            if expires_at <= consumed_until:
                return {
                    "status": "error",
                    "error_code": "recording_refresh_reclaim_denied",
                    "message": (
                        "recording_refresh_denied: runtime lease already claimed; "
                        "unlock again to re-establish owner"
                    ),
                }

        lease_id = secrets.token_urlsafe(24)
        self._recording_refresh_owner = {
            "uid": int(peer.uid),
            "pid": int(peer.pid),
            "writer_id": id(writer),
            "lease_id": lease_id,
        }
        if source == "runtime":
            self._runtime_refresh_claim_consumed_until[int(peer.uid)] = expires_at

        if source == "runtime":
            try:
                refresh_result = await self.client.send_command(
                    Command(
                        command=CommandType.REFRESH_RECORDING_UNLOCK,
                        data={
                            "uid": int(peer.uid),
                            "ttl": int(self._recording_refresh_ttl_s),
                        },
                    )
                )
            except Exception:
                self._recording_refresh_owner = None
                return {"status": "error", "message": "Daemon unavailable"}

            if refresh_result.status != "ok":
                self._recording_refresh_owner = None
                return {
                    "status": "error",
                    "error_code": "recording_refresh_denied",
                    "message": refresh_result.error
                    or "Failed to establish recording refresh lease",
                }

        unlock_status = await self._resolve_unlock_status_async(peer.uid)
        return {
            "status": "ok",
            "lease_id": lease_id,
            **self._serialize_recording_unlock_state(
                {
                    **unlock_status,
                    "source": source,
                    "expires_at": int(unlock_status.get("expires_at", expires_at) or expires_at),
                },
                refresh_owner=True,
            ),
        }

    async def _refresh_recording_unlock(
        self,
        peer: PeerCredentials,
        writer: asyncio.StreamWriter,
        lease_id: str,
    ) -> dict:
        if not lease_id:
            return {
                "status": "error",
                "error_code": "recording_refresh_denied",
                "message": "recording_refresh_denied: missing lease id",
            }

        owner = self._recording_refresh_owner
        if owner is None:
            return {
                "status": "error",
                "error_code": "recording_refresh_denied",
                "message": "recording_refresh_denied: no active refresh owner",
            }

        if (
            owner.get("uid") != int(peer.uid)
            or owner.get("pid") != int(peer.pid)
            or owner.get("writer_id") != id(writer)
            or owner.get("lease_id") != lease_id
        ):
            return {
                "status": "error",
                "error_code": "recording_refresh_owner_mismatch",
                "message": "recording_refresh_denied: caller is not active refresh owner",
            }

        try:
            result = await self.client.send_command(
                Command(
                    command=CommandType.REFRESH_RECORDING_UNLOCK,
                    data={
                        "uid": int(peer.uid),
                        "ttl": int(self._recording_refresh_ttl_s),
                    },
                )
            )
        except Exception:
            return {"status": "error", "message": "Daemon unavailable"}

        if result.status != "ok":
            return {
                "status": "error",
                "error_code": "recording_refresh_denied",
                "message": result.error or "Failed to refresh recording unlock",
            }

        unlock_status = await self._resolve_unlock_status_async(peer.uid)
        if str(unlock_status.get("source", "none") or "none") == "runtime":
            expires_at = int(unlock_status.get("expires_at", 0) or 0)
            consumed_until = int(
                self._runtime_refresh_claim_consumed_until.get(int(peer.uid), 0) or 0
            )
            if expires_at > consumed_until:
                self._runtime_refresh_claim_consumed_until[int(peer.uid)] = expires_at
        if not bool(unlock_status.get("unlocked", False)):
            self._recording_refresh_owner = None

        return {
            "status": "ok",
            **self._serialize_recording_unlock_state(
                unlock_status,
                refresh_owner=self._is_refresh_owner_request(peer, writer),
            ),
        }

    async def _lock_recording_unlock(
        self,
        peer: PeerCredentials,
        writer: asyncio.StreamWriter,
        lease_id: str,
    ) -> dict:
        if not lease_id:
            return {
                "status": "error",
                "error_code": "recording_lock_denied",
                "message": "recording_lock_denied: missing lease id",
            }

        owner = self._recording_refresh_owner
        if owner is None:
            return {
                "status": "error",
                "error_code": "recording_lock_denied",
                "message": "recording_lock_denied: no active refresh owner",
            }

        if (
            owner.get("uid") != int(peer.uid)
            or owner.get("pid") != int(peer.pid)
            or owner.get("writer_id") != id(writer)
            or owner.get("lease_id") != lease_id
        ):
            return {
                "status": "error",
                "error_code": "recording_lock_owner_mismatch",
                "message": "recording_lock_denied: caller is not active refresh owner",
            }

        try:
            result = await self.client.send_command(
                Command(
                    command=CommandType.LOCK_RECORDING_UNLOCK,
                    data={"uid": int(peer.uid)},
                )
            )
        except Exception:
            return {"status": "error", "message": "Daemon unavailable"}

        if result.status != "ok":
            return {
                "status": "error",
                "error_code": "recording_lock_denied",
                "message": result.error or "Failed to lock recording unlock",
            }

        self._recording_refresh_owner = None
        self._runtime_refresh_claim_consumed_until.pop(int(peer.uid), None)
        return {
            "status": "ok",
            **self._serialize_recording_unlock_state(
                {"unlocked": False, "source": "none", "expires_at": 0},
                refresh_owner=False,
            ),
        }

    async def _begin_capture(self, hardware_id: str) -> dict:
        self._capture_locks.add(hardware_id)

        current_profiles = list(
            self._resolved_devices.get(
                hardware_id, ResolvedDeviceProfile(hardware_id)
            ).active_profile_names
        )
        self._capture_resume_profiles[hardware_id] = current_profiles

        released = False
        if hardware_id in self._grabbed_devices:
            await self._deactivate_profile(hardware_id, immediate=True)
            released = True

        return {
            "status": "ok",
            "hardware_id": hardware_id,
            "released": released,
            "profiles": current_profiles,
        }

    async def _capture_begin(self, hardware_id: str) -> dict:
        lock_result = await self._begin_capture(hardware_id)
        try:
            result = await self.client.send_command(
                Command(command=CommandType.CAPTURE_BEGIN, data={"hardware_id": hardware_id})
            )
        except Exception:
            await self._end_capture(hardware_id)
            return {"status": "error", "message": "Daemon unavailable"}

        if result.status != "ok" or not isinstance(result.data, dict):
            await self._end_capture(hardware_id)
            return {"status": "error", "message": result.error or "Failed to begin capture"}

        token = str(result.data.get("token", ""))
        if not token:
            await self._end_capture(hardware_id)
            return {"status": "error", "message": "Missing capture token"}

        self._capture_tokens[hardware_id] = token
        response = {
            "status": "ok",
            "hardware_id": hardware_id,
            "token": token,
            "warnings": result.data.get("warnings", []),
        }
        response.update(lock_result)
        return response

    async def _capture_read(self, hardware_id: str) -> dict:
        token = self._capture_tokens.get(hardware_id, "")
        if not token:
            return {"status": "error", "message": "capture not active"}

        try:
            result = await self.client.send_command(
                Command(command=CommandType.CAPTURE_READ, data={"token": token})
            )
        except Exception:
            return {"status": "error", "message": "Daemon unavailable"}

        if result.status == "ok" and isinstance(result.data, dict):
            return {"status": "ok", "captured": result.data.get("captured")}
        return {"status": "error", "message": result.error or "Failed to read capture"}

    async def _capture_end(self, hardware_id: str) -> dict:
        token = self._capture_tokens.pop(hardware_id, "")
        if token:
            try:
                await self.client.send_command(
                    Command(command=CommandType.CAPTURE_END, data={"token": token})
                )
            except Exception:
                pass
        return await self._end_capture(hardware_id)

    async def _end_capture(self, hardware_id: str) -> dict:
        was_locked = hardware_id in self._capture_locks
        self._capture_locks.discard(hardware_id)

        previous_profile_names = self._capture_resume_profiles.pop(hardware_id, [])
        if not was_locked:
            return {"status": "ok", "hardware_id": hardware_id, "resumed": False}

        await self._reevaluate_profiles()
        active_names = list(
            self._resolved_devices.get(
                hardware_id, ResolvedDeviceProfile(hardware_id)
            ).active_profile_names
        )
        return {
            "status": "ok",
            "hardware_id": hardware_id,
            "resumed": bool(active_names),
            "profiles": active_names or previous_profile_names,
        }

    async def _capture_combo(self, profile_name: str, timeout_s: float) -> dict:
        profile = self.profiles.get_profile(profile_name)
        if profile is None:
            return {"status": "error", "message": f"Unknown profile '{profile_name}'"}

        hardware_ids = sorted(
            {
                *self.hardware.list_hardware_ids(),
                *profile.config.device_layers.keys(),
                *(
                    event.hardware_id
                    for combo in getattr(profile.config, "combos", [])
                    for step in combo.steps
                    for event in step.events
                    if event.hardware_id
                ),
            }
        )
        if not hardware_ids:
            return {
                "status": "error",
                "message": "No known devices available for combo capture",
            }

        try:
            result = await self.client.send_command(
                Command(
                    command=CommandType.CAPTURE_COMBO,
                    data={
                        "hardware_ids": hardware_ids,
                        "timeout_s": float(timeout_s),
                    },
                )
            )
        except Exception:
            return {"status": "error", "message": "Daemon unavailable"}

        if result.status != "ok" or not isinstance(result.data, dict):
            return {"status": "error", "message": result.error or "Combo capture failed"}

        events = result.data.get("events")
        if not isinstance(events, list):
            return {"status": "error", "message": "Combo capture returned no events"}

        return {
            "status": "ok",
            "events": [
                {
                    "evdev": str(event.get("evdev", "") or ""),
                    "hardware_id": str(event.get("hardware_id", "") or ""),
                    "source": str(event.get("source", "") or ""),
                }
                for event in events
                if isinstance(event, dict)
            ],
            "warnings": list(result.data.get("warnings", [])),
        }

    def _build_active_profiles_payload(self) -> dict:
        return {
            "status": "ok",
            "active_profiles": list(self._active_profile_names),
            "devices": {
                hardware_id: {
                    "profiles": list(resolved.active_profile_names),
                    "mapping_count": resolved.mapping_count,
                    "always_grab_all": resolved.always_grab_all,
                }
                for hardware_id, resolved in sorted(self._resolved_devices.items())
            },
            "window": self._current_window,
        }

    async def _get_active_window_payload(self) -> dict:
        if self._window_listener is not None:
            try:
                (
                    window_class,
                    window_title,
                    window_tags,
                ) = await self._window_listener.get_active_window()
                window_info = self._normalize_window_info(window_class, window_title, window_tags)
                if window_info["class"] or window_info["title"] or window_info["tags"]:
                    self._current_window = window_info
                    return {"status": "ok", **window_info}
            except Exception as e:
                log.debug(
                    "Active window query failed (compositor_id=%s listener=%s): %s",
                    self._compositor_id,
                    getattr(self._window_listener, "name", "unknown"),
                    e,
                )

        if self._current_window:
            return {"status": "ok", **self._normalize_window_info_from_dict(self._current_window)}

        return {
            "status": "error",
            "message": "Active window is unavailable on this compositor",
        }

    def _normalize_window_info(
        self,
        window_class: str,
        window_title: str,
        window_tags: list[str],
    ) -> dict[str, str | list[str]]:
        return {
            "class": str(window_class or ""),
            "title": str(window_title or ""),
            "tags": [str(tag) for tag in window_tags if str(tag or "").strip()],
        }

    def _normalize_window_info_from_dict(self, window_info: dict) -> dict[str, str | list[str]]:
        return self._normalize_window_info(
            str(window_info.get("class", "") or ""),
            str(window_info.get("title", "") or ""),
            [str(tag) for tag in list(window_info.get("tags", []) or []) if str(tag or "").strip()],
        )

    def _broadcast_profiles_changed(self) -> None:
        message = {"event": "profiles_changed", **self._build_active_profiles_payload()}
        self._broadcast_to_session_clients(message)

    def _broadcast_keyforged_status(self, connected: bool) -> None:
        message = {
            "event": "keyforged_status",
            "connected": connected,
        }
        self._broadcast_to_session_clients(message)

    def _device_name_for_hardware(self, hardware_id: str) -> str:
        hardware = self.hardware.get_hardware(hardware_id)
        if hardware is None:
            return hardware_id
        return str(getattr(hardware, "name", "") or hardware_id)

    def _cancel_grab_retry(self, hardware_id: str) -> None:
        task = self._grab_retry_tasks.pop(hardware_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _schedule_grab_retry(self, hardware_id: str, delay_s: float = GRAB_RETRY_DELAY_S) -> None:
        if not hardware_id:
            return
        existing = self._grab_retry_tasks.get(hardware_id)
        if existing is not None and not existing.done():
            return

        async def _retry() -> None:
            try:
                await asyncio.sleep(delay_s)
                await self._reevaluate_profiles()
            except asyncio.CancelledError:
                pass
            finally:
                task = self._grab_retry_tasks.get(hardware_id)
                if task is asyncio.current_task():
                    self._grab_retry_tasks.pop(hardware_id, None)

        self._grab_retry_tasks[hardware_id] = asyncio.create_task(_retry())

    def _handle_device_grab_status_event(self, data: dict) -> None:
        hardware_id = str(data.get("hardware_id", "") or "")
        state = str(data.get("state", "") or "").strip().lower()
        active_keys = [str(key) for key in list(data.get("active_keys", []) or []) if str(key)]
        summary = ", ".join(active_keys) if active_keys else "unknown keys"

        self._broadcast_to_session_clients({"event": "device_grab_status", **data})

        if not hardware_id:
            return

        device_name = self._device_name_for_hardware(hardware_id)
        if state == "waiting":
            if hardware_id in self._grab_waiting_devices:
                return
            self._grab_waiting_devices.add(hardware_id)
            self._send_notification(
                "Keyforge: Grab Pending",
                f"{device_name}: waiting for keys to be released ({summary}).",
            )
            return

        if state == "ready":
            self._grab_waiting_devices.discard(hardware_id)
            return

        if state == "timed_out":
            self._grab_waiting_devices.discard(hardware_id)
            self._send_notification(
                "Keyforge: Grab Timed Out",
                f"{device_name}: keys stayed down too long ({summary}). Retrying automatically.",
            )
            self._schedule_grab_retry(hardware_id)

    def _broadcast_to_session_clients(self, message: dict) -> None:
        for writer in list(self._session_clients):
            try:
                writer.write(json.dumps(message).encode() + b"\n")
                asyncio.create_task(self._drain_session_writer(writer))
            except Exception:
                self._session_clients.discard(writer)

    async def _drain_session_writer(self, writer: asyncio.StreamWriter) -> None:
        try:
            await writer.drain()
        except Exception:
            self._session_clients.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _signal_handler(self) -> None:
        log.info("Received shutdown signal")
        self._shutdown_event.set()
        self._retry_event.set()

    async def _wait_for_session_clients_to_close(self, timeout_s: float = 1.0) -> None:
        if not self._session_clients:
            return

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.05, float(timeout_s))
        while self._session_clients and loop.time() < deadline:
            await asyncio.sleep(0.01)

        if self._session_clients:
            log.debug(
                "Timed out waiting for %s session client(s) to close",
                len(self._session_clients),
            )

    def _reload_handler(self) -> None:
        if self._reload_pending:
            log.debug("Reload already pending, skipping")
            return

        self._reload_pending = True

        if self._reload_task and not self._reload_task.done():
            log.debug("Reload task still running, will retry after")
            return

        log.info("Received reload signal (SIGHUP)")
        self._reload_task = asyncio.create_task(self._reload_profiles())

    async def _reload_profiles(self) -> None:
        await asyncio.sleep(0.05)

        self._reload_pending = False

        await asyncio.to_thread(self._reload_config_from_disk)
        log.info("Reloaded all superkeys, profiles and hardware configs")

        configured_ids = set(self.hardware.list_hardware_ids())
        stale_ids = [hw_id for hw_id in list(self._grabbed_devices) if hw_id not in configured_ids]
        for hardware_id in stale_ids:
            log.info(f"Hardware removed for {hardware_id}, deactivating profile")
            await self._deactivate_profile(hardware_id)
            self._resolved_devices.pop(hardware_id, None)

        await self._reevaluate_profiles()

    def _reload_config_from_disk(self) -> None:
        self._security_policy = load_security_policy(SECURITY_POLICY_PATH)
        self.superkeys.reload()
        self.profiles._load_all()
        self.hardware._load_all()

    async def _connect_loop(self) -> None:
        retry_delay = 1.0
        max_delay = 30.0

        while self.running:
            try:
                log.info(f"Connecting to keyforged at {SOCKET_PATH}")
                await self.client.connect()
                self._connected = True
                retry_delay = 1.0
                log.info("Connected to keyforged")
                self._broadcast_keyforged_status(True)

                try:
                    await self._activate_initial_profiles()
                except Exception as e:
                    log.error(f"Failed to activate initial profiles: {e}")
                    traceback.print_exc()

                await self.client.wait_disconnected()

            except Exception as e:
                log.warning(f"Connection failed: {e}")
                was_connected = self._connected
                self._connected = False
                self._grabbed_devices.clear()
                self._grabbed_interfaces.clear()
                self._grab_waiting_devices.clear()
                for task in list(self._grab_retry_tasks.values()):
                    if not task.done():
                        task.cancel()
                self._grab_retry_tasks.clear()
                self._last_sent_mapping_signatures.clear()
                self._last_sent_combo_signature = ""
                self._active_profile_names.clear()
                self._resolved_devices.clear()

                if was_connected:
                    self._broadcast_keyforged_status(False)

                if self.running:
                    try:
                        await asyncio.wait_for(self._retry_event.wait(), timeout=retry_delay)
                    except TimeoutError:
                        pass
                    retry_delay = min(retry_delay * 2, max_delay)

    async def _compositor_supervisor_loop(self) -> None:
        while self.running:
            try:
                await self._ensure_compositor_listener()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug(f"Compositor supervisor error: {e}")

            stable = (
                self._window_listener is not None
                and self._compositor_id is not None
                and self._compositor_candidate == self._compositor_id
                and self._compositor_candidate_hits >= 2
            )
            await asyncio.sleep(
                self._compositor_probe_slow_s if stable else self._compositor_probe_fast_s
            )

    async def _ensure_compositor_listener(self) -> None:
        detected = await detect_compositor(self.dbus)

        if detected == self._compositor_candidate:
            self._compositor_candidate_hits += 1
        else:
            self._compositor_candidate = detected
            self._compositor_candidate_hits = 1

        current_healthy = False
        if self._window_listener is not None:
            with contextlib.suppress(Exception):
                current_healthy = await self._window_listener.health_check()

        if self._window_listener is not None and not current_healthy:
            log.warning("Window listener became unhealthy, restarting compositor binding")
            await self._stop_window_listener()
            self._compositor_id = None

        if self._compositor_candidate_hits < 2:
            return

        target = self._compositor_candidate
        if target == self._compositor_id and self._window_listener is not None:
            return

        await self._switch_compositor(target)

    async def _switch_compositor(self, compositor_id: str | None) -> None:
        if compositor_id == self._compositor_id and self._window_listener is not None:
            return

        if compositor_id and compositor_id == self._compositor_id and self._window_listener is None:
            if not self._listener_retry_ready(compositor_id):
                return

        previous = self._compositor_id
        await self._stop_window_listener()

        self._compositor_id = compositor_id
        self._compositor_capabilities = get_compositor_capabilities(self._compositor_id)

        if compositor_id is None:
            if previous is not None:
                log.info("Compositor transitioned %s -> none (headless mode)", previous)
            return

        compositor_name = get_compositor_name(compositor_id)
        support_details: dict[str, bool | str] = {}
        if compositor_id == "gnome":
            support_details = await get_compositor_support_details(compositor_id, self.dbus)
            supported = bool(support_details.get("supported", False))
        else:
            supported = await is_compositor_supported(compositor_id, self.dbus)
        if not supported:
            self._note_listener_failure(
                compositor_id,
                str(
                    support_details.get("warning", "")
                    or f"listener support unavailable for {compositor_name}"
                ),
            )
            return

        await self._start_window_listener()
        if self._window_listener is None:
            self._note_listener_failure(compositor_id, self._last_listener_start_error)
            return

        self._listener_retry_after.pop(compositor_id, None)
        self._listener_last_error.pop(compositor_id, None)
        self._listener_last_log_at.pop(compositor_id, None)

        if previous != compositor_id:
            log.info("Compositor transitioned %s -> %s", previous or "none", compositor_id)
        else:
            log.info("Compositor listener restarted for %s", compositor_id)

    async def _stop_window_listener(self) -> None:
        if self._window_listener is None:
            return
        try:
            await self._window_listener.stop()
        except Exception as e:
            log.debug(f"Error stopping window listener: {e}")
        self._window_listener = None

    def _listener_retry_ready(self, compositor_id: str) -> bool:
        now = asyncio.get_running_loop().time()
        return now >= float(self._listener_retry_after.get(compositor_id, 0.0))

    def _note_listener_failure(self, compositor_id: str, error: str) -> None:
        now = asyncio.get_running_loop().time()
        error_text = (error or "listener startup failed").strip()

        previous_error = self._listener_last_error.get(compositor_id)
        last_log_at = float(self._listener_last_log_at.get(compositor_id, 0.0))
        should_log = (
            previous_error != error_text or (now - last_log_at) >= self._listener_log_interval_s
        )

        if should_log:
            log.warning(
                "No compatible window listener environment detected for '%s': %s. "
                "Window tracking is disabled until environment changes.",
                compositor_id,
                error_text,
            )
            self._listener_last_log_at[compositor_id] = now

        self._listener_last_error[compositor_id] = error_text
        self._listener_retry_after[compositor_id] = now + self._listener_retry_interval_s

    async def _start_window_listener(self) -> None:
        listener_class = get_listener_class(self._compositor_id)
        log.debug(f"Window listener class for {self._compositor_id}: {listener_class}")
        if not listener_class:
            log.debug(f"No window listener available for compositor: {self._compositor_id}")
            self._last_listener_start_error = "no listener class available"
            return

        try:
            self._window_listener = listener_class(self.on_window_change, self.client, self.dbus)
            log.debug(f"Window listener instance created: {self._window_listener}")
            await self._window_listener.start()
            log.info(f"Started {self._window_listener.name} window listener")
            self._last_listener_start_error = ""
        except NotImplementedError as e:
            self._last_listener_start_error = str(e)
            self._window_listener = None
        except Exception as e:
            self._last_listener_start_error = str(e)
            log.debug(f"Failed to start window listener: {e}")
            self._window_listener = None

    async def _handle_event(self, event_type: CommandType, data: dict) -> None:
        if self.verbosity >= 1:
            log.debug(f"Event: {event_type.value} -> {self._event_log_view(data)}")

        if event_type == CommandType.ACTION_TRIGGER:
            exec_ref = data.get("exec_ref")
            if exec_ref is not None:
                if exec_ref >= 10000:
                    ref_data = self._superkey_exec_refs.get(exec_ref)
                    if ref_data:
                        hardware_id, cmd = ref_data
                        data["cmd"] = cmd
                        data["hardware_id"] = hardware_id
                        await self.action_handler.handle_action(data)
                    else:
                        log.warning(f"Unknown superkey exec_ref: {exec_ref}")
                else:
                    cmd = self._exec_refs.get(exec_ref)
                    if cmd:
                        data["cmd"] = cmd
                        await self.action_handler.handle_action(data)
                    else:
                        log.warning(f"Unknown exec_ref: {exec_ref}")
            action_type_str = data.get("action_type", "")
            if action_type_str == "start_macro_recording":
                asyncio.create_task(self._handle_start_macro_trigger())
            elif action_type_str == "stop_macro_recording":
                asyncio.create_task(self._handle_stop_macro_trigger())
            elif action_type_str == "cancel_macro_playback":
                asyncio.create_task(self._handle_cancel_macro_trigger())
            elif action_type_str in {"profile_enable", "profile_disable", "profile_toggle"}:
                asyncio.create_task(self._handle_profile_trigger(data))
            elif action_type_str == "exec" and exec_ref is None:
                asyncio.create_task(self._handle_exec_trigger(data))
            elif action_type_str in {"compositor_dispatch", "hyprland_dispatch"}:
                asyncio.create_task(self._handle_compositor_dispatch_trigger(data))
            elif action_type_str == "macro":
                macro_name = str(data.get("macro_name", "")).strip()
                if macro_name:
                    asyncio.create_task(self._play_macro_by_name(macro_name))
        elif event_type == CommandType.DEVICE_CONNECTED:
            log.info(f"Device connected: {data}")
            await self._on_device_connected(data)
        elif event_type == CommandType.DEVICE_DISCONNECTED:
            log.info(f"Device disconnected: {data}")
        elif event_type == CommandType.DEVICE_GRAB_STATUS:
            self._handle_device_grab_status_event(data)
        elif event_type == CommandType.RECORDING_STARTED:
            self._recording_active = True
            self._broadcast_to_session_clients({"event": "recording_started", **data})
        elif event_type == CommandType.RECORDING_STOPPED:
            self._recording_active = False
            recording_data = dict(data)
            if self._recording_start_cursor:
                recording_data["start_x"] = int(self._recording_start_cursor[0])
                recording_data["start_y"] = int(self._recording_start_cursor[1])
                recording_data["move_to_start"] = True
            self._pending_recording_data = recording_data
            self._recording_start_cursor = None
            self._broadcast_to_session_clients(
                {
                    "event": "recording_stopped",
                    "duration_ms": recording_data.get("duration_ms", 0),
                    "event_count": len(recording_data.get("events", [])),
                    "device_types": recording_data.get("device_types", []),
                    "start_x": recording_data.get("start_x"),
                    "start_y": recording_data.get("start_y"),
                    "move_to_start": recording_data.get("move_to_start", False),
                }
            )
        elif event_type == CommandType.RECORDING_PROGRESS:
            self._broadcast_to_session_clients({"event": "recording_progress", **data})
    async def _handle_start_macro_trigger(self) -> None:
        if self._recording_active:
            await self._handle_stop_macro_trigger()
            return

        if not self._has_active_gui_recording_owner():
            log.info("Ignored start_macro_recording trigger: no active GUI recording owner")
            self._send_notification(
                "Keyforge: Recording Unavailable",
                "Macro recording from triggers requires Keyforge GUI to be open.",
            )
            self._broadcast_to_session_clients({"event": "recording_auth_requested"})
            return

        result = await self._start_recording(reset_if_active=False)
        if result.get("status") != "ok":
            self._notify_recording_unlock_required(result)
            self._broadcast_to_session_clients({"event": "recording_auth_requested"})

    async def _handle_stop_macro_trigger(self) -> None:
        if not self._recording_active:
            return
        try:
            result = await self.client.send_command(Command(command=CommandType.STOP_RECORDING))
            if result.status == "ok" and isinstance(result.data, dict):
                recording_data = dict(result.data)
                if self._recording_start_cursor:
                    recording_data["start_x"] = int(self._recording_start_cursor[0])
                    recording_data["start_y"] = int(self._recording_start_cursor[1])
                    recording_data["move_to_start"] = True
                self._pending_recording_data = recording_data
            self._recording_active = False
            self._recording_start_cursor = None
        except Exception:
            pass

    async def _handle_cancel_macro_trigger(self) -> None:
        try:
            await self.client.send_command(Command(command=CommandType.CANCEL_MACRO_PLAYBACK))
        except Exception:
            pass

    async def _handle_profile_trigger(self, data: dict) -> None:
        action_type = str(data.get("action_type", "") or "").strip().lower()
        profile_name = str(data.get("profile_name", "") or "").strip()
        if not profile_name:
            return

        enabled: bool | None
        if action_type == "profile_enable":
            enabled = True
        elif action_type == "profile_disable":
            enabled = False
        else:
            enabled = None

        result = await self._set_profile_enabled(profile_name, enabled)
        if result.get("status") != "ok":
            log.warning(
                "Profile trigger failed action=%s profile=%s message=%s",
                action_type,
                profile_name,
                result.get("message", "unknown error"),
            )

    async def _handle_exec_trigger(self, data: dict) -> None:
        cmd = str(data.get("cmd", "") or "").strip()
        if not cmd:
            return

        wait_id = str(data.get("macro_exec_wait_id", "") or "").strip()
        is_async = bool(data.get("macro_exec_async", False))

        if wait_id:
            returncode = await self.action_handler.execute_command(cmd)
            try:
                await self.client.send_command(
                    Command(
                        command=CommandType.MACRO_EXEC_COMPLETE,
                        data={"wait_id": wait_id, "returncode": int(returncode)},
                    )
                )
            except Exception:
                pass
            return

        if is_async:
            self.action_handler.execute_command_sync(cmd)
            return

        await self.action_handler.execute_command(cmd)

    async def _handle_compositor_dispatch_trigger(self, data: dict) -> None:
        dispatcher = str(data.get("dispatcher", "") or "").strip()
        args = str(data.get("args", "") or "").strip()
        if not dispatcher:
            return

        listener = self._window_listener
        if listener is None:
            log.warning(
                (
                    "Ignored compositor dispatch trigger while listener inactive: "
                    "dispatcher=%s compositor=%s"
                ),
                dispatcher,
                self._compositor_id or "none",
            )
            return

        ok, message = await listener.dispatch(dispatcher, args)
        if not ok:
            log.warning(
                "Compositor dispatch failed: dispatcher=%s args=%s message=%s",
                dispatcher,
                args,
                message,
            )

    async def _activate_initial_profiles(self) -> None:
        hardware_ids = self.hardware.list_hardware_ids()
        log.info(f"Found {len(hardware_ids)} hardware config(s): {hardware_ids}")
        await self._reevaluate_profiles()

    async def _set_profile_enabled(
        self,
        profile_name: str,
        enabled: bool | None,
    ) -> dict:
        profile = await asyncio.to_thread(
            self.profiles.set_profile_enabled,
            profile_name,
            enabled,
        )
        if profile is None:
            self._send_notification(
                "Keyforge: Profile Not Found",
                f"Profile '{profile_name}' was not found.",
            )
            return {
                "status": "error",
                "message": f"Profile '{profile_name}' not found",
            }

        await self._reevaluate_profiles()
        return {
            "status": "ok",
            "profile_name": profile.name,
            "enabled": profile.enabled,
            "active_profiles": list(self._active_profile_names),
        }

    def _build_profile_overview(self) -> dict:
        known_hardware_ids = set(self.hardware.list_hardware_ids())
        for info in self.profiles.list_profiles():
            known_hardware_ids.update(info.config.device_layers.keys())

        profiles = sorted(
            self.profiles.list_profiles(),
            key=lambda p: (p.config.name.casefold(), p.config.created_at or datetime.min),
        )
        devices: list[dict] = []
        for hardware_id in sorted(known_hardware_ids):
            hardware = self.hardware.get_hardware(hardware_id)
            resolved = self._resolved_devices.get(hardware_id, ResolvedDeviceProfile(hardware_id))
            devices.append(
                {
                    "hardware_id": hardware_id,
                    "device_name": hardware.name if hardware else hardware_id,
                    "active_profiles": list(resolved.active_profile_names),
                    "mapping_count": resolved.mapping_count,
                    "always_grab_all": resolved.always_grab_all,
                    "profile_count": sum(
                        1 for info in profiles if hardware_id in info.config.device_layers
                    ),
                }
            )

        return {
            "status": "ok",
            "profiles": [
                {
                    "name": info.config.name,
                    "enabled": info.config.enabled,
                    "is_permanent": info.config.is_permanent,
                    "priority": info.config.priority,
                    "window_rule_count": len(info.config.window_rules),
                    "created_at": (
                        info.config.created_at.isoformat() if info.config.created_at else ""
                    ),
                    "devices": sorted(info.config.device_layers.keys()),
                    "active": info.config.name in self._active_profile_names,
                }
                for info in profiles
            ],
            "devices": devices,
        }

    async def _reevaluate_profiles(self) -> None:
        hardware_ids = self.hardware.list_hardware_ids()
        resolved = self.profiles.resolve_active_profiles(
            self._current_window,
            self._compositor_capabilities,
            hardware_ids=hardware_ids,
        )
        self._active_profile_names = [profile.name for profile in resolved.active_profiles]

        for hardware_id in hardware_ids:
            if hardware_id in self._capture_locks:
                log.info("Reevaluate skipped for %s: capture lock active", hardware_id)
                continue
            device_resolution = resolved.devices.get(
                hardware_id, ResolvedDeviceProfile(hardware_id)
            )
            await self._apply_resolved_device_profile(hardware_id, device_resolution)

        stale_ids = [
            hw_id for hw_id in list(self._resolved_devices) if hw_id not in set(hardware_ids)
        ]
        for hardware_id in stale_ids:
            self._resolved_devices.pop(hardware_id, None)
            self._last_sent_mapping_signatures.pop(hardware_id, None)

        await self._update_combos(resolved.combos)
        self._broadcast_profiles_changed()

    async def _apply_resolved_device_profile(
        self, hardware_id: str, resolved: ResolvedDeviceProfile
    ) -> None:
        if hardware_id in self._capture_locks:
            log.debug(f"Skipping activation for {hardware_id} while capture is active")
            return

        hardware_config = self.hardware.get_hardware(hardware_id)
        if not hardware_config:
            log.warning(f"No hardware config for {hardware_id}")
            return

        old_resolved = self._resolved_devices.get(hardware_id)
        old_profile_names = old_resolved.active_profile_names if old_resolved else []
        self._resolved_devices[hardware_id] = resolved

        if not resolved.has_effective_mapping:
            self._cancel_grab_retry(hardware_id)
            self._grab_waiting_devices.discard(hardware_id)
            if hardware_id in self._grabbed_devices:
                await self._deactivate_profile(hardware_id, immediate=True)
            return

        new_interfaces = self._get_interfaces_to_grab(hardware_config, resolved)
        current_interfaces = self._grabbed_interfaces.get(hardware_id, {})

        if hardware_id in self._grabbed_devices:
            if set(current_interfaces.keys()) == set(new_interfaces.keys()):
                mapping_update_needed = self._mapping_update_needed(hardware_id, resolved)
                if not mapping_update_needed:
                    log.debug("Skipping unchanged mapping for %s", hardware_id)
                    self._maybe_notify_profile_activation(
                        hardware_config.name,
                        old_profile_names,
                        resolved,
                    )
                    return
                if old_profile_names == resolved.active_profile_names and self.verbosity >= 1:
                    log.debug(
                        "Resolved profile set already active for %s, updating mapping only",
                        hardware_id,
                    )
                elif old_profile_names != resolved.active_profile_names:
                    log.info(
                        "Same interfaces for %s, updating mapping only (old=%s new=%s)",
                        hardware_id,
                        old_profile_names,
                        resolved.active_profile_names,
                    )
                updated = await self._update_mapping(hardware_id, resolved)
                if updated:
                    self._maybe_notify_profile_activation(
                        hardware_config.name,
                        old_profile_names,
                        resolved,
                    )
                    return
                log.warning(
                    "Mapping update failed for %s with same interfaces; forcing re-grab",
                    hardware_id,
                )
                await self._deactivate_profile(hardware_id)

            log.info(
                f"Interfaces changed for {hardware_id}, reconfiguring in keyforged "
                f"(old: {list(current_interfaces.keys())} -> new: {list(new_interfaces.keys())})"
            )

        log.info(f"Grabbing device {hardware_id} (interfaces: {list(new_interfaces.keys())})")
        try:
            result = await self.client.send_command(
                Command(
                    command=CommandType.GRAB_DEVICE,
                    data={
                        "hardware_id": hardware_id,
                        "evdev_paths": list(new_interfaces.values()),
                        "button_map": {b.id: b.evdev for b in hardware_config.buttons},
                        "button_sources": {
                            b.id: b.source for b in hardware_config.buttons if b.source
                        },
                        "force_grab_unmapped": bool(resolved.combo_event_count),
                    },
                ),
                timeout=GRAB_DEVICE_TIMEOUT_S,
            )
            if result.status == "ok":
                self._cancel_grab_retry(hardware_id)
                self._grab_waiting_devices.discard(hardware_id)
                self._grabbed_devices.add(hardware_id)
                self._grabbed_interfaces[hardware_id] = new_interfaces
                log.info(f"keyforged: Grabbed device {hardware_id}: {result.data}")
                if (
                    isinstance(result.data, dict)
                    and int(result.data.get("grabbed_count", 0) or 0) == 0
                ):
                    log.warning(
                        (
                            "keyforged grab returned zero interfaces for %s "
                            "(requested=%s, mappings=%d)"
                        ),
                        hardware_id,
                        list(new_interfaces.keys()),
                        len(resolved.mappings),
                    )
            else:
                log.error(f"keyforged: Failed to grab device {hardware_id}: {result.error}")
                if "timed out waiting" in str(result.error or "").lower():
                    self._schedule_grab_retry(hardware_id)
                return
        except Exception as e:
            log.error(
                f"keyforged: Exception grabbing device {hardware_id}: {type(e).__name__}: {e}"
            )
            traceback.print_exc()
            if isinstance(e, TimeoutError):
                self._send_notification(
                    "Keyforge: Grab Timed Out",
                    (
                        f"{self._device_name_for_hardware(hardware_id)}: grab timed out while "
                        "waiting for keys to be released. Retrying automatically."
                    ),
                )
                self._schedule_grab_retry(hardware_id)
            return

        log.info(
            "Setting mapping for %s with %d buttons from profiles=%s",
            hardware_id,
            len(resolved.mappings),
            resolved.active_profile_names,
        )
        try:
            mapping = self._profile_to_mapping(resolved, hardware_id)
            log.debug(f"Mapping data: {self._mapping_log_view(mapping)}")

            result = await self.client.send_command(
                Command(
                    command=CommandType.SET_MAPPING,
                    data={
                        "hardware_id": hardware_id,
                        "mapping": mapping,
                    },
                )
            )

            if result.status == "ok":
                self._last_sent_mapping_signatures[hardware_id] = (
                    self._resolved_mapping_signature(resolved, hardware_id)
                )
                log.info(
                    "Activated resolved profiles %s for %s",
                    resolved.active_profile_names,
                    hardware_id,
                )
                self._maybe_notify_profile_activation(
                    hardware_config.name,
                    old_profile_names,
                    resolved,
                )
            else:
                log.error(f"Failed to set mapping: {result.error}")

        except Exception as e:
            log.error(f"Exception setting mapping: {type(e).__name__}: {e}")
            traceback.print_exc()

    def _get_interfaces_to_grab(
        self, hardware_config, resolved: ResolvedDeviceProfile
    ) -> dict[str, str]:
        from keyforge.common.models import ActionType

        interface_to_path = {}
        for dev in hardware_config.evdev_devices:
            if dev.id:
                interface_to_path[dev.id] = dev.path

        if resolved.always_grab_all:
            return interface_to_path

        button_to_source = {b.id: b.source for b in hardware_config.buttons if b.source}

        sources_to_grab = set()

        for button_id, action in resolved.mappings.items():
            if action.action_type != ActionType.PASSTHROUGH:
                source = button_to_source.get(button_id)
                if source:
                    sources_to_grab.add(source)

        if resolved.combo_event_count:
            if resolved.combo_sources:
                sources_to_grab.update(resolved.combo_sources)
            else:
                return interface_to_path

        log.info(
            (
                "Interface selection for %s profile=%s: total_ifaces=%d "
                "mapped_buttons=%d resolved_sources=%d"
            ),
            hardware_config.hardware_id,
            resolved.active_profile_names,
            len(interface_to_path),
            len(resolved.mappings),
            len(sources_to_grab),
        )

        return {
            source: interface_to_path[source]
            for source in sources_to_grab
            if source in interface_to_path
        }

    async def _update_combos(self, combos: list[ResolvedCombo]) -> None:
        signature = self._resolved_combos_signature(combos)
        if signature == self._last_sent_combo_signature:
            log.debug("Skipping unchanged combo payload")
            return
        self._clear_combo_exec_refs()
        payload = self._resolved_combos_payload(combos)
        try:
            result = await self.client.send_command(
                Command(
                    command=CommandType.SET_COMBOS,
                    data={"combos": payload},
                )
            )
            if result.status != "ok":
                log.error("Failed to update combos: %s", result.error)
                return
            self._last_sent_combo_signature = signature
        except Exception as e:
            log.error("Exception updating combos: %s: %s", type(e).__name__, e)

    async def _update_mapping(self, hardware_id: str, resolved: ResolvedDeviceProfile) -> bool:
        if hardware_id not in self._grabbed_devices:
            return False

        signature = self._resolved_mapping_signature(resolved, hardware_id)
        self._clear_exec_refs(hardware_id)

        log.info(f"Updating mapping for {hardware_id} with {len(resolved.mappings)} buttons")
        try:
            mapping = self._profile_to_mapping(resolved, hardware_id)
            result = await self.client.send_command(
                Command(
                    command=CommandType.SET_MAPPING,
                    data={
                        "hardware_id": hardware_id,
                        "mapping": mapping,
                    },
                )
            )
            if result.status == "ok":
                log.info(f"Updated mapping for {hardware_id}")
                self._last_sent_mapping_signatures[hardware_id] = signature
                return True
            else:
                log.error(f"Failed to update mapping: {result.error}")
                return False
        except Exception as e:
            log.error(f"Exception updating mapping: {type(e).__name__}: {e}")
            return False

    async def _deactivate_profile(self, hardware_id: str, immediate: bool = False) -> None:
        self._cancel_grab_retry(hardware_id)
        self._grab_waiting_devices.discard(hardware_id)
        if hardware_id not in self._grabbed_devices:
            return

        try:
            result = await self.client.send_command(
                Command(
                    command=CommandType.RELEASE_DEVICE,
                    data={"hardware_id": hardware_id, "immediate": bool(immediate)},
                )
            )
            if result.status != "ok":
                log.error(f"Failed to release device {hardware_id}: {result.error}")
                return
            self._grabbed_devices.discard(hardware_id)
            self._grabbed_interfaces.pop(hardware_id, None)
            self._last_sent_mapping_signatures.pop(hardware_id, None)
        except Exception as e:
            log.error(f"Failed to release device {hardware_id}: {e}")

        self._clear_exec_refs(hardware_id)
        log.info("Deactivated grabbed mapping for %s", hardware_id)

    def _clear_exec_refs(self, hardware_id: str) -> None:
        refs = self._device_exec_refs.pop(hardware_id, set())
        for ref in refs:
            self._exec_refs.pop(ref, None)
        if refs:
            log.debug(f"Cleared {len(refs)} exec refs for {hardware_id}")

        superkey_refs_to_clear = [
            ref for ref, (hid, _) in list(self._superkey_exec_refs.items()) if hid == hardware_id
        ]
        for ref in superkey_refs_to_clear:
            self._superkey_exec_refs.pop(ref, None)
        if superkey_refs_to_clear:
            log.debug(f"Cleared {len(superkey_refs_to_clear)} superkey exec refs for {hardware_id}")

    def _clear_combo_exec_refs(self) -> None:
        refs = list(self._combo_exec_refs)
        self._combo_exec_refs.clear()
        for ref in refs:
            self._exec_refs.pop(ref, None)
        if refs:
            log.debug("Cleared %d combo exec refs", len(refs))

    def _mapping_update_needed(self, hardware_id: str, resolved: ResolvedDeviceProfile) -> bool:
        return (
            self._last_sent_mapping_signatures.get(hardware_id, "")
            != self._resolved_mapping_signature(resolved, hardware_id)
        )

    def _resolved_mapping_signature(self, resolved: ResolvedDeviceProfile, hardware_id: str) -> str:
        mapping: dict[str, dict[str, object]] = {}
        for button_id in sorted(resolved.mappings):
            mapping[button_id] = self._action_signature_payload(
                resolved.mappings[button_id],
                hardware_id,
            )
        return json.dumps(mapping, sort_keys=True, separators=(",", ":"))

    def _resolved_combos_signature(self, combos: list[ResolvedCombo]) -> str:
        payload: list[dict[str, object]] = []
        for combo in combos:
            if combo.action is None:
                continue
            action_data = self._combo_action_signature_payload(combo.action)
            if action_data is None:
                continue
            steps: list[dict[str, object]] = []
            for step in combo.steps:
                events = [
                    {
                        "hardware_id": str(event.hardware_id or ""),
                        "source": str(event.source or ""),
                        "evdev": str(event.evdev or ""),
                    }
                    for event in step.events
                    if event.hardware_id and event.evdev
                ]
                if not events:
                    continue
                events.sort(
                    key=lambda event: (
                        str(event["hardware_id"]),
                        str(event["source"]),
                        str(event["evdev"]),
                    )
                )
                step_payload: dict[str, object] = {"events": events}
                if step.timeout_ms is not None:
                    step_payload["timeout_ms"] = int(step.timeout_ms)
                steps.append(step_payload)
            if not steps:
                continue
            payload.append(
                {
                    "id": combo.id,
                    "name": combo.name,
                    "profile_name": combo.profile_name,
                    "steps": steps,
                    "action": action_data,
                }
            )
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def _action_signature_payload(
        self,
        action: MappingAction,
        hardware_id: str,
    ) -> dict[str, object]:
        action_type = action.action_type.value
        data: dict[str, object] = {"action": action_type}

        if action_type in (
            "keyboard",
            "mouse",
            "gamepad",
            "mouse_move_rel",
            "mouse_move_abs",
        ):
            data["target"] = action.target or ""
            if action_type in ("mouse_move_rel", "mouse_move_abs"):
                data["x"] = int(action.move_x)
                data["y"] = int(action.move_y)
            if action.rapidfire_enabled:
                data["rapidfire_enabled"] = True
                data["rapidfire_hold_ms"] = int(action.rapidfire_hold_ms)
                data["rapidfire_wait_ms"] = int(action.rapidfire_wait_ms)
            if action.tap_enabled:
                data["tap_enabled"] = True
                data["tap_hold_ms"] = int(action.tap_hold_ms)
            return data

        if action_type == "exec":
            data["cmd"] = action.cmd or ""
            return data

        if action_type == "compositor_dispatch":
            data["dispatcher"] = action.compositor_dispatcher or ""
            data["args"] = action.compositor_args or ""
            return data

        if action_type in (
            "start_macro_recording",
            "stop_macro_recording",
            "cancel_macro_playback",
        ):
            return data

        if action_type in (
            "profile_enable",
            "profile_disable",
            "profile_toggle",
        ):
            data["profile_name"] = action.profile_name or action.target or ""
            return data

        if action_type == "macro":
            data["macro_name"] = action.macro_name or ""
            data["macro_replay_mouse_movement"] = bool(action.macro_replay_mouse_movement)
            data["macro_replay_mouse_clicks"] = bool(action.macro_replay_mouse_clicks)
            data["macro_speed"] = float(action.macro_speed)
            data["macro_loop_mode"] = action.macro_loop_mode
            data["macro_loop_count"] = int(action.macro_loop_count)
            return data

        if action_type == "superkey":
            if action.superkey_name:
                superkey_config = self.superkeys.get_superkey(action.superkey_name)
                if superkey_config:
                    data["superkey"] = self._serialize_superkey_signature(
                        superkey_config,
                        hardware_id,
                    )
            return data

        return data

    def _combo_action_signature_payload(self, action: MappingAction) -> dict[str, object] | None:
        data = self._action_signature_payload(action, "")
        if data.get("action") == "superkey":
            return None
        if data.get("action") == "exec" and not str(data.get("cmd", "") or ""):
            return None
        if data.get("action") == "compositor_dispatch" and not str(
            data.get("dispatcher", "") or ""
        ):
            return None
        if data.get("action") == "macro" and not str(data.get("macro_name", "") or ""):
            return None
        return data

    async def _on_device_connected(self, device_info: dict) -> None:
        hardware_id = f"{device_info.get('vendor_id', '')}:{device_info.get('product_id', '')}"
        if not hardware_id or ":" not in hardware_id:
            return

        hardware_config = self.hardware.get_hardware(hardware_id)
        if not hardware_config:
            return

        await self._reevaluate_profiles()

    async def on_window_change(
        self, window_class: str, window_title: str, window_tags: list[str]
    ) -> None:
        window_info = self._normalize_window_info(window_class, window_title, window_tags)

        if self.verbosity >= 1:
            log.debug(
                "Window changed: class=%s, title=%s, tags=%s",
                window_class,
                window_title,
                window_tags,
            )

        self._current_window = window_info
        await self._reevaluate_profiles()

    def _send_notification(self, title: str, message: str) -> None:
        log.info("Notification: %s: %s", title, message)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._send_notification_async(title, message))

    async def _send_notification_async(self, title: str, message: str) -> None:
        try:
            await self.dbus.notify(title, message, app_name="keyforge", timeout_ms=2000)
        except Exception as e:
            log.debug(f"Failed to send notification: {e}")

    def _is_recording_locked_error(self, result: dict) -> bool:
        if result.get("error_code") == "recording_locked":
            return True

        message = str(result.get("message", "") or "").lower()
        return "recording_locked" in message

    def _notify_recording_unlock_required(self, result: dict) -> None:
        if not self._is_recording_locked_error(result):
            return

        self._send_notification(
            "Keyforge: Recording Locked",
            "Recording/capture requires unlock in Keyforge GUI.",
        )

    def _maybe_notify_profile_activation(
        self,
        device_name: str,
        old_profile_names: list[str],
        resolved: ResolvedDeviceProfile,
    ) -> None:
        if old_profile_names == resolved.active_profile_names:
            return
        if not resolved.notify_profiles:
            return
        profile_list = ", ".join(resolved.active_profile_names) or "passthrough"
        self._send_notification("Profile Activated", f"{device_name}: {profile_list}")

    def _profile_to_mapping(self, resolved: ResolvedDeviceProfile, hardware_id: str) -> dict:
        if hardware_id not in self._device_exec_refs:
            self._device_exec_refs[hardware_id] = set()

        mapping = {}
        for button_id, action in resolved.mappings.items():
            action_data = {"action": action.action_type.value}

            if action.action_type.value in (
                "keyboard",
                "mouse",
                "gamepad",
                "mouse_move_rel",
                "mouse_move_abs",
            ):
                action_data["target"] = action.target
                if action.action_type.value in ("mouse_move_rel", "mouse_move_abs"):
                    action_data["x"] = int(action.move_x)
                    action_data["y"] = int(action.move_y)
                if action.rapidfire_enabled:
                    action_data["rapidfire_enabled"] = True
                    action_data["rapidfire_hold_ms"] = action.rapidfire_hold_ms
                    action_data["rapidfire_wait_ms"] = action.rapidfire_wait_ms
                if action.tap_enabled:
                    action_data["tap_enabled"] = True
                    action_data["tap_hold_ms"] = action.tap_hold_ms
            elif action.action_type.value == "exec":
                if action.cmd:
                    exec_ref = self._next_exec_ref
                    self._next_exec_ref += 1
                    self._exec_refs[exec_ref] = action.cmd
                    self._device_exec_refs[hardware_id].add(exec_ref)
                    action_data["exec_ref"] = exec_ref
            elif action.action_type.value == "compositor_dispatch":
                action_data["dispatcher"] = action.compositor_dispatcher or ""
                action_data["args"] = action.compositor_args or ""
            elif action.action_type.value in (
                "start_macro_recording",
                "stop_macro_recording",
                "cancel_macro_playback",
            ):
                pass
            elif action.action_type.value in (
                "profile_enable",
                "profile_disable",
                "profile_toggle",
            ):
                action_data["profile_name"] = action.profile_name or action.target or ""
            elif action.action_type.value == "macro":
                if action.macro_name:
                    action_data["macro_name"] = action.macro_name
                    action_data["macro_replay_mouse_movement"] = action.macro_replay_mouse_movement
                    action_data["macro_replay_mouse_clicks"] = action.macro_replay_mouse_clicks
                    action_data["macro_speed"] = action.macro_speed
                    action_data["macro_loop_mode"] = action.macro_loop_mode
                    action_data["macro_loop_count"] = int(action.macro_loop_count)
            elif action.action_type.value == "superkey":
                if action.superkey_name:
                    superkey_config = self.superkeys.get_superkey(action.superkey_name)
                    if superkey_config:
                        action_data["superkey"] = self._serialize_superkey(
                            superkey_config, hardware_id
                        )

            mapping[button_id] = action_data

        return mapping

    def _resolved_combos_payload(self, combos: list[ResolvedCombo]) -> list[dict]:
        payload: list[dict] = []
        for combo in combos:
            if combo.action is None:
                continue
            action_data = self._combo_action_to_payload(combo.action)
            if action_data is None:
                continue
            steps: list[dict[str, object]] = []
            for step in combo.steps:
                events: list[dict[str, str]] = []
                for event in step.events:
                    if not event.hardware_id or not event.evdev:
                        continue
                    event_data = {
                        "hardware_id": event.hardware_id,
                        "evdev": event.evdev,
                    }
                    if event.source:
                        event_data["source"] = event.source
                    events.append(event_data)
                if events:
                    step_payload: dict[str, object] = {"events": events}
                    if step.timeout_ms is not None:
                        step_payload["timeout_ms"] = int(step.timeout_ms)
                    steps.append(step_payload)
            if not steps:
                continue
            payload.append(
                {
                    "id": combo.id,
                    "name": combo.name,
                    "profile_name": combo.profile_name,
                    "steps": steps,
                    "action": action_data,
                }
            )
        return payload

    def _combo_action_to_payload(self, action: MappingAction) -> dict | None:
        action_type = action.action_type.value
        action_data: dict[str, object] = {"action": action_type}

        if action_type in (
            "keyboard",
            "mouse",
            "gamepad",
            "mouse_move_rel",
            "mouse_move_abs",
        ):
            action_data["target"] = action.target
            if action_type in ("mouse_move_rel", "mouse_move_abs"):
                action_data["x"] = int(action.move_x)
                action_data["y"] = int(action.move_y)
            if action.rapidfire_enabled:
                action_data["rapidfire_enabled"] = True
                action_data["rapidfire_hold_ms"] = action.rapidfire_hold_ms
                action_data["rapidfire_wait_ms"] = action.rapidfire_wait_ms
            if action.tap_enabled:
                action_data["tap_enabled"] = True
                action_data["tap_hold_ms"] = action.tap_hold_ms
            return action_data

        if action_type == "exec":
            if not action.cmd:
                return None
            exec_ref = self._next_exec_ref
            self._next_exec_ref += 1
            self._exec_refs[exec_ref] = action.cmd
            self._combo_exec_refs.add(exec_ref)
            action_data["exec_ref"] = exec_ref
            return action_data

        if action_type == "compositor_dispatch":
            dispatcher = str(action.compositor_dispatcher or "").strip()
            if not dispatcher:
                return None
            action_data["dispatcher"] = dispatcher
            action_data["args"] = action.compositor_args or ""
            return action_data

        if action_type in (
            "start_macro_recording",
            "stop_macro_recording",
            "cancel_macro_playback",
        ):
            return action_data

        if action_type in ("profile_enable", "profile_disable", "profile_toggle"):
            action_data["profile_name"] = action.profile_name or action.target or ""
            return action_data

        if action_type == "macro":
            if action.macro_name:
                action_data["macro_name"] = action.macro_name
                action_data["macro_replay_mouse_movement"] = action.macro_replay_mouse_movement
                action_data["macro_replay_mouse_clicks"] = action.macro_replay_mouse_clicks
                action_data["macro_speed"] = action.macro_speed
                action_data["macro_loop_mode"] = action.macro_loop_mode
                action_data["macro_loop_count"] = int(action.macro_loop_count)
                return action_data
            return None

        if action_type == "suppress":
            return action_data

        return None

    def _serialize_superkey(self, config, hardware_id: str) -> dict:
        data = {
            "name": config.name,
            "tap_timeout_ms": config.tap_timeout_ms,
            "double_tap_window_ms": config.double_tap_window_ms,
            "hold_threshold_ms": config.hold_threshold_ms,
        }

        if config.tap_action:
            data["tap_action"] = self._serialize_superkey_action(config.tap_action, hardware_id)
        if config.double_tap_action:
            data["double_tap_action"] = self._serialize_superkey_action(
                config.double_tap_action, hardware_id
            )
        if config.hold_action:
            data["hold_action"] = self._serialize_superkey_action(config.hold_action, hardware_id)
        if config.tap_hold_action:
            data["tap_hold_action"] = self._serialize_superkey_action(
                config.tap_hold_action, hardware_id
            )

        return data

    def _serialize_superkey_signature(self, config, hardware_id: str) -> dict:
        data = {
            "name": config.name,
            "tap_timeout_ms": int(config.tap_timeout_ms),
            "double_tap_window_ms": int(config.double_tap_window_ms),
            "hold_threshold_ms": int(config.hold_threshold_ms),
        }

        if config.tap_action:
            data["tap_action"] = self._serialize_superkey_action_signature(
                config.tap_action,
                hardware_id,
            )
        if config.double_tap_action:
            data["double_tap_action"] = self._serialize_superkey_action_signature(
                config.double_tap_action,
                hardware_id,
            )
        if config.hold_action:
            data["hold_action"] = self._serialize_superkey_action_signature(
                config.hold_action,
                hardware_id,
            )
        if config.tap_hold_action:
            data["tap_hold_action"] = self._serialize_superkey_action_signature(
                config.tap_hold_action,
                hardware_id,
            )

        return data

    def _serialize_superkey_action(self, action, hardware_id: str) -> dict:
        data = {"action": action.action_type.value}

        if action.target:
            data["target"] = action.target
        if action.cmd:
            data["cmd"] = action.cmd
        if action.macro_name:
            data["macro_name"] = action.macro_name
        if action.rapidfire_enabled:
            data["rapidfire_enabled"] = True
            data["rapidfire_hold_ms"] = action.rapidfire_hold_ms
            data["rapidfire_wait_ms"] = action.rapidfire_wait_ms

        if action.action_type.value == "exec" and action.cmd:
            exec_ref = self._next_superkey_exec_ref
            self._next_superkey_exec_ref += 1
            self._superkey_exec_refs[exec_ref] = (hardware_id, action.cmd)
            data["exec_ref"] = exec_ref

        return data

    def _serialize_superkey_action_signature(self, action, hardware_id: str) -> dict:
        data = {"action": action.action_type.value}

        if action.target:
            data["target"] = action.target
        if action.cmd:
            data["cmd"] = action.cmd
        if action.macro_name:
            data["macro_name"] = action.macro_name
        if action.rapidfire_enabled:
            data["rapidfire_enabled"] = True
            data["rapidfire_hold_ms"] = int(action.rapidfire_hold_ms)
            data["rapidfire_wait_ms"] = int(action.rapidfire_wait_ms)

        if action.action_type.value == "superkey" and action.superkey_name:
            superkey_config = self.superkeys.get_superkey(action.superkey_name)
            if superkey_config:
                data["superkey"] = self._serialize_superkey_signature(superkey_config, hardware_id)

        return data

    def _compositor_dispatch_available(self) -> bool:
        return self._window_listener is not None and bool(
            getattr(self._window_listener, "running", False)
            and self._window_listener.supports_compositor_dispatch
        )

    async def _play_macro_by_name(self, name: str) -> None:
        try:
            get_result = await self.client.send_command(
                Command(command=CommandType.MACRO_GET, data={"name": name})
            )
            if get_result.status != "ok" or not isinstance(get_result.data, dict):
                return
            macro = get_result.data.get("macro")
            if not isinstance(macro, dict):
                return
            macro = self._sanitize_macro_for_policy(macro)
            payload = {
                "macro_name": str(macro.get("name", name) or name),
                "macro_events": macro.get("events", []),
                "replay_mouse_movement": True,
                "replay_mouse_clicks": True,
                "speed": 1.0,
                "loop_mode": str(macro.get("loop_mode", "none") or "none"),
                "loop_count": int(macro.get("loop_count", 1) or 1),
                "move_to_start": bool(macro.get("move_to_start", False)),
                "start_x": int(macro.get("start_x", 0) or 0),
                "start_y": int(macro.get("start_y", 0) or 0),
                "block_mouse_movement": bool(macro.get("block_mouse_movement", False)),
            }

            await self.client.send_command(Command(command=CommandType.PLAY_MACRO, data=payload))
        except Exception:
            pass

    def _sanitize_macro_for_policy(self, macro: dict) -> dict:
        cloned = dict(macro)
        events = cloned.get("events")
        if not isinstance(events, list):
            return cloned

        max_timeout = max(1, int(self._security_policy.macro_exec_timeout_max_ms))
        sanitized: list[dict] = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            item = dict(ev)
            action = str(item.get("macro_action", "") or "").lower()
            if action == "exec_sync":
                timeout_ms = int(item.get("timeout_ms", max_timeout) or max_timeout)
                item["timeout_ms"] = max(1, min(timeout_ms, max_timeout))
            sanitized.append(item)
        cloned["events"] = sanitized
        return cloned

    def _update_recording_settings(self, request: dict) -> None:
        if "include_mouse_movement" in request:
            self._recording_settings["include_mouse_movement"] = bool(
                request.get("include_mouse_movement")
            )
        if "include_mouse_clicks" in request:
            self._recording_settings["include_mouse_clicks"] = bool(
                request.get("include_mouse_clicks")
            )
        if "record_start_position" in request:
            self._recording_settings["record_start_position"] = bool(
                request.get("record_start_position")
            )
        if "record_keyboard" in request:
            self._recording_settings["record_keyboard"] = bool(request.get("record_keyboard"))
        if "record_mouse" in request:
            self._recording_settings["record_mouse"] = bool(request.get("record_mouse"))
        if "record_gamepad" in request:
            self._recording_settings["record_gamepad"] = bool(request.get("record_gamepad"))
        if "device_overrides" in request:
            overrides = request.get("device_overrides")
            if isinstance(overrides, dict):
                self._recording_settings["device_overrides"] = {
                    str(path): bool(enabled) for path, enabled in overrides.items()
                }
        self._queue_recording_settings_save(dict(self._recording_settings))

    def _queue_recording_settings_save(self, settings: dict) -> None:
        self._recording_settings_pending_save = settings
        if self._recording_settings_save_task and not self._recording_settings_save_task.done():
            return
        self._recording_settings_save_task = asyncio.create_task(
            self._flush_recording_settings_saves()
        )

    async def _flush_recording_settings_saves(self) -> None:
        try:
            while self._recording_settings_pending_save is not None:
                pending = self._recording_settings_pending_save
                self._recording_settings_pending_save = None
                await asyncio.to_thread(self._save_recording_settings_to_disk, pending)
        finally:
            self._recording_settings_save_task = None

    def _load_recording_settings_from_disk(self) -> None:
        try:
            if not self._RECORDING_SETTINGS_PATH.exists():
                return
            data = json.loads(self._RECORDING_SETTINGS_PATH.read_text())
            if not isinstance(data, dict):
                return
            self._recording_settings["include_mouse_movement"] = bool(
                data.get("include_mouse_movement", False)
            )
            self._recording_settings["include_mouse_clicks"] = bool(
                data.get("include_mouse_clicks", False)
            )
            self._recording_settings["record_start_position"] = bool(
                data.get("record_start_position", False)
            )
            self._recording_settings["record_keyboard"] = bool(data.get("record_keyboard", True))
            self._recording_settings["record_mouse"] = bool(data.get("record_mouse", False))
            self._recording_settings["record_gamepad"] = bool(data.get("record_gamepad", True))
            overrides = data.get("device_overrides", {})
            if isinstance(overrides, dict):
                self._recording_settings["device_overrides"] = {
                    str(path): bool(enabled) for path, enabled in overrides.items()
                }
        except Exception:
            pass

    def _save_recording_settings_to_disk(self, settings: dict | None = None) -> None:
        settings = settings or self._recording_settings
        try:
            existing: dict = {}
            if self._RECORDING_SETTINGS_PATH.exists():
                loaded = json.loads(self._RECORDING_SETTINGS_PATH.read_text())
                if isinstance(loaded, dict):
                    existing = dict(loaded)

            existing["include_mouse_movement"] = bool(
                settings.get("include_mouse_movement", False)
            )
            existing["include_mouse_clicks"] = bool(
                settings.get("include_mouse_clicks", False)
            )
            existing["record_start_position"] = bool(
                settings.get("record_start_position", False)
            )

            self._RECORDING_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._RECORDING_SETTINGS_PATH.write_text(json.dumps(existing))
        except Exception:
            pass

    async def _start_recording(self, reset_if_active: bool = False) -> dict:
        if self._recording_active:
            if not reset_if_active:
                return {"status": "error", "message": "Recording already in progress"}
            try:
                result = await self.client.send_command(Command(command=CommandType.STOP_RECORDING))
                if result.status == "ok" and isinstance(result.data, dict):
                    self._pending_recording_data = result.data
            except Exception:
                pass
            self._recording_active = False

        include_mouse_movement = self._recording_settings.get("include_mouse_movement", False)
        include_mouse_clicks = self._recording_settings.get("include_mouse_clicks", False)
        record_start_position = self._recording_settings.get("record_start_position", False)
        device_types: list[str] = []
        if self._recording_settings.get("record_keyboard", True):
            device_types.append("keyboard")
        if self._recording_settings.get("record_gamepad", True):
            device_types.append("gamepad")
        if self._recording_settings.get("record_mouse", False) or (
            include_mouse_movement or include_mouse_clicks
        ):
            device_types.append("mouse")

        devices: list[dict]
        if self._recording_devices_cache:
            devices = [
                d
                for d in self._recording_devices_cache
                if self._recording_device_matches_types(d, device_types)
            ]
        else:
            try:
                devices = await asyncio.wait_for(
                    self._get_devices_for_recording(device_types),
                    timeout=1.5,
                )
            except Exception:
                devices = []

        overrides = self._recording_settings.get("device_overrides", {})
        if isinstance(overrides, dict) and overrides:
            devices = [
                d
                for d in devices
                if bool(
                    overrides.get(
                        str(d.get("path", "")),
                        self._recording_device_matches_types(d, device_types),
                    )
                )
            ]
        log.debug(
            "recording start device selection: types=%s overrides=%r devices=%s",
            device_types,
            overrides,
            [str(d.get("path", "")) for d in devices],
        )

        # Get initial cursor position if enabled, otherwise use 0,0
        start_x, start_y = 0, 0
        self._recording_start_cursor = None
        if record_start_position:
            if self._window_listener:
                try:
                    pos = await self._window_listener.get_cursor_position()
                    if pos:
                        start_x, start_y = int(pos[0]), int(pos[1])
                        self._recording_start_cursor = (start_x, start_y)
                        log.debug(
                            f"Recording start cursor position captured: x={start_x}, y={start_y}"
                        )
                    else:
                        log.debug("Recording start: get_cursor_position returned None")
                except Exception as e:
                    log.debug(f"Failed to get cursor position for recording start: {e}")
            else:
                log.debug("Recording start: no window listener available")
        else:
            log.debug("Recording start: record_start_position is disabled")

        try:
            result = await self.client.send_command(
                Command(
                    command=CommandType.START_RECORDING,
                    data={
                        "devices": devices,
                        "include_mouse_movement": include_mouse_movement,
                        "include_mouse_clicks": include_mouse_clicks,
                        "start_x": start_x,
                        "start_y": start_y,
                    },
                )
            )
        except Exception:
            return {"status": "error", "message": "Daemon unavailable"}

        if result.status == "ok":
            self._recording_active = True
            return result.data or {"status": "ok"}

        message = str(result.error or "Daemon unavailable")
        response = {"status": "error", "message": message}
        if "recording_locked" in message.lower():
            response["error_code"] = "recording_locked"
        return response

    async def _refresh_recording_devices_cache(self) -> None:
        try:
            devices = await self._get_devices_for_recording(["keyboard", "gamepad", "mouse"])
            self._recording_devices_cache = devices
        except Exception:
            pass

    def _recording_device_types(self, device: dict) -> list[str]:
        return normalize_input_classes(
            device.get("device_types"),
            device.get("device_type", "other"),
        )

    def _recording_device_matches_types(self, device: dict, device_types: list[str]) -> bool:
        return bool(set(device_types).intersection(self._recording_device_types(device)))

    async def _get_devices_for_recording(
        self,
        device_types: list[str],
        include_grabbed: bool = False,
    ) -> list[dict]:
        try:
            result = await self.client.send_command(Command(command=CommandType.LIST_DEVICES))
        except Exception:
            return []
        if result.status != "ok" or not result.data:
            return []

        grabbed_paths = {
            p for interface_map in self._grabbed_interfaces.values() for p in interface_map.values()
        }

        devices = []
        for d in result.data.get("devices", []):
            path = d.get("path")
            dtype = d.get("device_type", "other")
            resolved_types = self._recording_device_types(d)
            if not path or not set(device_types).intersection(resolved_types):
                continue

            is_grabbed = path in grabbed_paths
            if is_grabbed and not include_grabbed:
                continue

            devices.append(
                {
                    "path": path,
                    "name": d.get("name", path),
                    "vendor_id": str(d.get("vendor_id", "") or ""),
                    "product_id": str(d.get("product_id", "") or ""),
                    "device_type": dtype,
                    "device_types": resolved_types,
                    "grabbed_by_keyforge": is_grabbed,
                }
            )

        return devices

    async def _save_recording(
        self,
        name: str,
        move_to_start: bool = False,
        start_x: int = 0,
        start_y: int = 0,
        block_mouse_movement: bool = False,
    ) -> dict:
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", name).strip("._")
        if not safe_name:
            raise ValueError("Invalid macro name")

        data = self._pending_recording_data or {}
        macro = {
            "name": safe_name,
            "created_at": datetime.now().isoformat(),
            "duration_ms": int(data.get("duration_ms", 0)),
            "device_types": data.get("device_types", []),
            "events": data.get("events", []),
            "move_to_start": bool(move_to_start),
            "start_x": int(start_x),
            "start_y": int(start_y),
            "block_mouse_movement": bool(block_mouse_movement),
        }
        try:
            result = await self.client.send_command(
                Command(command=CommandType.MACRO_CREATE, data={"macro": macro})
            )
        except Exception:
            return {"status": "error", "message": "Daemon unavailable"}

        if result.status != "ok":
            return {"status": "error", "message": result.error or "Failed to save recording"}

        created_name = safe_name
        if isinstance(result.data, dict):
            created = result.data.get("macro")
            if isinstance(created, dict):
                created_name = str(created.get("name", safe_name))

        self._pending_recording_data = None
        self._broadcast_to_session_clients({"event": "macro_saved", "name": created_name})
        return {"status": "ok", "name": created_name}

    def _mapping_log_view(self, mapping: dict) -> dict:
        view: dict = {}
        for button_id, action_data in mapping.items():
            if not isinstance(action_data, dict):
                view[button_id] = action_data
                continue

            data = dict(action_data)
            events = data.get("macro_events")
            if isinstance(events, list):
                data["macro_events"] = f"<{len(events)} events>"
            view[button_id] = data

        return view

    def _event_log_view(self, data: dict) -> dict:
        view = dict(data)
        if isinstance(view.get("events"), list):
            events = view["events"]
            view["events"] = f"<{len(events)} events>"
            if "event_count" not in view:
                view["event_count"] = len(events)
        return view


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="keyforge-session",
        description="Keyforge Session Manager",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Enable debug logging (-v) or trace logging (-vv)",
    )

    args = parser.parse_args()

    if args.verbose >= 2:
        log_level = logging.DEBUG
    elif args.verbose >= 1:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.verbose >= 2:
        log.info("Trace logging enabled (-vv)")

    try:
        manager = SessionManager(verbosity=args.verbose)
        asyncio.run(manager.start())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log.error(f"Fatal error: {e}")
        raise


if __name__ == "__main__":
    main()
