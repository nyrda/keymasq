import argparse
import asyncio
import logging
import os
import signal
import socket
import stat
import sys
import time
from collections.abc import Awaitable, Callable
from typing import cast

from keymasq.common.asyncio_runtime import ensure_uvloop
from keymasq.common.coercion import coerce_int
from keymasq.common.ipc import CommandType
from keymasq.common.paths import (
    RECORDING_UNLOCK_RUNTIME_DIR,
    RUN_DIR,
    SECURITY_POLICY_PATH,
    SOCKET_PATH,
    STATE_DIR,
)
from keymasq.common.recording_guard import (
    UnlockStatus,
    resolve_macro_recording_status,
    resolve_unlock_status,
    runtime_unlock_path,
    write_unlock_expires_at,
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
from keymasq.keymasqd.macro_store import MacroStore
from keymasq.keymasqd.recording import RecordingManager
from keymasq.keymasqd.runtime import source_hiding
from keymasq.keymasqd.sleep import LogindSleepCoordinator
from keymasq.keymasqd.socket_server import ClientContext, SocketServer
from keymasq.keymasqd.timer_precision import set_timer_slack_ns

log = logging.getLogger("keymasqd")

type _GuardStatusCache = dict[int, tuple[float, bool, int, str]]
type _GuardStatusResolver = Callable[[int], UnlockStatus]
type _GuardStateLogger = Callable[[int, bool, str, int, str], None]


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
        self.recording_manager = RecordingManager()
        self.macro_store = MacroStore(STATE_DIR / "macros")
        self.capture_manager = CaptureManager()
        self.sleep_coordinator = LogindSleepCoordinator(
            self.device_manager.cancel_macro_playback_and_release_outputs,
        )
        self.socket_server: SocketServer | None = None
        self.running = False
        self._shutdown_event = asyncio.Event()
        self.verbosity = verbosity
        self.security_policy: SecurityPolicy | None = None
        self._unlock_cache: _GuardStatusCache = {}
        self._macro_recording_cache: _GuardStatusCache = {}
        self._unlock_cache_interval_s = 1.0
        self._unlock_state_last_logged: dict[int, tuple[bool, str]] = {}
        self._macro_recording_state_last_logged: dict[int, tuple[bool, str]] = {}
        self._recording_refresh_owners: dict[int, tuple[int, int]] = {}

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
            await self.socket_server.start()
            await self.device_manager.start_topology_watcher()
            await self.sleep_coordinator.start()

            sd_notify("READY=1")

            await self._shutdown_event.wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        if not self.running:
            return

        sd_notify("STOPPING=1")
        log.info("Stopping keymasqd")
        self.running = False

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
        await self._run_async_cleanup(
            "clear runtime unlocks",
            lambda: self._clear_all_runtime_unlocks_async(reason="daemon_stop"),
        )
        self._run_sync_cleanup(
            "shut down output devices",
            self.device_manager.shutdown_output_devices,
        )

    async def _run_async_cleanup(
        self,
        label: str,
        cleanup: Callable[[], Awaitable[object]],
    ) -> None:
        try:
            await cleanup()
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
        if client and self.security_policy:
            await self._ensure_sensitive_command_allowed(command_type, client)

        if self.verbosity >= 1:
            log.debug(f"Command: {command_type.value} -> {self._log_view(data)}")

        device_result = await daemon_device_commands.handle_device_command(
            cast(daemon_device_commands.DeviceCommandDaemon, self),
            command_type,
            data,
        )
        if device_result is not None:
            return device_result

        if command_type == CommandType.PING:
            return {"pong": True}

        if command_type == CommandType.MACRO_RECORDING_STATUS:
            fallback_uid = int(client.uid) if client is not None else os.getuid()
            uid = coerce_int(data.get("uid"), fallback_uid)
            return cast(JsonObject, await asyncio.to_thread(resolve_macro_recording_status, uid))

        if command_type == CommandType.RECORDING_UNLOCK_STATUS:
            fallback_uid = int(client.uid) if client is not None else os.getuid()
            uid = coerce_int(data.get("uid"), fallback_uid)
            return cast(JsonObject, await asyncio.to_thread(resolve_unlock_status, uid))

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

        if command_type == CommandType.REFRESH_RECORDING_UNLOCK:
            if client is None:
                raise PermissionError("recording_refresh_denied: missing client context")
            uid = int(client.uid)
            ttl = coerce_int(data.get("ttl", 60), 60)
            return self._refresh_runtime_unlock(uid, ttl, client)

        if command_type == CommandType.LOCK_RECORDING_UNLOCK:
            if client is None:
                raise PermissionError("recording_lock_denied: missing client context")
            uid = int(client.uid)
            cleanup = bool(data.get("cleanup", False))
            return self._lock_runtime_unlock(uid, client, cleanup=cleanup)

        raise ValueError(f"Unknown command: {command_type}")

    async def _ensure_sensitive_command_allowed(
        self,
        command_type: CommandType,
        client: ClientContext,
    ) -> None:
        if command_type == CommandType.START_RECORDING:
            await asyncio.to_thread(self._ensure_macro_recording_enabled, client.uid)
            return

        policy = self.security_policy
        tier1_commands = {
            CommandType.CAPTURE_BEGIN,
            CommandType.CAPTURE_READ,
            CommandType.CAPTURE_END,
            CommandType.CAPTURE_COMBO,
            CommandType.MACRO_SAVE_RECORDING,
            CommandType.DEVICE_INSPECTOR_START,
            CommandType.DEVICE_INSPECTOR_ENABLE_SUPPRESSION,
        }

        tier2_commands = {
            CommandType.MACRO_GET,
            CommandType.MACRO_CREATE,
            CommandType.MACRO_UPDATE,
            CommandType.MACRO_RENAME,
            CommandType.MACRO_DELETE,
        }

        is_tier1_command = command_type in tier1_commands
        if is_tier1_command:
            self._ensure_sensitive_owner(
                command_type,
                client,
                claim_if_missing=command_type != CommandType.CAPTURE_END,
            )

        if policy is None:
            return

        requires_unlock = is_tier1_command and policy.recording_unlock_required
        if command_type in tier2_commands and policy.macro_edit_requires_unlock:
            requires_unlock = True

        if not requires_unlock:
            return

        if not is_tier1_command:
            self._ensure_sensitive_owner(command_type, client)

        if command_type == CommandType.CAPTURE_END:
            return

        unlocked, expires_at, source = await asyncio.to_thread(
            self._recording_unlocked_for_uid,
            client.uid,
        )
        if unlocked:
            return

        if source == "none":
            raise PermissionError(
                "recording_locked: capture unlock required for input capture features"
            )
        raise PermissionError(f"recording_locked: unlock lease expired at {expires_at}")

    def _ensure_macro_recording_enabled(self, uid: int) -> None:
        enabled, expires_at, source = self._macro_recording_enabled_for_uid(uid)
        if enabled:
            return
        if source == "none":
            raise PermissionError("macro_recording_disabled: macro recording opt-in required")
        raise PermissionError(f"macro_recording_disabled: opt-in expired at {expires_at}")

    def _macro_recording_enabled_for_uid(self, uid: int) -> tuple[bool, int, str]:
        return self._guard_status_for_uid(
            uid,
            self._macro_recording_cache,
            resolve_macro_recording_status,
            self._log_macro_recording_state_change,
        )

    def _guard_status_for_uid(
        self,
        uid: int,
        cache: _GuardStatusCache,
        resolver: _GuardStatusResolver,
        log_state_change: _GuardStateLogger,
    ) -> tuple[bool, int, str]:
        now_mono = time.monotonic()
        now_wall = int(time.time())
        cached = cache.get(uid)

        if cached is not None:
            checked_mono, unlocked, expires_at, source = cached
            cache_fresh = (now_mono - checked_mono) < self._unlock_cache_interval_s
            if unlocked and cache_fresh and (expires_at == 0 or expires_at >= now_wall):
                return unlocked, expires_at, source

        status = resolver(uid)
        unlocked = bool(status.get("unlocked", False))
        expires_at = int(status.get("expires_at", 0) or 0)
        source = str(status.get("source", "none") or "none")
        cache[uid] = (now_mono, unlocked, expires_at, source)
        log_state_change(uid, unlocked, source, expires_at, "status_probe")
        return unlocked, expires_at, source

    def _log_macro_recording_state_change(
        self,
        uid: int,
        enabled: bool,
        source: str,
        expires_at: int,
        reason: str,
    ) -> None:
        current = (bool(enabled), str(source))
        previous = self._macro_recording_state_last_logged.get(int(uid))
        if previous == current:
            return

        self._macro_recording_state_last_logged[int(uid)] = current
        state = "ENABLED" if enabled else "DISABLED"
        log.info(
            (
                "Macro recording opt-in state changed uid=%s state=%s "
                "source=%s expires_at=%s reason=%s"
            ),
            int(uid),
            state,
            str(source),
            int(expires_at),
            str(reason),
        )

    def _recording_unlocked_for_uid(self, uid: int) -> tuple[bool, int, str]:
        return self._guard_status_for_uid(
            uid,
            self._unlock_cache,
            resolve_unlock_status,
            self._log_unlock_state_change,
        )

    def _log_unlock_state_change(
        self,
        uid: int,
        unlocked: bool,
        source: str,
        expires_at: int,
        reason: str,
    ) -> None:
        current = (bool(unlocked), str(source))
        previous = self._unlock_state_last_logged.get(int(uid))
        if previous == current:
            return

        self._unlock_state_last_logged[int(uid)] = current
        state = "UNLOCKED" if unlocked else "LOCKED"
        log.info(
            "Recording guard state changed uid=%s state=%s source=%s expires_at=%s reason=%s",
            int(uid),
            state,
            str(source),
            int(expires_at),
            str(reason),
        )

    def _ensure_sensitive_owner(
        self,
        command_type: CommandType,
        client: ClientContext,
        *,
        claim_if_missing: bool = True,
    ) -> None:
        self._ensure_recording_unlock_owner(
            int(client.uid),
            client,
            claim_if_missing=claim_if_missing,
            denial_message="sensitive_command_denied: caller is not active session owner",
            log_label="Sensitive command",
            command_type=command_type,
        )

    def _ensure_recording_unlock_owner(
        self,
        uid: int,
        client: ClientContext,
        *,
        claim_if_missing: bool,
        denial_message: str,
        log_label: str,
        missing_owner_message: str | None = None,
        command_type: CommandType | None = None,
    ) -> None:
        uid = int(uid)
        owner = self._recording_refresh_owners.get(uid)
        if owner is None:
            if not claim_if_missing:
                raise PermissionError(missing_owner_message or denial_message)
            self._recording_refresh_owners[uid] = (
                int(client.pid),
                int(client.connection_id),
            )
            return

        owner_pid, owner_connection_id = owner
        if owner_pid == int(client.pid) and owner_connection_id == int(client.connection_id):
            return

        message = (
            f"{log_label} owner mismatch uid=%s pid=%s connection=%s "
            "owner_pid=%s owner_connection=%s"
        )
        args: tuple[object, ...] = (
            client.uid,
            client.pid,
            client.connection_id,
            owner_pid,
            owner_connection_id,
        )
        if command_type is not None:
            message = f"{message} command=%s"
            args = (*args, command_type.value)

        log.warning(message, *args)
        raise PermissionError(denial_message)

    def _refresh_runtime_unlock(self, uid: int, ttl: int, client: ClientContext) -> JsonObject:
        self._ensure_recording_unlock_owner(
            uid,
            client,
            claim_if_missing=True,
            denial_message="recording_refresh_denied: caller is not active session owner",
            log_label="Recording refresh",
        )

        status = resolve_unlock_status(uid)
        unlocked = bool(status.get("unlocked", False))
        source = str(status.get("source", "none") or "none")
        if not unlocked or source != "runtime":
            raise PermissionError("recording_refresh_denied: runtime unlock lease is not active")

        ttl_value = max(1, int(ttl))
        expires_at = int(time.time()) + ttl_value
        write_unlock_expires_at(
            runtime_unlock_path(uid),
            expires_at,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
            mode=0o600,
        )
        self._unlock_cache[uid] = (time.monotonic(), True, expires_at, "runtime")
        self._log_unlock_state_change(uid, True, "runtime", expires_at, reason="refresh")
        return {
            "status": "ok",
            "uid": int(uid),
            "source": "runtime",
            "expires_at": int(expires_at),
            "owner_pid": int(client.pid),
        }

    def _clear_runtime_unlock(self, uid: int, *, reason: str) -> None:
        path = runtime_unlock_path(uid)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

        self._record_runtime_unlock_cleared(uid, reason=reason)

    async def _clear_runtime_unlock_async(self, uid: int, *, reason: str) -> None:
        path = runtime_unlock_path(uid)
        try:
            await asyncio.get_running_loop().run_in_executor(None, path.unlink)
        except FileNotFoundError:
            pass

        self._record_runtime_unlock_cleared(uid, reason=reason)

    def _record_runtime_unlock_cleared(self, uid: int, *, reason: str) -> None:
        self._unlock_cache[uid] = (time.monotonic(), False, 0, "none")
        self._recording_refresh_owners.pop(uid, None)
        self._log_unlock_state_change(uid, False, "none", 0, reason=reason)

    def _runtime_unlock_file_uids(self) -> set[int]:
        runtime_uids: set[int] = set()
        try:
            for path in RECORDING_UNLOCK_RUNTIME_DIR.glob("recording-unlock-*"):
                suffix = path.name.removeprefix("recording-unlock-")
                try:
                    runtime_uids.add(int(suffix))
                except ValueError:
                    continue
        except FileNotFoundError:
            pass
        return runtime_uids

    async def _runtime_unlock_file_uids_async(self) -> set[int]:
        return await asyncio.get_running_loop().run_in_executor(
            None,
            self._runtime_unlock_file_uids,
        )

    def _clear_all_runtime_unlocks(self, *, reason: str) -> None:
        runtime_uids = {int(uid) for uid in self._recording_refresh_owners}
        runtime_uids.update(self._runtime_unlock_file_uids())

        for uid in sorted(runtime_uids):
            try:
                self._clear_runtime_unlock(uid, reason=reason)
            except OSError as exc:
                log.warning(
                    "Failed to clear runtime unlock uid=%s during %s: %s",
                    uid,
                    reason,
                    exc,
                )

        self._recording_refresh_owners.clear()

    async def _clear_all_runtime_unlocks_async(self, *, reason: str) -> None:
        runtime_uids = {int(uid) for uid in self._recording_refresh_owners}
        runtime_uids.update(await self._runtime_unlock_file_uids_async())

        for uid in sorted(runtime_uids):
            try:
                await self._clear_runtime_unlock_async(uid, reason=reason)
            except OSError as exc:
                log.warning(
                    "Failed to clear runtime unlock uid=%s during %s: %s",
                    uid,
                    reason,
                    exc,
                )

        self._recording_refresh_owners.clear()

    def _clear_runtime_unlock_for_client(
        self,
        client: ClientContext,
        *,
        reason: str,
    ) -> None:
        uid = int(client.uid)
        owner = self._recording_refresh_owners.get(uid)
        if owner != (int(client.pid), int(client.connection_id)):
            return

        try:
            self._clear_runtime_unlock(uid, reason=reason)
        except OSError as exc:
            log.warning(
                "Failed to clear runtime unlock uid=%s during %s: %s",
                uid,
                reason,
                exc,
            )

    async def _clear_runtime_unlock_for_client_async(
        self,
        client: ClientContext,
        *,
        reason: str,
    ) -> None:
        uid = int(client.uid)
        owner = self._recording_refresh_owners.get(uid)
        if owner != (int(client.pid), int(client.connection_id)):
            return

        try:
            await self._clear_runtime_unlock_async(uid, reason=reason)
        except OSError as exc:
            log.warning(
                "Failed to clear runtime unlock uid=%s during %s: %s",
                uid,
                reason,
                exc,
            )

    def _lock_runtime_unlock(
        self,
        uid: int,
        client: ClientContext,
        *,
        cleanup: bool = False,
    ) -> JsonObject:
        if not cleanup:
            self._ensure_recording_unlock_owner(
                uid,
                client,
                claim_if_missing=False,
                denial_message="recording_lock_denied: caller is not active session owner",
                missing_owner_message="recording_lock_denied: no active session owner",
                log_label="Recording lock",
            )

        self._clear_runtime_unlock(
            uid,
            reason="disconnect_cleanup" if cleanup else "explicit_lock",
        )
        return {
            "status": "ok",
            "uid": int(uid),
            "source": "runtime",
            "locked": True,
        }

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

    async def _on_client_disconnect(self, client: ClientContext | None = None) -> None:
        if client is None and self.socket_server is not None:
            client = self.socket_server.owner_context

        log.info("Client disconnected, releasing all devices")
        if client is not None:
            await self._run_async_cleanup(
                "clear runtime unlock for client",
                lambda: self._clear_runtime_unlock_for_client_async(
                    client,
                    reason="session_disconnect",
                ),
            )
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
