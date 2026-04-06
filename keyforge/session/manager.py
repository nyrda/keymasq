import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import traceback
from datetime import datetime
from typing import cast

from keyforge.common.devices import resolve_evdev_code
from keyforge.common.ipc import Command, CommandType
from keyforge.common.models import (
    ButtonDefinition,
    HardwareConfig,
    MappingAction,
    SuperkeyAction,
    SuperkeyConfig,
)
from keyforge.common.paths import (
    CONFIG_DIR,
    SECURITY_POLICY_PATH,
    SESSION_SOCKET_PATH,
    SOCKET_PATH,
    ensure_session_socket_dir,
)
from keyforge.common.security import (
    PeerCredentials,
    SecurityPolicy,
    get_peer_credentials,
    load_security_policy,
    uid_allowed,
)
from keyforge.session import manager_compositor as runtime_compositor
from keyforge.session import manager_recording as runtime_recording
from keyforge.session import manager_session_commands as session_commands
from keyforge.session.action_handler import ActionHandler
from keyforge.session.client import KeyforgedClient
from keyforge.session.dbus import SessionDBus
from keyforge.session.hardware import HardwareManager
from keyforge.session.listeners.base import WindowListener
from keyforge.session.manager_common import JsonObject
from keyforge.session.manager_common import int_value as _int_value
from keyforge.session.manager_common import json_list as _json_list
from keyforge.session.manager_common import json_object as _json_object
from keyforge.session.manager_common import str_value as _str_value
from keyforge.session.manager_state import (
    CaptureRuntimeState,
    RecordingRuntimeState,
    UnlockRuntimeState,
)
from keyforge.session.profiles import ProfileManager, ResolvedCombo, ResolvedDeviceProfile
from keyforge.session.superkeys import SuperkeyManager

