import argparse
import asyncio
import contextlib
import logging
import os
import signal
import socket
import stat
import sys
from collections.abc import Awaitable, Callable
from typing import cast

from keymasq.common.asyncio_runtime import ensure_uvloop
from keymasq.common.ipc import CommandType
from keymasq.common.paths import (
    HANDOFF_SOCKET_PATH,
    RUN_DIR,
    SECURITY_POLICY_PATH,
    SOCKET_PATH,
    STATE_DIR,
)
from keymasq.common.recording_policy import (
    MACRO_RECORDING_DISABLED_ERROR_CODE,
    MACRO_RECORDING_DISABLED_MESSAGE,
)
from keymasq.common.security import (
    PeerCredentials,
    SecurityPolicy,
    SecurityPolicyError,
    load_security_policy,
    uid_allowed,
)
from keymasq.common.types import JsonObject
from keymasq.keymasqd import (
    daemon_capture_commands,
    daemon_device_commands,
    daemon_macro_commands,
)
from keymasq.keymasqd.capture_manager import CaptureManager
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.fd_handoff import FdHandoffServer
from keymasq.keymasqd.hardware_masking import MASK_COMMANDS, HardwareMasking
from keymasq.keymasqd.macro_store import MacroStore
from keymasq.keymasqd.recording import RecordingManager
from keymasq.keymasqd.runtime import source_hiding
from keymasq.keymasqd.sleep import LogindSleepCoordinator
from keymasq.keymasqd.socket_server import ClientContext, SocketServer
from keymasq.keymasqd.timer_precision import set_timer_slack_ns

log = logging.getLogger("keymasqd")


def sd_notify(state: str) -> None:
    notify_socket = os.environ.get("NOTIFY_SOCKET")
    if not notify_socket:
        return
    if notify_socket.startswith("@"):
        notify_socket = "\0" + notify_socket[1:]

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(notify_socket)
            sock.sendall(f"{state}\n".encode())
    except (OSError, RuntimeError):
        log.debug("Failed to send sd_notify state", exc_info=True)


