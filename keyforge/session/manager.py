import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import traceback
from typing import cast

from keyforge.common.devices import resolve_evdev_code
from keyforge.common.ipc import Command, CommandType
from keyforge.common.models import (
    ButtonDefinition,
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
from keyforge.session import manager_events as runtime_events
from keyforge.session import manager_profiles as runtime_profiles
from keyforge.session import manager_recording as runtime_recording
from keyforge.session import manager_session_commands as session_commands
from keyforge.session.action_handler import ActionHandler
from keyforge.session.client import KeyforgedClient
from keyforge.session.dbus import SessionDBus
from keyforge.session.hardware import HardwareManager
from keyforge.session.listeners.base import WindowListener
from keyforge.session.manager_common import JsonObject
from keyforge.session.manager_state import (
    CaptureRuntimeState,
    ExecRuntimeState,
    ProfileRuntimeState,
    RecordingRuntimeState,
    UnlockRuntimeState,
)
from keyforge.session.profiles import ProfileManager
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
        async def _client_event_handler(event_type: CommandType, data: JsonObject) -> None:
            await runtime_events.handle_event(self, event_type, data)

        self.client = KeyforgedClient(_client_event_handler)
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

        self.profile_state = ProfileRuntimeState()
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

        self.exec_state = ExecRuntimeState()
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

        topology_task = self.profile_state.topology_refresh_task
        if topology_task:
            topology_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await topology_task
            self.profile_state.topology_refresh_task = None

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

    def _broadcast_keyforged_status(self, connected: bool) -> None:
        message = {
            "event": "keyforged_status",
            "connected": connected,
        }
        self._broadcast_to_session_clients(cast(JsonObject, message))

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
        stale_ids = [
            hardware_id
            for hardware_id in list(self.profile_state.grabbed_devices)
            if hardware_id not in configured_ids
        ]
        for hardware_id in stale_ids:
            log.info(f"Hardware removed for {hardware_id}, deactivating profile")
            await runtime_profiles.deactivate_profile(self, hardware_id)
            self.profile_state.resolved_devices.pop(hardware_id, None)

        await runtime_profiles.reevaluate_profiles(self)

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
                    await runtime_profiles.activate_initial_profiles(self)
                except Exception as e:
                    log.error(f"Failed to activate initial profiles: {e}")
                    traceback.print_exc()

                await self.client.wait_disconnected()

            except Exception as e:
                log.warning(f"Connection failed: {e}")
                was_connected = self._connected
                self._connected = False
                self.profile_state.grabbed_devices.clear()
                self.profile_state.grabbed_interfaces.clear()
                self.profile_state.grab_waiting_devices.clear()
                for task in list(self.profile_state.grab_retry_tasks.values()):
                    if not task.done():
                        task.cancel()
                self.profile_state.grab_retry_tasks.clear()
                self.profile_state.last_sent_mapping_signatures.clear()
                self.profile_state.last_sent_combo_signature = ""
                self.profile_state.active_profile_names.clear()
                self.profile_state.resolved_devices.clear()

                if was_connected:
                    self._broadcast_keyforged_status(False)

                if self.running:
                    try:
                        await asyncio.wait_for(self._retry_event.wait(), timeout=retry_delay)
                    except TimeoutError:
                        pass
                    retry_delay = min(retry_delay * 2, max_delay)


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