log = logging.getLogger("keyforge-session")
GRAB_DEVICE_TIMEOUT_S = 330.0
GRAB_RETRY_DELAY_S = 5.0
TOPOLOGY_REFRESH_DEBOUNCE_S = 0.5
TOPOLOGY_REFRESH_RETRY_S = 1.0


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
        self._reload_task: asyncio.Task[None] | None = None
        self._reload_pending = False
        self.verbosity = verbosity

        self._grabbed_devices: set[str] = set()
        self._grabbed_interfaces: dict[str, dict[str, str]] = {}
        self._grab_waiting_devices: set[str] = set()
        self._grab_retry_tasks: dict[str, asyncio.Task[None]] = {}
        self._topology_refresh_task: asyncio.Task[None] | None = None
        self._last_sent_mapping_signatures: dict[str, str] = {}
        self._last_sent_combo_signature: str = ""
        self._active_profile_names: list[str] = []
        self._resolved_devices: dict[str, ResolvedDeviceProfile] = {}
        self._current_window: JsonObject = {}
        self._window_listener: WindowListener | None = None
        self._compositor_id: str | None = None
        self._compositor_capabilities: list[str] = []
        self._compositor_supervisor_task: asyncio.Task[None] | None = None
        self._connect_task: asyncio.Task[None] | None = None
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
        self.capture_state = CaptureRuntimeState()

        self._exec_refs: dict[int, str] = {}
        self._next_exec_ref: int = 1
        self._device_exec_refs: dict[str, set[int]] = {}
        self._combo_exec_refs: set[int] = set()

        self._superkey_exec_refs: dict[int, tuple[str, str]] = {}
        self._next_superkey_exec_ref: int = 10000
        self.recording_state = RecordingRuntimeState()
        self.unlock_state = UnlockRuntimeState()
        runtime_recording.load_recording_settings_from_disk(self)
        self._security_policy: SecurityPolicy = load_security_policy(SECURITY_POLICY_PATH)
        self.dbus = SessionDBus()

        self.action_handler = ActionHandler()

    def _resolved_button_codes(self, buttons: list[ButtonDefinition]) -> dict[str, int]:
        resolved: dict[str, int] = {}
        for button in buttons:
            button_id = str(getattr(button, "id", "") or "")
            if not button_id:
                continue
            code = getattr(button, "evdev_code", None)
            if code is None:
                code = resolve_evdev_code(getattr(button, "evdev", None))
            if code is None:
                continue
            resolved[button_id] = int(code)
        return resolved

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
        self._compositor_supervisor_task = asyncio.create_task(
            runtime_compositor.compositor_supervisor_loop(self)
        )

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

        if self._topology_refresh_task:
            self._topology_refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._topology_refresh_task
            self._topology_refresh_task = None

        save_task = cast(asyncio.Task[None] | None, self.recording_state.settings_save_task)
        if save_task:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await save_task
            self.recording_state.settings_save_task = None

        await runtime_compositor.stop_window_listener(self)
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

        for token in list(self.capture_state.tokens.values()):
            try:
                await self.client.send_command(
                    Command(command=CommandType.CAPTURE_END, data={"token": token})
                )
            except Exception:
                pass
        self.capture_state.tokens.clear()

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
            await runtime_recording.clear_recording_refresh_owner_if_writer(self, peer, writer)
            self._session_clients.discard(writer)
            self._session_client_peers.pop(writer, None)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_session_request(
        self,
        request: JsonObject,
        client_class: str,
        peer: PeerCredentials,
        writer: asyncio.StreamWriter,
    ) -> JsonObject:
        return await session_commands.handle_session_request(
            self,
            request,
            client_class,
            peer,
            writer,
        )

    def _build_active_profiles_payload(self) -> JsonObject:
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

    def _broadcast_profiles_changed(self) -> None:
        message = {"event": "profiles_changed", **self._build_active_profiles_payload()}
        self._broadcast_to_session_clients(message)

    def _broadcast_keyforged_status(self, connected: bool) -> None:
        message = {
            "event": "keyforged_status",
            "connected": connected,
        }
        self._broadcast_to_session_clients(cast(JsonObject, message))

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

    def _handle_device_grab_status_event(self, data: JsonObject) -> None:
        hardware_id = str(data.get("hardware_id", "") or "")
        state = str(data.get("state", "") or "").strip().lower()
        active_keys = [str(key) for key in _json_list(data.get("active_keys")) if str(key)]
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

    def _broadcast_to_session_clients(self, message: JsonObject) -> None:
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

    def _invalidate_grabbed_state(self) -> None:
        for hardware_id in list(self._device_exec_refs):
            self._clear_exec_refs(hardware_id)
        self._clear_combo_exec_refs()
        for task in list(self._grab_retry_tasks.values()):
            if not task.done():
                task.cancel()
        self._grab_retry_tasks.clear()
        self._grabbed_devices.clear()
        self._grabbed_interfaces.clear()
        self._grab_waiting_devices.clear()
        self._last_sent_mapping_signatures.clear()
        self._last_sent_combo_signature = ""

    def _schedule_topology_refresh(self) -> None:
        existing = self._topology_refresh_task
        if existing is not None and not existing.done():
            existing.cancel()

        async def _run() -> None:
            try:
                delay = TOPOLOGY_REFRESH_DEBOUNCE_S
                while True:
                    await asyncio.sleep(delay)
                    try:
                        self._invalidate_grabbed_state()
                        await self._reevaluate_profiles()
                        return
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.warning("Topology refresh failed: %s", e)
                        delay = TOPOLOGY_REFRESH_RETRY_S
            except asyncio.CancelledError:
                raise
            finally:
                task = self._topology_refresh_task
                if task is asyncio.current_task():
                    self._topology_refresh_task = None

        self._topology_refresh_task = asyncio.create_task(_run())

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
        self.profiles.reload()
        self.hardware.reload()

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


    async def _handle_event(self, event_type: CommandType, data: JsonObject) -> None:
        if self.verbosity >= 1:
            log.debug(f"Event: {event_type.value} -> {self._event_log_view(data)}")

        if event_type == CommandType.ACTION_TRIGGER:
            exec_ref_raw = data.get("exec_ref")
            exec_ref = _int_value(exec_ref_raw, -1) if exec_ref_raw is not None else None
            if exec_ref is not None:
                if exec_ref >= 10000:
                    ref_data = self._superkey_exec_refs.get(exec_ref)
                    if ref_data:
                        hardware_id, cmd = ref_data
                        data["cmd"] = cmd
                        data["hardware_id"] = hardware_id
                        action_handler = self.action_handler
                        if action_handler is not None:
                            await action_handler.handle_action(data)
                    else:
                        log.warning(f"Unknown superkey exec_ref: {exec_ref}")
                else:
                    cmd = self._exec_refs.get(exec_ref)
                    if cmd:
                        data["cmd"] = cmd
                        action_handler = self.action_handler
                        if action_handler is not None:
                            await action_handler.handle_action(data)
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
                asyncio.create_task(
                    runtime_compositor.handle_compositor_dispatch_trigger(self, data)
                )
            elif action_type_str == "macro":
                macro_name = str(data.get("macro_name", "")).strip()
                if macro_name:
                    asyncio.create_task(runtime_recording.play_macro_by_name(self, macro_name))
        elif event_type == CommandType.DEVICE_CONNECTED:
            log.info(f"Device connected: {data}")
            await self._on_device_connected(data)
        elif event_type == CommandType.DEVICE_DISCONNECTED:
            log.info(f"Device disconnected: {data}")
            await self._on_device_disconnected(data)
        elif event_type == CommandType.DEVICE_GRAB_STATUS:
            self._handle_device_grab_status_event(data)
        elif event_type == CommandType.RECORDING_STARTED:
            self.recording_state.active = True
            self._broadcast_to_session_clients({"event": "recording_started", **data})
        elif event_type == CommandType.RECORDING_STOPPED:
            self.recording_state.active = False
            recording_data = dict(data)
            if self.recording_state.start_cursor:
                recording_data["start_x"] = int(self.recording_state.start_cursor[0])
                recording_data["start_y"] = int(self.recording_state.start_cursor[1])
                recording_data["move_to_start"] = True
            self.recording_state.pending_data = recording_data
            self.recording_state.start_cursor = None
            self._broadcast_to_session_clients(
                {
                    "event": "recording_stopped",
                    "duration_ms": recording_data.get("duration_ms", 0),
                    "event_count": len(_json_list(recording_data.get("events"))),
                    "device_types": recording_data.get("device_types", []),
                    "start_x": recording_data.get("start_x"),
                    "start_y": recording_data.get("start_y"),
                    "move_to_start": recording_data.get("move_to_start", False),
                }
            )
        elif event_type == CommandType.RECORDING_PROGRESS:
            self._broadcast_to_session_clients({"event": "recording_progress", **data})

    async def _handle_start_macro_trigger(self) -> None:
        if self.recording_state.active:
            await self._handle_stop_macro_trigger()
            return

        if not runtime_recording.has_active_gui_recording_owner(self):
            log.info("Ignored start_macro_recording trigger: no active GUI recording owner")
            self._send_notification(
                "Keyforge: Recording Unavailable",
                "Macro recording from triggers requires Keyforge GUI to be open.",
            )
            self._broadcast_to_session_clients({"event": "recording_auth_requested"})
            return

        result = await runtime_recording.start_recording(self, reset_if_active=False)
        if result.get("status") != "ok":
            runtime_recording.notify_recording_unlock_required(self, result)
            self._broadcast_to_session_clients({"event": "recording_auth_requested"})

    async def _handle_stop_macro_trigger(self) -> None:
        if not self.recording_state.active:
            return
        try:
            await runtime_recording.stop_recording(self, error_if_idle=False)
        except Exception:
            pass

    async def _handle_cancel_macro_trigger(self) -> None:
        try:
            await self.client.send_command(Command(command=CommandType.CANCEL_MACRO_PLAYBACK))
        except Exception:
            pass

    async def _handle_profile_trigger(self, data: JsonObject) -> None:
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

    async def _handle_exec_trigger(self, data: JsonObject) -> None:
        cmd = str(data.get("cmd", "") or "").strip()
        if not cmd:
            return

        wait_id = str(data.get("macro_exec_wait_id", "") or "").strip()
        is_async = bool(data.get("macro_exec_async", False))

        if wait_id:
            action_handler = self.action_handler
            if action_handler is None:
                return
            returncode = await action_handler.execute_command(cmd)
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
            action_handler = self.action_handler
            if action_handler is None:
                return
            action_handler.execute_command_sync(cmd)
            return

        action_handler = self.action_handler
        if action_handler is None:
            return
        await action_handler.execute_command(cmd)

    async def _activate_initial_profiles(self) -> None:
        hardware_ids = self.hardware.list_hardware_ids()
        log.info(f"Found {len(hardware_ids)} hardware config(s): {hardware_ids}")
        await self._reevaluate_profiles()

    async def _set_profile_enabled(
        self,
        profile_name: str,
        enabled: bool | None,
    ) -> JsonObject:
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

    def _build_profile_overview(self) -> JsonObject:
        known_hardware_ids = set(self.hardware.list_hardware_ids())
        for info in self.profiles.list_profiles():
            known_hardware_ids.update(info.config.device_layers.keys())

        profiles = sorted(
            self.profiles.list_profiles(),
            key=lambda p: (p.config.name.casefold(), p.config.created_at or datetime.min),
        )
        devices: list[JsonObject] = []
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
            if hardware_id in self.capture_state.locks:
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
        if hardware_id in self.capture_state.locks:
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
                        "button_codes": self._resolved_button_codes(hardware_config.buttons),
                        "button_sources": {
                            b.id: b.source for b in hardware_config.buttons if b.source
                        },
                        "force_grab_unmapped": bool(resolved.combo_event_count),
                    },
                ),
                timeout=GRAB_DEVICE_TIMEOUT_S,
            )
            if result.status == "ok":
                result_data = _json_object(result.data)
                grabbed_count = (
                    _int_value(result_data.get("grabbed_count"), 0)
                    if result_data is not None
                    else 0
                )
                self._cancel_grab_retry(hardware_id)
                self._grab_waiting_devices.discard(hardware_id)
                log.info(f"keyforged: Grabbed device {hardware_id}: {result.data}")
                if grabbed_count > 0:
                    self._grabbed_devices.add(hardware_id)
                    self._grabbed_interfaces[hardware_id] = new_interfaces
                else:
                    self._grabbed_devices.discard(hardware_id)
                    self._grabbed_interfaces.pop(hardware_id, None)
                    log.warning(
                        (
                            "keyforged grab returned zero interfaces for %s "
                            "(requested=%s, mappings=%d)"
                        ),
                        hardware_id,
                        list(new_interfaces.keys()),
                        len(resolved.mappings),
                    )
                    self._last_sent_mapping_signatures.pop(hardware_id, None)
                    return
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
                self._last_sent_mapping_signatures[hardware_id] = self._resolved_mapping_signature(
                    resolved, hardware_id
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
        self, hardware_config: HardwareConfig, resolved: ResolvedDeviceProfile
    ) -> dict[str, str]:
        from keyforge.common.models import ActionType

        interface_to_path: dict[str, str] = {}
        for dev in hardware_config.evdev_devices:
            if dev.id:
                interface_to_path[dev.id] = dev.path

        if resolved.always_grab_all:
            return interface_to_path

        button_to_source: dict[str, str] = {
            b.id: b.source for b in hardware_config.buttons if b.source
        }

        sources_to_grab: set[str] = set()

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
        return self._last_sent_mapping_signatures.get(
            hardware_id, ""
        ) != self._resolved_mapping_signature(resolved, hardware_id)

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
            if action.compositor_id:
                data["compositor"] = action.compositor_id
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

    async def _on_device_connected(self, device_info: JsonObject) -> None:
        hardware_id = (
            f"{_str_value(device_info.get('vendor_id'), '')}:"
            f"{_str_value(device_info.get('product_id'), '')}"
        )
        if not hardware_id or ":" not in hardware_id:
            return

        if (
            self.hardware.get_hardware(hardware_id) is None
            and hardware_id not in self._resolved_devices
        ):
            return
        self._schedule_topology_refresh()

    async def _on_device_disconnected(self, device_info: JsonObject) -> None:
        hardware_id = _str_value(device_info.get("hardware_id"), "")
        if not hardware_id or ":" not in hardware_id:
            hardware_id = (
                f"{_str_value(device_info.get('vendor_id'), '')}:"
                f"{_str_value(device_info.get('product_id'), '')}"
            )
        if not hardware_id or ":" not in hardware_id:
            return
        if (
            self.hardware.get_hardware(hardware_id) is None
            and hardware_id not in self._resolved_devices
        ):
            return
        self._schedule_topology_refresh()

    async def on_window_change(
        self, window_class: str, window_title: str, window_tags: list[str]
    ) -> None:
        await runtime_compositor.on_window_change(self, window_class, window_title, window_tags)

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

    def _profile_to_mapping(self, resolved: ResolvedDeviceProfile, hardware_id: str) -> JsonObject:
        if hardware_id not in self._device_exec_refs:
            self._device_exec_refs[hardware_id] = set()

        mapping: dict[str, dict[str, object]] = {}
        for button_id, action in resolved.mappings.items():
            action_data: dict[str, object] = {"action": action.action_type.value}

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
                if action.compositor_id:
                    action_data["compositor"] = action.compositor_id
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

        return cast(JsonObject, mapping)

    def _resolved_combos_payload(self, combos: list[ResolvedCombo]) -> list[JsonObject]:
        payload: list[JsonObject] = []
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

    def _combo_action_to_payload(self, action: MappingAction) -> JsonObject | None:
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
            if action.compositor_id:
                action_data["compositor"] = action.compositor_id
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

    def _serialize_superkey(self, config: SuperkeyConfig, hardware_id: str) -> JsonObject:
        data: JsonObject = {
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

    def _serialize_superkey_signature(
        self, config: SuperkeyConfig, hardware_id: str
    ) -> JsonObject:
        data: JsonObject = {
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

    def _serialize_superkey_action(
        self, action: SuperkeyAction, hardware_id: str
    ) -> JsonObject:
        data: JsonObject = {"action": action.action_type.value}

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

    def _serialize_superkey_action_signature(
        self, action: SuperkeyAction, hardware_id: str
    ) -> JsonObject:
        data: JsonObject = {"action": action.action_type.value}

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

        superkey_name = getattr(action, "superkey_name", None)
        if action.action_type.value == "superkey" and isinstance(superkey_name, str):
            superkey_config = self.superkeys.get_superkey(superkey_name)
            if superkey_config:
                data["superkey"] = self._serialize_superkey_signature(superkey_config, hardware_id)

        return data

    def _mapping_log_view(self, mapping: JsonObject) -> JsonObject:
        view: JsonObject = {}
        for button_id, action_data in mapping.items():
            action_data_dict = _json_object(action_data)
            if action_data_dict is None:
                view[button_id] = action_data
                continue

            data = dict(action_data_dict)
            events = data.get("macro_events")
            if isinstance(events, list):
                event_count = len(cast(list[object], events))
                data["macro_events"] = f"<{event_count} events>"
            view[button_id] = data

        return view

    def _event_log_view(self, data: JsonObject) -> JsonObject:
        view = dict(data)
        events = _json_list(view.get("events"))
        if events:
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
