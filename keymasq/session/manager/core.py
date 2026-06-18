import argparse
import asyncio
import contextlib
import ctypes
import json
import logging
import os
import signal
import socket
import struct
import sys
from pathlib import Path
from typing import cast

from keymasq.common.asyncio_runtime import ensure_uvloop
from keymasq.common.devices import resolve_evdev_code
from keymasq.common.ipc import Command, CommandType
from keymasq.common.models import (
    ButtonDefinition,
)
from keymasq.common.paths import (
    ANALOG_CONTROLS_DIR,
    CONFIG_DIR,
    HARDWARE_DIR,
    PROFILES_DIR,
    SECURITY_POLICY_PATH,
    SESSION_SOCKET_PATH,
    SETTINGS_PATH,
    SOCKET_PATH,
    SUPERKEYS_DIR,
    ensure_session_socket_dir,
)
from keymasq.common.security import (
    PeerCredentials,
    SecurityPolicy,
    SecurityPolicyError,
    get_peer_credentials,
    load_security_policy,
    uid_allowed,
)
from keymasq.session.action_handler import ActionHandler
from keymasq.session.analog_controls import AnalogControlManager
from keymasq.session.client import KeymasqdClient
from keymasq.session.dbus import SessionDBus
from keymasq.session.hardware import HardwareManager
from keymasq.session.mpris import MprisController, MprisDBusError
from keymasq.session.profiles import ProfileManager
from keymasq.session.settings import load_global_settings
from keymasq.session.superkeys import SuperkeyManager

from . import commands as session_commands
from . import compositor as runtime_compositor
from . import constants as manager_constants
from . import device_inspector as runtime_device_inspector
from . import events as runtime_events
from . import profiles as runtime_profiles
from . import recording as runtime_recording
from .common import JsonObject
from .state import (
    CaptureRuntimeState,
    CompositorRuntimeState,
    DeviceInspectorRuntimeState,
    EventRuntimeState,
    ExecRuntimeState,
    ProfileRuntimeState,
    RecordingRuntimeState,
    UnlockRuntimeState,
)

log = logging.getLogger("keymasq-session")
_TASK_SHUTDOWN_ERRORS = (asyncio.CancelledError, OSError, RuntimeError)
GRAB_DEVICE_TIMEOUT_S = manager_constants.GRAB_DEVICE_TIMEOUT_S
GRAB_RETRY_DELAY_S = manager_constants.GRAB_RETRY_DELAY_S
TOPOLOGY_REFRESH_DEBOUNCE_S = manager_constants.TOPOLOGY_REFRESH_DEBOUNCE_S
TOPOLOGY_REFRESH_RETRY_S = manager_constants.TOPOLOGY_REFRESH_RETRY_S
CONFIG_RELOAD_DEBOUNCE_S = 0.5
IN_ACCESS = 0x00000001
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_IGNORED = 0x00008000
IN_ISDIR = 0x40000000
IN_NONBLOCK = 0x00000800
IN_CLOEXEC = 0x00080000
INOTIFY_EVENT_STRUCT = struct.Struct("iIII")
INOTIFY_WATCH_MASK = (
    IN_ATTRIB
    | IN_CLOSE_WRITE
    | IN_CREATE
    | IN_DELETE
    | IN_DELETE_SELF
    | IN_MOVE_SELF
    | IN_MOVED_FROM
    | IN_MOVED_TO
)