class Daemon:
    def __init__(self, verbosity: int = 0) -> None:
        self.device_manager = DeviceManager(verbosity=verbosity)
        self.hardware_masking = HardwareMasking(self.device_manager)
        self.device_manager.masking_recovery = self.hardware_masking.recover_if_needed
        self.recording_manager = RecordingManager()
        self.macro_store = MacroStore(STATE_DIR / "macros")
        self.capture_manager = CaptureManager()
        self.sleep_coordinator = LogindSleepCoordinator(
            self.device_manager.neutralize_runtime,
            pause_runtime=self.device_manager.pause_runtime_input,
            resume_runtime=self.device_manager.resume_runtime_input,
            cleanup_hardware=self.hardware_masking.suspend,
            resume_hardware=self.hardware_masking.resume,
        )
        self.socket_server: SocketServer | None = None
        self.fd_handoff: FdHandoffServer | None = None
        self.running = False
        self._shutdown_event = asyncio.Event()
        self._watchdog_task: asyncio.Task[None] | None = None
        self.verbosity = verbosity
        self.security_policy: SecurityPolicy | None = None

    async def start(self) -> None:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        self._secure_run_dir()
        await source_hiding.reconcile_all()
        self.security_policy = load_security_policy(SECURITY_POLICY_PATH)
        self.device_manager.macro_exec_timeout_max_ms = int(
            self.security_policy.macro_exec_timeout_max_ms
        )
        self.device_manager.emergency_cancel_combo_enabled = bool(
            self.security_policy.emergency_cancel_combo_enabled
        )
        self.recording_manager.macro_recording_time_limit = int(
            self.security_policy.macro_recording_time_limit
        )
        await asyncio.to_thread(self._prepare_macro_store)
        await self.recording_manager.load_persisted_slot_recordings()
        log.info(
            "Security policy loaded from %s",
            SECURITY_POLICY_PATH,
        )

        self._cleanup_socket_path()

        self.socket_server = SocketServer(
            str(SOCKET_PATH),
            self._handle_command,
            self._on_client_disconnect,
            socket_mode=0o666,
            peer_validator=self._validate_peer,
            single_owner=True,
        )

        self.device_manager.broadcast_callback = self.socket_server.broadcast_event
        self.recording_manager.broadcast_callback = self.socket_server.broadcast_event
        self.device_manager.recording_manager = self.recording_manager
        self.device_manager.macro_store = self.macro_store

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._signal_handler)

        log.info(f"Starting keymasqd (socket: {SOCKET_PATH})")

        self.running = True
        try:
            self.device_manager.initialize_output_devices()
            self.fd_handoff = FdHandoffServer(HANDOFF_SOCKET_PATH)
            await self.fd_handoff.start()
            await self.socket_server.start()
            await self.device_manager.start_topology_watcher()
            await self.sleep_coordinator.start()

            sd_notify("READY=1")
            self._watchdog_task = asyncio.create_task(self._watchdog(), name="daemon-watchdog")

            await self._shutdown_event.wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        if not self.running:
            return

        sd_notify("STOPPING=1")
        log.info("Stopping keymasqd")
        self.running = False
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog_task
            self._watchdog_task = None

        # Foreground daemons also restore access on a clean exit. The installed
        # service additionally runs privileged cleanup after crashes or hangs.
        await self._run_async_cleanup("restore hardware masks", self.hardware_masking.close)
        await self._run_async_cleanup(
            "stop logind sleep coordination",
            self.sleep_coordinator.stop,
        )

        if self.socket_server:
            await self._run_async_cleanup("stop socket server", self.socket_server.stop)
        else:
            self._cleanup_socket_path()

        await self._run_async_cleanup(
            "abort active recording",
            self.recording_manager.abort,
        )
        await self._run_async_cleanup(
            "close active captures",
            lambda: asyncio.to_thread(self.capture_manager.close_all),
        )

        await self._run_async_cleanup(
            "stop topology watcher",
            self.device_manager.stop_topology_watcher,
        )
        await self._run_async_cleanup(
            "cancel macro playback",
            self.device_manager.cancel_macro_playback,
        )
        await self._run_async_cleanup(
            "release all devices",
            self.device_manager.release_all_devices,
        )
        if self.fd_handoff is not None:
            await self._run_async_cleanup("stop privileged handoffs", self.fd_handoff.stop)
        self._run_sync_cleanup(
            "shut down output devices",
            self.device_manager.shutdown_output_devices,
        )

    async def _watchdog(self) -> None:
        """Feed systemd from the same event loop that processes physical input."""
        try:
            interval = int(os.environ.get("WATCHDOG_USEC", "0")) / 1_000_000
            pid = int(os.environ.get("WATCHDOG_PID", str(os.getpid())))
        except ValueError:
            return
        if interval <= 0 or pid != os.getpid():
            return
        while self.running:
            # A blocked loop cannot reach this await or send another heartbeat.
            await asyncio.sleep(interval / 3)
            # Hardware jobs have their own deadlines. Awaiting one, or a lock
            # it owns, does not mean this input event loop is unresponsive.
            sd_notify("WATCHDOG=1")

    async def _run_async_cleanup(
        self,
        label: str,
        cleanup: Callable[[], Awaitable[object]],
    ) -> None:
        try:
            await cleanup()
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            # A cancelled child must not cancel the remaining daemon cleanup.
            log.exception("Cancelled child while attempting to %s during daemon cleanup", label)
        except Exception:
            log.exception("Failed to %s during daemon cleanup", label)

    def _run_sync_cleanup(self, label: str, cleanup: Callable[[], object]) -> None:
        try:
            cleanup()
        except Exception:
            log.exception("Failed to %s during daemon cleanup", label)

    def _prepare_macro_store(self) -> None:
        self.macro_store.ensure()
        self._register_internal_macros()
        self.recording_manager.cleanup_spool_dir()

    def _register_internal_macros(self) -> None:
        ev_rel = 2
        rel_x = 0

        self.macro_store.register_internal(
            "__cursor_position_trigger",
            events=[
                {"device_type": "mouse", "type": ev_rel, "code": rel_x, "value": 1, "t_us": 10000},
                {
                    "device_type": "mouse",
                    "type": ev_rel,
                    "code": rel_x,
                    "value": -1,
                    "t_us": 20000,
                },
            ],
            duration_ms=30,
            device_types=["mouse"],
        )
        log.debug("Registered internal macros: __cursor_position_trigger")

    def _signal_handler(self) -> None:
        log.info("Received shutdown signal")
        self._shutdown_event.set()

    async def _handle_command(
        self,
        command_type: CommandType,
        data: JsonObject,
        client: ClientContext | None = None,
    ) -> JsonObject:
        if command_type == CommandType.START_RECORDING:
            self._ensure_macro_recording_allowed()

        if self.verbosity >= 1:
            log.debug(f"Command: {command_type.value} -> {self._log_view(data)}")

        if command_type in MASK_COMMANDS:
            if client is None:
                raise PermissionError("Hardware masking requires an authenticated user session")
            return await self.hardware_masking.handle(command_type, data, uid=client.uid)

        device_result = await daemon_device_commands.handle_device_command(
            cast(daemon_device_commands.DeviceCommandDaemon, self),
            command_type,
            data,
        )
        if device_result is not None:
            return device_result

        if command_type == CommandType.PING:
            return {"pong": True}

        macro_result = await daemon_macro_commands.handle_macro_command(
            cast(daemon_macro_commands.MacroCommandDaemon, self),
            command_type,
            data,
        )
        if macro_result is not None:
            return macro_result

        capture_result = await daemon_capture_commands.handle_capture_command(
            cast(daemon_capture_commands.CaptureCommandDaemon, self),
            command_type,
            data,
        )
        if capture_result is not None:
            return capture_result

        raise ValueError(f"Unknown command: {command_type}")

    def _ensure_macro_recording_allowed(self) -> None:
        if self.security_policy is not None and not self.security_policy.macro_recording_allowed:
            raise PermissionError(
                f"{MACRO_RECORDING_DISABLED_ERROR_CODE}: {MACRO_RECORDING_DISABLED_MESSAGE}"
            )

    def _log_view(self, data: JsonObject) -> JsonObject:
        def sanitize(value: object) -> object:
            if isinstance(value, dict):
                out: JsonObject = {}
                items = cast(dict[object, object], value)
                for raw_key, raw_value in items.items():
                    key = str(raw_key)
                    if key in ("macro_events", "events") and isinstance(raw_value, list):
                        events = cast(list[object], raw_value)
                        out[key] = f"<{len(events)} events>"
                    else:
                        out[key] = sanitize(raw_value)
                return out
            if isinstance(value, list):
                items = cast(list[object], value)
                return [sanitize(item) for item in items]
            return value

        view: JsonObject = {}
        for key, value in data.items():
            view[key] = sanitize(value)
        return view

    async def _on_client_disconnect(self) -> None:
        await self._run_async_cleanup(
            "restore hardware after owner loss",
            lambda: self.hardware_masking.close(restore_hardware=self.running),
        )
        log.info("Client disconnected, releasing all devices")
        await self._run_async_cleanup(
            "abort active recording",
            self.recording_manager.abort,
        )
        await self._run_async_cleanup(
            "discard pending recordings",
            self.recording_manager.discard_all_pending_recordings,
        )
        await self._run_async_cleanup(
            "end active captures",
            lambda: asyncio.to_thread(self.capture_manager.close_all),
        )
        await self._run_async_cleanup(
            "release all devices after client disconnect",
            self.device_manager.release_all_devices,
        )

    def _secure_run_dir(self) -> None:
        try:
            os.chmod(RUN_DIR, 0o755)
        except OSError as exc:
            raise RuntimeError(f"Failed to set run directory mode on {RUN_DIR}: {exc}") from exc

        mode = RUN_DIR.stat().st_mode
        if mode & stat.S_IWOTH:
            raise RuntimeError(
                f"Insecure run directory permissions on {RUN_DIR}: {mode & 0o777:04o}"
            )

    def _cleanup_socket_path(self) -> None:
        try:
            SOCKET_PATH.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(f"Failed to remove daemon socket path {SOCKET_PATH}: {exc}") from exc

    def _validate_peer(self, peer: PeerCredentials) -> tuple[bool, str]:
        if self.security_policy is None:
            return False, "security policy not loaded"

        if not uid_allowed(peer.uid, self.security_policy.daemon_allowed_uids):
            return False, f"uid {peer.uid} is not allowed by daemon policy"

        return True, "peer uid allowed"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="keymasqd",
        description="Keymasq Input Remapping Daemon",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Enable debug logging (-v), trace logging (-vv), or raw hardware event tracing (-vvv)",
    )
    parser.add_argument(
        "--allow-root",
        action="store_true",
        help="Allow running as root (not recommended)",
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
    # Tighten kernel timer slack on the main thread before any worker threads
    # are spawned so they inherit the tighter wakeup resolution. This measurably
    # reduces jitter on the sub-millisecond asyncio.sleep() deadlines used by
    # macro replay.
    set_timer_slack_ns(logger=log)

    if args.verbose >= 2:
        log.info("Trace logging enabled (-vv)")
    if args.verbose >= 3:
        log.info("Raw hardware event tracing enabled (-vvv)")

    if os.geteuid() == 0 and not args.allow_root:
        log.error("keymasqd should not run as root. Use --allow-root to override.")
        sys.exit(1)

    if os.geteuid() == 0:
        log.warning("Running as root - this is not recommended for security")

    try:
        daemon = Daemon(verbosity=args.verbose)
        asyncio.run(daemon.start())
    except KeyboardInterrupt:
        pass
    except SecurityPolicyError as exc:
        log.error("%s", exc)
        sys.exit(1)
    except Exception:
        log.exception("Fatal error")
        sys.exit(1)


if __name__ == "__main__":
    main()
