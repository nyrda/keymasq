import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path

from keymasq.common.asyncio_runtime import ensure_uvloop
from keymasq.common.devices import resolve_evdev_code
from keymasq.common.ipc import Command, CommandType
from keymasq.common.model.hardware import ButtonDefinition
from keymasq.common.paths import (
    CONFIG_DIR,
    SECURITY_POLICY_PATH,
    SESSION_SOCKET_PATH,
)
from keymasq.common.security import (
    PeerCredentials,
    SecurityPolicy,
    SecurityPolicyError,
    load_security_policy,
)
from keymasq.session.action_handler import ActionHandler
from keymasq.session.analog_controls import AnalogControlManager
from keymasq.session.client import KeymasqdClient
from keymasq.session.dbus import SessionDBus
from keymasq.session.hardware import HardwareManager
from keymasq.session.motion_controls import MotionControlManager
from keymasq.session.mpris import MprisController, MprisDBusError
from keymasq.session.profile.manager import ProfileManager
from keymasq.session.settings import load_global_settings
from keymasq.session.superkeys import SuperkeyManager

from . import compositor, events, recording_device_selection
from .common import JsonObject
from .playback import PlaybackRequests
from .profile import application, coordinator, runtime_state
from .service.connection import DaemonConnectionMixin
from .service.server import SessionServerMixin
from .service.watcher import ConfigWatcherMixin
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


class SessionManager(SessionServerMixin, ConfigWatcherMixin, DaemonConnectionMixin):
    RECORDING_SETTINGS_PATH = CONFIG_DIR / "recording_settings.toml"
    MAX_SESSION_CLIENT_BUFFER_BYTES = 16 * 1024 * 1024
    SESSION_CLIENT_CLOSE_TIMEOUT_S = 0.5
    RELOAD_DEBOUNCE_S = 0.5

    def __init__(self, verbosity: int = 0) -> None:
        async def _client_event_handler(event_type: CommandType, data: JsonObject) -> None:
            await events.handle_event(self, event_type, data)

        def _prepare_client_event(event_type: CommandType, data: JsonObject) -> JsonObject:
            return events.prepare_event(self, event_type, data)

        self.client: KeymasqdClient = KeymasqdClient(
            _client_event_handler,
            event_preprocessor=_prepare_client_event,
        )
        self.superkeys = SuperkeyManager()
        self.analog_controls = AnalogControlManager()
        self.motion_controls = MotionControlManager()
        self.profiles = ProfileManager(
            superkey_manager=self.superkeys,
            analog_control_manager=self.analog_controls,
            motion_control_manager=self.motion_controls,
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
        self._config_reload_coalesce_until = 0.0
        self.config_watch_fd: int | None = None
        self.config_watch_watches: dict[int, Path] = {}
        self._registered_signals: set[signal.Signals] = set()
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
        self.playback_requests = PlaybackRequests(self)
        self.capture_state = CaptureRuntimeState()

        self.exec_state = ExecRuntimeState()
        self.event_state = EventRuntimeState()
        self.device_inspector_state = DeviceInspectorRuntimeState()
        self.recording_state = RecordingRuntimeState()
        self.unlock_state = UnlockRuntimeState()
        recording_device_selection.load_recording_settings_from_disk(self)
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
        try:
            loop = asyncio.get_event_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self._signal_handler)
                self._registered_signals.add(sig)
            for sig in (signal.SIGHUP,):
                loop.add_signal_handler(sig, self._reload_handler)
                self._registered_signals.add(sig)

            log.info("Starting keymasq-session")

            await self._start_session_server()
            try:
                await self.mpris_controller.start()
            except MprisDBusError:
                log.debug("MPRIS controller startup deferred", exc_info=True)

            self.connect_task = asyncio.create_task(self.connect_loop())
            self._start_config_watcher()
            self.compositor_state.supervisor_task = asyncio.create_task(
                compositor.compositor_supervisor_loop(self)
            )

            await self._shutdown_event.wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        if not self.running:
            return

        log.info("Stopping keymasq-session")
        self.running = False

        loop = asyncio.get_event_loop()
        for sig in self._registered_signals:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError):
                log.debug("Failed to remove signal handler for %s", sig, exc_info=True)
        self._registered_signals.clear()

        self._shutdown_event.set()
        await runtime_state.cancel_all_grab_retries(self)

        if self.compositor_state.supervisor_task:
            self.compositor_state.supervisor_task.cancel()
            with contextlib.suppress(*_TASK_SHUTDOWN_ERRORS):
                await self.compositor_state.supervisor_task
            self.compositor_state.supervisor_task = None

        await events.cancel_event_tasks(self)

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

        await compositor.stop_window_listener(self)
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

        await self.playback_requests.shutdown()
        await self.client.disconnect()
        await events.cancel_event_tasks(self)
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

    def _signal_handler(self) -> None:
        log.info("Received shutdown signal")
        self._shutdown_event.set()
        self._retry_event.set()

    def _reload_handler(self) -> None:
        if self.reload_pending:
            log.debug("Reload already pending, skipping")
            return

        if self.reload_task and not self.reload_task.done():
            log.debug("Reload task still running, dropping reload request")
            return

        self.reload_pending = True
        log.info("Received reload signal (SIGHUP)")
        self.reload_task = asyncio.create_task(self.reload_profiles())

    async def reload_profiles(self) -> bool:
        try:
            await asyncio.sleep(self.RELOAD_DEBOUNCE_S)
        finally:
            self.reload_pending = False

        try:
            await asyncio.to_thread(self.reload_config_from_disk)
        except Exception as exc:
            log.exception("Failed to reload user config from disk; keeping previous active config")
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
        runtime_state.invalidate_runtime_payload_signatures(self)

        configured_ids = set(self.hardware.list_hardware_ids())
        stale_ids = [
            hardware_id
            for hardware_id in list(self.profile_state.grabbed_devices)
            if hardware_id not in configured_ids
        ]
        for hardware_id in stale_ids:
            log.info(f"Hardware removed for {hardware_id}, deactivating profile")
            await application.deactivate_profile(self, hardware_id, immediate=True)
            self.profile_state.resolved_devices.pop(hardware_id, None)

        await coordinator.reevaluate_profiles(self, reason="config reload")
        return True

    async def on_window_change(
        self, window_class: str, window_title: str, window_tags: list[str]
    ) -> None:
        await compositor.on_window_change(self, window_class, window_title, window_tags)

    def send_notification(self, title: str, message: str) -> None:
        log.info("Notification: %s: %s", title, message)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        events.create_event_task(
            self,
            self._send_notification_async(title, message),
            name="notification",
        )

    async def _send_notification_async(self, title: str, message: str) -> None:
        try:
            await self.dbus.notify(title, message, app_name="keymasq", timeout_ms=5000)
        except Exception:
            log.exception("Failed to send notification")


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


if __name__ == "__main__":
    main()