class SessionManager:
    RECORDING_SETTINGS_PATH = CONFIG_DIR / "recording_settings.toml"
    MAX_SESSION_CLIENT_BUFFER_BYTES = 16 * 1024 * 1024
    SESSION_CLIENT_CLOSE_TIMEOUT_S = 0.5

    def __init__(self, verbosity: int = 0) -> None:
        async def _client_event_handler(event_type: CommandType, data: JsonObject) -> None:
            await runtime_events.handle_event(self, event_type, data)

        self.client: KeymasqdClient = KeymasqdClient(_client_event_handler)
        self.superkeys = SuperkeyManager()
        self.analog_controls = AnalogControlManager()
        self.profiles = ProfileManager(
            superkey_manager=self.superkeys,
            analog_control_manager=self.analog_controls,
            auto_create_default_if_empty=True,
        )
        self.hardware = HardwareManager()
        settings = load_global_settings()
        self.virtual_gamepad_count = settings.virtual_gamepad_count
        self.action_handler: ActionHandler | None = None
        self.running = False
        self._shutdown_event = asyncio.Event()
        self._retry_event = asyncio.Event()
        self.connected = False
        self.restart_on_daemon_disconnect = _env_flag(
            "KEYMASQ_SESSION_RESTART_ON_DAEMON_DISCONNECT"
        )
        self.restart_requested = False
        self.reload_task: asyncio.Task[bool] | None = None
        self.reload_pending = False
        self.config_reload_timer: asyncio.TimerHandle | None = None
        self.config_watch_fd: int | None = None
        self.config_watch_watches: dict[int, Path] = {}
        self.verbosity = verbosity

        self.profile_state = ProfileRuntimeState()
        self.compositor_state = CompositorRuntimeState()
        self.connect_task: asyncio.Task[None] | None = None
        self.session_server: asyncio.Server | None = None
        self._session_socket_owned = False
        self.session_clients: set[asyncio.StreamWriter] = set()
        self.session_client_peers: dict[asyncio.StreamWriter, PeerCredentials] = {}
        self.session_client_drain_tasks: dict[
            asyncio.StreamWriter,
            asyncio.Task[None],
        ] = {}
        self.capture_state = CaptureRuntimeState()

        self.exec_state = ExecRuntimeState()
        self.event_state = EventRuntimeState()
        self.device_inspector_state = DeviceInspectorRuntimeState()
        self.recording_state = RecordingRuntimeState()
        self.unlock_state = UnlockRuntimeState()
        runtime_recording.load_recording_settings_from_disk(self)
        self.security_policy: SecurityPolicy = load_security_policy(SECURITY_POLICY_PATH)
        self.dbus = SessionDBus()
        self.mpris_controller = MprisController(self.dbus)

        self.action_handler = ActionHandler()

    def resolved_button_codes(self, buttons: list[ButtonDefinition]) -> dict[str, int]:
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

        log.info("Starting keymasq-session")

        await self._start_session_server()
        try:
            await self.mpris_controller.start()
        except MprisDBusError:
            log.debug("MPRIS controller startup deferred", exc_info=True)

        self.connect_task = asyncio.create_task(self.connect_loop())
        self._start_config_watcher()
        self.compositor_state.supervisor_task = asyncio.create_task(
            runtime_compositor.compositor_supervisor_loop(self)
        )

        try:
            await self._shutdown_event.wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        if not self.running:
            return

        log.info("Stopping keymasq-session")
        self.running = False

        self._shutdown_event.set()
        await runtime_profiles.cancel_all_grab_retries(self)

        if self.compositor_state.supervisor_task:
            self.compositor_state.supervisor_task.cancel()
            with contextlib.suppress(*_TASK_SHUTDOWN_ERRORS):
                await self.compositor_state.supervisor_task
            self.compositor_state.supervisor_task = None

        await runtime_events.cancel_event_tasks(self)

        if self.action_handler is not None:
            await self.action_handler.cancel_background_tasks()

        self._stop_config_watcher()

        profile_apply_task = self.profile_state.apply_task
        if profile_apply_task:
            profile_apply_task.cancel()
            with contextlib.suppress(*_TASK_SHUTDOWN_ERRORS):
                await profile_apply_task
            self.profile_state.apply_task = None

        topology_task = self.profile_state.topology_refresh_task
        if topology_task:
            topology_task.cancel()
            with contextlib.suppress(*_TASK_SHUTDOWN_ERRORS):
                await topology_task
            self.profile_state.topology_refresh_task = None

        save_task = self.recording_state.settings_save_task
        if save_task:
            with contextlib.suppress(*_TASK_SHUTDOWN_ERRORS):
                await save_task
            self.recording_state.settings_save_task = None

        await runtime_compositor.stop_window_listener(self)
        await self.mpris_controller.stop()
        await self.dbus.disconnect()

        if self.session_server:
            self.session_server.close()
            await self.session_server.wait_closed()

        session_writers = list(self.session_clients)
        if session_writers:
            await asyncio.gather(
                *(
                    self._close_session_writer(
                        writer,
                        self.session_client_peers.get(writer),
                    )
                    for writer in session_writers
                ),
                return_exceptions=True,
            )
            for writer in session_writers:
                self._drop_session_client_writer(writer)
        for task in list(self.session_client_drain_tasks.values()):
            task.cancel()
        if self.session_client_drain_tasks:
            await asyncio.gather(
                *self.session_client_drain_tasks.values(),
                return_exceptions=True,
            )
            self.session_client_drain_tasks.clear()
        await self._wait_for_session_clients_to_close()

        for token in list(self.capture_state.tokens.values()):
            try:
                await self.client.send_command(
                    Command(command=CommandType.CAPTURE_END, data={"token": token})
                )
            except OSError:
                log.debug("Failed to end daemon capture during session shutdown", exc_info=True)
            except Exception:
                log.exception("Unexpected failure ending daemon capture during session shutdown")
        self.capture_state.tokens.clear()

        await self.client.disconnect()
        await runtime_events.cancel_event_tasks(self)
        if self.action_handler is not None:
            await self.action_handler.cancel_background_tasks()

        if self.connect_task:
            self.connect_task.cancel()
            with contextlib.suppress(*_TASK_SHUTDOWN_ERRORS):
                await self.connect_task
            self.connect_task = None

        if self._session_socket_owned and SESSION_SOCKET_PATH.exists():
            try:
                SESSION_SOCKET_PATH.unlink()
            except OSError:
                log.debug("Failed to remove owned session socket", exc_info=True)
            self._session_socket_owned = False

    async def _start_session_server(self) -> None:
        ensure_session_socket_dir()

        if SESSION_SOCKET_PATH.exists():
            if await _session_socket_accepts_connections():
                msg = f"keymasq-session is already listening on {SESSION_SOCKET_PATH}"
                raise RuntimeError(msg)
            try:
                SESSION_SOCKET_PATH.unlink()
            except OSError:
                log.debug("Failed to remove stale session socket", exc_info=True)

        self.session_server = await asyncio.start_unix_server(
            self._handle_session_client,
            path=str(SESSION_SOCKET_PATH),
        )
        self._session_socket_owned = True
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
            await self._close_session_writer(writer)
            return

        if not uid_allowed(peer.uid, self.security_policy.session_allowed_uids):
            log.warning(
                "Denied session client pid=%s uid=%s reason=%s",
                peer.pid,
                peer.uid,
                f"uid {peer.uid} is not allowed by session policy",
            )
            await self._close_session_writer(writer, peer)
            return

        client_class = "client"

        log.debug(
            "Session client connected pid=%s uid=%s class=%s",
            peer.pid,
            peer.uid,
            client_class,
        )
        self.session_clients.add(writer)
        self.session_client_peers[writer] = peer
        buffer = b""

        try:
            while self.running:
                data = await reader.read(4096)
                if not data:
                    break

                buffer += data
                if len(buffer) > self.MAX_SESSION_CLIENT_BUFFER_BYTES:
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
                        except json.JSONDecodeError:
                            writer.write(json.dumps({"error": "invalid json"}).encode() + b"\n")
                            await writer.drain()
                            continue

                        try:
                            response = await self._handle_session_request(
                                request,
                                client_class,
                                peer,
                                writer,
                            )
                        except (ValueError, TypeError, KeyError) as exc:
                            response = {"status": "error", "message": str(exc)}
                        except Exception as exc:
                            log.exception(
                                "Session request failed pid=%s uid=%s",
                                peer.pid,
                                peer.uid,
                            )
                            response = {"status": "error", "message": str(exc)}
                        writer.write(json.dumps(response).encode() + b"\n")
                        await writer.drain()
        except asyncio.CancelledError:
            pass
        except OSError:
            log.debug("Session client I/O error", exc_info=True)
        except Exception:
            log.exception(
                "Unexpected session client error pid=%s uid=%s",
                peer.pid,
                peer.uid,
            )
        finally:
            try:
                await runtime_device_inspector.clear_device_inspectors_for_writer(self, writer)
            except Exception:
                log.exception("Failed to clear device inspectors for disconnected session client")
            try:
                await runtime_recording.clear_captures_for_writer(self, writer)
            except Exception:
                log.exception("Failed to clear captures for disconnected session client")
            runtime_recording.clear_active_recording_owner_if_writer(self, writer)
            await runtime_recording.clear_recording_refresh_owner_if_writer(self, peer, writer)
            self._drop_session_client_writer(writer)
            await self._close_session_writer(writer, peer)

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

    def _broadcast_keymasqd_status(self, connected: bool) -> None:
        message = {
            "event": "keymasqd_status",
            "connected": connected,
        }
        self.broadcast_to_session_clients(cast(JsonObject, message))

    def broadcast_to_session_clients(self, message: JsonObject) -> None:
        self.broadcast_to_session_client_ids(message, None)

    def broadcast_to_session_client_ids(
        self,
        message: JsonObject,
        writer_ids: set[int] | None,
    ) -> None:
        for writer in list(self.session_clients):
            if writer_ids is not None and id(writer) not in writer_ids:
                continue
            try:
                writer.write(json.dumps(message).encode() + b"\n")
                task = self.session_client_drain_tasks.get(writer)
                if task is None or task.done():
                    self.session_client_drain_tasks[writer] = asyncio.create_task(
                        self._drain_session_writer(writer)
                    )
            except OSError:
                peer = self.session_client_peers.get(writer)
                self._drop_session_client_writer(writer)
                asyncio.create_task(self._close_session_writer(writer, peer))
            except Exception:
                log.exception("Unexpected failure broadcasting to session client")
                peer = self.session_client_peers.get(writer)
                self._drop_session_client_writer(writer)
                asyncio.create_task(self._close_session_writer(writer, peer))

    async def _drain_session_writer(self, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(writer.drain(), timeout=2.0)
        except asyncio.CancelledError:
            raise
        except OSError:
            peer = self.session_client_peers.get(writer)
            self._drop_session_client_writer(writer)
            await self._close_session_writer(writer, peer)
        except Exception:
            log.exception("Unexpected failure draining session client writer")
            peer = self.session_client_peers.get(writer)
            self._drop_session_client_writer(writer)
            await self._close_session_writer(writer, peer)
        finally:
            if self.session_client_drain_tasks.get(writer) is asyncio.current_task():
                self.session_client_drain_tasks.pop(writer, None)

    async def _close_session_writer(
        self,
        writer: asyncio.StreamWriter,
        peer: PeerCredentials | None = None,
    ) -> None:
        try:
            writer.close()
            await asyncio.wait_for(
                writer.wait_closed(),
                timeout=self.SESSION_CLIENT_CLOSE_TIMEOUT_S,
            )
        except TimeoutError:
            if peer is None:
                log.debug("Timed out waiting for session client socket to close")
            else:
                log.debug(
                    "Timed out waiting for session client socket to close pid=%s uid=%s",
                    peer.pid,
                    peer.uid,
                )
            transport = getattr(writer, "transport", None)
            if transport is not None:
                try:
                    transport.abort()
                except (OSError, RuntimeError):
                    log.debug("Failed to abort session client transport", exc_info=True)
        except OSError:
            log.debug("Failed while waiting for session client socket to close", exc_info=True)
        except Exception:
            log.exception("Unexpected failure closing session client socket")

    def _drop_session_client_writer(self, writer: asyncio.StreamWriter) -> None:
        self.session_clients.discard(writer)
        self.session_client_peers.pop(writer, None)
        task = self.session_client_drain_tasks.pop(writer, None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _signal_handler(self) -> None:
        log.info("Received shutdown signal")
        self._shutdown_event.set()
        self._retry_event.set()

    async def _wait_for_session_clients_to_close(self, timeout_s: float = 1.0) -> None:
        if not self.session_clients:
            return

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.05, float(timeout_s))
        while self.session_clients and loop.time() < deadline:
            await asyncio.sleep(0.01)

        if self.session_clients:
            log.debug(
                "Timed out waiting for %s session client(s) to close",
                len(self.session_clients),
            )

    def _reload_handler(self) -> None:
        if self.reload_pending:
            log.debug("Reload already pending, skipping")
            return

        self.reload_pending = True

        if self.reload_task and not self.reload_task.done():
            log.debug("Reload task still running, will retry after")
            return

        log.info("Received reload signal (SIGHUP)")
        self.reload_task = asyncio.create_task(self.reload_profiles())

    async def reload_profiles(self) -> bool:
        await asyncio.sleep(0.05)

        self.reload_pending = False

        try:
            await asyncio.to_thread(self.reload_config_from_disk)
        except Exception as exc:
            log.exception(
                "Failed to reload user config from disk; keeping previous active config"
            )
            self.send_notification(
                "Keymasq Config Error",
                "Failed to reload config; keeping the previous active config. See logs.",
            )
            self.broadcast_to_session_clients(
                {
                    "event": "config_reload_failed",
                    "status": "error",
                    "message": str(exc),
                }
            )
            return False
        log.info("Reloaded all superkeys, analog controls, profiles and hardware configs")
        self.broadcast_to_session_clients({"event": "config_reloaded", "status": "ok"})
        await self._sync_virtual_gamepads_to_daemon()
        runtime_profiles.invalidate_runtime_payload_signatures(self)

        configured_ids = set(self.hardware.list_hardware_ids())
        stale_ids = [
            hardware_id
            for hardware_id in list(self.profile_state.grabbed_devices)
            if hardware_id not in configured_ids
        ]
        for hardware_id in stale_ids:
            log.info(f"Hardware removed for {hardware_id}, deactivating profile")
            await runtime_profiles.deactivate_profile(self, hardware_id, immediate=True)
            self.profile_state.resolved_devices.pop(hardware_id, None)

        await runtime_profiles.reevaluate_profiles(self, reason="config reload")
        return True

    def reload_config_from_disk(self) -> None:
        superkeys_snapshot = self.superkeys.snapshot_superkeys()
        analog_controls_snapshot = self.analog_controls.snapshot_analog_controls()
        profiles_snapshot = self.profiles.snapshot_profiles()
        hardware_snapshot = self.hardware.snapshot_hardware()
        old_virtual_gamepad_count = self.virtual_gamepad_count

        try:
            self.superkeys.reload()
            self.analog_controls.reload()
            self.profiles.reload()
            self.hardware.reload()
            settings = load_global_settings(strict=True)
            self.virtual_gamepad_count = settings.virtual_gamepad_count
        except Exception:
            self.superkeys.restore_superkeys(superkeys_snapshot)
            self.analog_controls.restore_analog_controls(analog_controls_snapshot)
            self.profiles.restore_profiles(profiles_snapshot)
            self.hardware.restore_hardware(hardware_snapshot)
            self.virtual_gamepad_count = old_virtual_gamepad_count
            raise

    def _start_config_watcher(self) -> None:
        try:
            fd = _inotify_init()
        except OSError as exc:
            log.warning("Failed to start user config watcher: %s", exc)
            return

        self.config_watch_fd = fd
        self._refresh_config_watches()
        try:
            asyncio.get_running_loop().add_reader(fd, self._handle_config_watch_events)
        except (OSError, RuntimeError, ValueError) as exc:
            log.warning("Failed to register user config watcher: %s", exc)
            self._stop_config_watcher()

    def _stop_config_watcher(self) -> None:
        timer = self.config_reload_timer
        if timer is not None:
            timer.cancel()
            self.config_reload_timer = None

        fd = self.config_watch_fd
        if fd is None:
            return

        with contextlib.suppress(RuntimeError, ValueError):
            asyncio.get_running_loop().remove_reader(fd)
        with contextlib.suppress(OSError):
            os.close(fd)
        self.config_watch_fd = None
        self.config_watch_watches.clear()

    def _refresh_config_watches(self) -> None:
        fd = self.config_watch_fd
        if fd is None:
            return

        watched_paths = set(self.config_watch_watches.values())
        for path in (CONFIG_DIR, PROFILES_DIR, HARDWARE_DIR, SUPERKEYS_DIR, ANALOG_CONTROLS_DIR):
            if path in watched_paths:
                continue
            try:
                wd = _inotify_add_watch(fd, path, INOTIFY_WATCH_MASK)
            except OSError as exc:
                log.debug("Failed to watch config path %s: %s", path, exc)
                continue
            self.config_watch_watches[wd] = path

    def _handle_config_watch_events(self) -> None:
        fd = self.config_watch_fd
        if fd is None:
            return

        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            return
        except OSError as exc:
            log.warning("User config watcher failed: %s", exc)
            self._stop_config_watcher()
            return

        should_reload = False
        offset = 0
        while offset + INOTIFY_EVENT_STRUCT.size <= len(data):
            wd, mask, _cookie, name_len = INOTIFY_EVENT_STRUCT.unpack_from(data, offset)
            offset += INOTIFY_EVENT_STRUCT.size
            raw_name = data[offset : offset + name_len]
            offset += name_len
            name = raw_name.split(b"\0", 1)[0].decode(errors="replace")
            watched_path = self.config_watch_watches.get(wd)
            if mask & IN_IGNORED:
                self.config_watch_watches.pop(wd, None)
            if watched_path is None:
                continue
            if self._config_watch_event_is_relevant(watched_path, name, mask):
                should_reload = True

        self._refresh_config_watches()
        if should_reload:
            self._schedule_config_reload()

    def _config_watch_event_is_relevant(self, watched_path: Path, name: str, mask: int) -> bool:
        if watched_path == CONFIG_DIR:
            if name == SETTINGS_PATH.name:
                return True
            return bool(mask & IN_ISDIR) and name in {
                PROFILES_DIR.name,
                HARDWARE_DIR.name,
                SUPERKEYS_DIR.name,
                ANALOG_CONTROLS_DIR.name,
            }
        if name:
            return name.endswith(".toml")
        return bool(mask & (IN_DELETE_SELF | IN_MOVE_SELF | IN_ATTRIB))

    def _schedule_config_reload(self) -> None:
        timer = self.config_reload_timer
        if timer is not None:
            timer.cancel()
        self.config_reload_timer = asyncio.get_running_loop().call_later(
            CONFIG_RELOAD_DEBOUNCE_S,
            self._run_scheduled_config_reload,
        )

    def _run_scheduled_config_reload(self) -> None:
        self.config_reload_timer = None
        if not self.running:
            return
        if self.reload_task is not None and not self.reload_task.done():
            log.debug("Config reload already running; skipping scheduled reload")
            return
        log.info("Detected user config file change; reloading")
        self.reload_task = asyncio.create_task(self.reload_profiles())

    async def _sync_virtual_gamepads_to_daemon(self) -> None:
        if not self.connected:
            return
        try:
            response = await self.client.send_command(
                Command(
                    command=CommandType.SET_VIRTUAL_GAMEPADS,
                    data={"count": int(self.virtual_gamepad_count)},
                )
            )
            if response.status == "ok" and isinstance(response.data, dict):
                data = cast(JsonObject, response.data)
                raw_count = data.get("count", self.virtual_gamepad_count)
                if isinstance(raw_count, (int, float, str)):
                    try:
                        self.virtual_gamepad_count = max(0, int(raw_count))
                    except (TypeError, ValueError, OverflowError):
                        log.warning(
                            "Ignoring malformed virtual gamepad count from keymasqd: %r",
                            raw_count,
                        )
        except OSError as exc:
            log.warning("Failed to configure virtual gamepads in keymasqd: %s", exc)

    async def connect_loop(self) -> None:
        retry_delay = 1.0
        max_delay = 30.0

        while self.running:
            try:
                log.info(f"Connecting to keymasqd at {SOCKET_PATH}")
                await self.client.connect()
                self.connected = True
                retry_delay = 1.0
                log.info("Connected to keymasqd")
                self._broadcast_keymasqd_status(True)
                await self._sync_virtual_gamepads_to_daemon()

                try:
                    await runtime_profiles.activate_initial_profiles(self)
                except Exception:
                    log.exception("Failed to activate initial profiles")
                await runtime_recording.sync_pending_macro_slots_from_daemon(self)
                await runtime_recording.refresh_recording_devices_cache(self)

                await self.client.wait_disconnected()
                log.warning("Disconnected from keymasqd")
                self._handle_keymasqd_disconnect()
                if self.restart_on_daemon_disconnect:
                    log.info("Requesting keymasq-session restart after keymasqd disconnect")
                    self.restart_requested = True
                    self._shutdown_event.set()
                    return

            except OSError as e:
                log.warning(f"Connection failed: {e}")
                self._handle_keymasqd_disconnect()
            except Exception:
                log.exception("Unexpected keymasqd connection loop failure")
                self._handle_keymasqd_disconnect()

            if self.running:
                try:
                    await asyncio.wait_for(self._retry_event.wait(), timeout=retry_delay)
                except TimeoutError:
                    pass
                retry_delay = min(retry_delay * 2, max_delay)

    def _handle_keymasqd_disconnect(self) -> None:
        was_connected = self.connected
        self.connected = False
        self.profile_state.grabbed_devices.clear()
        self.profile_state.grabbed_interfaces.clear()
        self.profile_state.grab_waiting_devices.clear()
        self.profile_state.grab_status.clear()
        self.profile_state.device_runtime_status.clear()
        for task in list(self.profile_state.grab_retry_tasks.values()):
            if not task.done():
                task.cancel()
        self.profile_state.grab_retry_tasks.clear()
        self.profile_state.last_sent_grab_signatures.clear()
        self.profile_state.last_sent_mapping_signatures.clear()
        self.profile_state.last_sent_combo_signature = ""
        self.profile_state.active_profile_names.clear()
        self.profile_state.resolved_devices.clear()
        self.profile_state.resolved_combos.clear()
        self.profile_state.runtime_profile_activations.clear()
        self.device_inspector_state.active_hardware_ids.clear()
        self.device_inspector_state.suppressed_hardware_ids.clear()
        self.device_inspector_state.owners_by_hardware_id.clear()
        self.recording_state.active = False
        self.recording_state.active_slot = 0
        self.recording_state.start_cursor = None
        self.recording_state.active_owner_writer_id = None
        self.recording_state.active_owner_pid = None
        self.recording_state.active_owner_uid = None
        self.recording_state.devices_cache.clear()
        self.recording_state.selected_devices_cache.clear()
        self.recording_state.devices_cache_ready = False

        if was_connected:
            self._broadcast_keymasqd_status(False)

    async def on_window_change(
        self, window_class: str, window_title: str, window_tags: list[str]
    ) -> None:
        await runtime_compositor.on_window_change(self, window_class, window_title, window_tags)

    def send_notification(self, title: str, message: str) -> None:
        log.info("Notification: %s: %s", title, message)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        runtime_events.create_event_task(
            self,
            self._send_notification_async(title, message),
            name="notification",
        )

    async def _send_notification_async(self, title: str, message: str) -> None:
        try:
            await self.dbus.notify(title, message, app_name="keymasq", timeout_ms=5000)
        except Exception:
            log.exception("Failed to send notification")


def _inotify_init() -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    inotify_init1 = libc.inotify_init1
    inotify_init1.argtypes = [ctypes.c_int]
    inotify_init1.restype = ctypes.c_int
    fd = int(inotify_init1(IN_NONBLOCK | IN_CLOEXEC))
    if fd < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return fd


def _inotify_add_watch(fd: int, path: Path, mask: int) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    inotify_add_watch = libc.inotify_add_watch
    inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    inotify_add_watch.restype = ctypes.c_int
    wd = int(inotify_add_watch(fd, os.fsencode(path), mask))
    if wd < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno), str(path))
    return wd


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="keymasq-session",
        description="Keymasq Session Manager",
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
    ensure_uvloop(log)

    if args.verbose >= 2:
        log.info("Trace logging enabled (-vv)")

    try:
        manager = SessionManager(verbosity=args.verbose)
        asyncio.run(manager.start())
        if manager.restart_requested:
            sys.exit(75)
    except KeyboardInterrupt:
        pass
    except SecurityPolicyError as exc:
        log.error("%s", exc)
        sys.exit(1)
    except Exception:
        log.exception("Fatal error")
        raise


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


async def _session_socket_accepts_connections(timeout_s: float = 0.2) -> bool:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.setblocking(False)
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().sock_connect(sock, str(SESSION_SOCKET_PATH)),
            timeout=timeout_s,
        )
    except TimeoutError:
        return True
    except OSError:
        return False
    finally:
        sock.close()
    return True


if __name__ == "__main__":
    main()
