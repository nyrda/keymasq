import argparse
import asyncio
import logging
import os
import signal
import socket
import stat
import sys
import time
from collections.abc import Sequence
from typing import Protocol, cast

from keymasq.common.asyncio_runtime import ensure_uvloop
from keymasq.common.ipc import CommandType
from keymasq.common.paths import (
    RECORDING_UNLOCK_RUNTIME_DIR,
    RUN_DIR,
    SECURITY_POLICY_PATH,
    SOCKET_PATH,
    STATE_DIR,
)
from keymasq.common.recording_guard import (
    resolve_unlock_status,
    runtime_unlock_path,
    write_unlock_expires_at,
)
from keymasq.common.security import (
    PeerCredentials,
    SecurityPolicy,
    command_allowed,
    load_security_policy,
    uid_allowed,
)
from keymasq.keymasqd import (
    daemon_capture_commands,
    daemon_device_commands,
    daemon_macro_commands,
)
from keymasq.keymasqd.capture_manager import CaptureManager
from keymasq.keymasqd.daemon_helpers import (
    JsonObject,
    JsonObjectList,
    int_like,
)
from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.macro_store import MacroStore
from keymasq.keymasqd.recording import RecordingManager
from keymasq.keymasqd.socket_server import ClientContext, SocketServer
from keymasq.keymasqd.timer_precision import set_timer_slack_ns

log = logging.getLogger("keymasqd")


class _GrabbedDeviceRef(Protocol):
    path: str


class _DaemonDeviceManager(Protocol):
    broadcast_callback: object | None
    recording_manager: object | None
    macro_store: object | None
    macro_exec_timeout_max_ms: int
    emergency_cancel_combo_enabled: bool
    grabbed_devices: dict[str, list[_GrabbedDeviceRef]]

    def initialize_output_devices(self) -> None: ...

    def shutdown_output_devices(self) -> None: ...

    async def start_topology_watcher(self) -> None: ...

    async def stop_topology_watcher(self) -> None: ...

    async def cancel_macro_playback(self) -> JsonObject: ...

    async def release_all_devices(self) -> None: ...

    async def grab_device(
        self,
        hardware_id: str,
        evdev_paths: list[str],
        button_map: dict[str, str],
        button_codes: dict[str, int] | None = None,
        button_values: dict[str, int] | None = None,
        force_grab_unmapped: bool = False,
    ) -> JsonObject: ...

    async def release_device(
        self, hardware_id: str, immediate: bool = False, grace_s: float | None = None
    ) -> JsonObject: ...

    async def set_mapping(self, hardware_id: str, mapping: JsonObject) -> JsonObject: ...

    async def set_combos(self, combos: Sequence[object]) -> JsonObject: ...

    def set_cursor_position_backend(self, enabled: bool) -> JsonObject: ...

    async def set_cursor_position(self, x: int, y: int) -> JsonObject: ...

    def complete_cursor_position_request(
        self, request_id: str, *, ok: bool, message: str = ""
    ) -> JsonObject: ...

    async def list_devices(self) -> JsonObject: ...

    async def play_macro(
        self,
        macro_events: JsonObjectList,
        macro_name: str = "",
        replay_mouse_movement: bool = True,
        replay_mouse_clicks: bool = True,
        speed: float = 1.0,
        loop_mode: str = "none",
        loop_count: int = 1,
        loop_stop_behavior: str = "finish_run",
        move_to_start: bool = False,
        start_x: int = 0,
        start_y: int = 0,
        block_mouse_movement: bool = False,
        source_device: str = "",
        source_button: str = "",
        trigger_value: int = 1,
    ) -> JsonObject: ...

    def begin_combo_capture(
        self, token: str, hardware_ids: set[str], notify_event: asyncio.Event
    ) -> None: ...

    def end_combo_capture(self, token: str) -> None: ...

    def read_combo_capture(self, token: str) -> JsonObject: ...

    async def set_diagnostics(self, enabled: bool, interval: float) -> JsonObject: ...

    def complete_macro_exec_wait(self, wait_id: str, returncode: int) -> JsonObject: ...


class _DaemonRecordingManager(Protocol):
    broadcast_callback: object | None

    async def start(
        self,
        devices: JsonObjectList,
        include_mouse_movement: bool = False,
        include_mouse_clicks: bool = False,
    ) -> JsonObject: ...

    async def stop(self) -> JsonObject: ...

    async def discard_all_pending_recordings(self) -> None: ...

    def cleanup_spool_dir(self, *, older_than_s: float | None = None) -> None: ...


class _DaemonMacroStore(Protocol):
    def ensure(self) -> None: ...

    def register_internal(self, name: str, events: JsonObjectList, **extra: object) -> None: ...

    def get(self, name: str) -> JsonObject: ...

    def list_meta(self) -> JsonObjectList: ...

    def create(self, payload: JsonObject) -> JsonObject: ...

    def update(
        self, name: str, payload: JsonObject, expected_revision: int | None
    ) -> JsonObject: ...

    def rename(self, old_name: str, new_name: str, expected_revision: int | None) -> JsonObject: ...

    def delete(self, name: str, expected_revision: int | None) -> None: ...


class _DaemonCaptureManager(Protocol):
    def begin(self, hardware_id: str) -> JsonObject: ...

    def read(self, token: str) -> JsonObject: ...

    def begin_combo(self, *args: object, **kwargs: object) -> JsonObject: ...

    def authorize_combo_capture(self) -> object: ...

    def register_combo_notifier(
        self, token: str, loop: asyncio.AbstractEventLoop, notify_event: asyncio.Event
    ) -> None: ...

    def read_combo_nowait(self, token: str) -> JsonObject: ...

    def end(self, token: str) -> JsonObject: ...

def sd_notify(state: str) -> None:
    notify_socket = os.environ.get("NOTIFY_SOCKET")
    if not notify_socket:
        return

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.connect(notify_socket)
        sock.sendall(f"{state}\n".encode())
        sock.close()
    except Exception:
        pass


class Daemon:
    def __init__(self, verbosity: int = 0) -> None:
        self.device_manager = cast(_DaemonDeviceManager, DeviceManager(verbosity=verbosity))
        self.recording_manager = cast(_DaemonRecordingManager, RecordingManager())
        self.macro_store = cast(_DaemonMacroStore, MacroStore(STATE_DIR / "macros"))
        self.capture_manager = cast(_DaemonCaptureManager, CaptureManager())
        self.socket_server: SocketServer | None = None
        self.running = False
        self._shutdown_event = asyncio.Event()
        self.verbosity = verbosity
        self.security_policy: SecurityPolicy | None = None
        self._unlock_cache: dict[int, tuple[float, bool, int, str]] = {}
        self._unlock_cache_interval_s = 1.0
        self._unlock_state_last_logged: dict[int, tuple[bool, str]] = {}
        self._recording_refresh_owners: dict[int, tuple[int, int]] = {}

    async def start(self) -> None:
        self.running = True

        RUN_DIR.mkdir(parents=True, exist_ok=True)
        self._secure_run_dir()
        self.security_policy = load_security_policy(SECURITY_POLICY_PATH)
        self.device_manager.macro_exec_timeout_max_ms = int(
            self.security_policy.macro_exec_timeout_max_ms
        )
        self.device_manager.emergency_cancel_combo_enabled = bool(
            self.security_policy.emergency_cancel_combo_enabled
        )
        await asyncio.to_thread(self._prepare_macro_store)
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

        self.device_manager.initialize_output_devices()
        await self.socket_server.start()
        await self.device_manager.start_topology_watcher()

        sd_notify("READY=1")

        try:
            await self._shutdown_event.wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        if not self.running:
            return

        sd_notify("STOPPING=1")
        log.info("Stopping keymasqd")
        self.running = False

        await self.device_manager.stop_topology_watcher()
        await self.device_manager.cancel_macro_playback()
        await self.device_manager.release_all_devices()
        self.device_manager.shutdown_output_devices()

        if self.socket_server:
            await self.socket_server.stop()

        try:
            self._cleanup_socket_path()
        except RuntimeError as exc:
            log.warning("Failed to remove daemon socket path %s: %s", SOCKET_PATH, exc)

    def _prepare_macro_store(self) -> None:
        self.macro_store.ensure()
        self._register_internal_macros()
        self.recording_manager.cleanup_spool_dir()

    def _register_internal_macros(self) -> None:
        ev_rel = 2
        ev_key = 1
        rel_x = 0
        btn_left = 272

        self.macro_store.register_internal(
            "__slurp_trigger",
            events=[
                {"device_type": "mouse", "type": ev_rel, "code": rel_x, "value": 1, "t_us": 10000},
                {
                    "device_type": "mouse",
                    "type": ev_rel,
                    "code": rel_x,
                    "value": -1,
                    "t_us": 20000,
                },
                {
                    "device_type": "mouse",
                    "type": ev_key,
                    "code": btn_left,
                    "value": 1,
                    "t_us": 30000,
                },
                {
                    "device_type": "mouse",
                    "type": ev_key,
                    "code": btn_left,
                    "value": 0,
                    "t_us": 40000,
                },
            ],
            duration_ms=50,
            device_types=["mouse"],
        )
        log.debug("Registered internal macros: __slurp_trigger")

    def _signal_handler(self) -> None:
        log.info("Received shutdown signal")
        self._shutdown_event.set()

    def _device_command_daemon(self) -> daemon_device_commands.DeviceCommandDaemon:
        return cast(daemon_device_commands.DeviceCommandDaemon, self)

    def _macro_command_daemon(self) -> daemon_macro_commands.MacroCommandDaemon:
        return cast(daemon_macro_commands.MacroCommandDaemon, self)

    def _capture_command_daemon(self) -> daemon_capture_commands.CaptureCommandDaemon:
        return cast(daemon_capture_commands.CaptureCommandDaemon, self)

    async def _handle_command(
        self,
        command_type: CommandType,
        data: JsonObject,
        client: ClientContext | None = None,
    ) -> JsonObject:
        if client and self.security_policy:
            if not command_allowed(
                command_type.value,
                self.security_policy.daemon_command_acl,
                client.client_class,
            ):
                raise PermissionError(
                    f"{client.client_class} is not allowed to call {command_type.value}"
                )

            self._ensure_sensitive_command_allowed(command_type, client)

        if self.verbosity >= 1:
            log.debug(f"Command: {command_type.value} -> {self._log_view(data)}")

        device_result = await daemon_device_commands.handle_device_command(
            self._device_command_daemon(),
            command_type,
            data,
        )
        if device_result is not None:
            return device_result

        if command_type == CommandType.PING:
            return {"pong": True}

        macro_result = await daemon_macro_commands.handle_macro_command(
            self._macro_command_daemon(),
            command_type,
            data,
        )
        if macro_result is not None:
            return macro_result

        capture_result = await daemon_capture_commands.handle_capture_command(
            self._capture_command_daemon(),
            command_type,
            data,
        )
        if capture_result is not None:
            return capture_result

        if command_type == CommandType.REFRESH_RECORDING_UNLOCK:
            if client is None:
                raise PermissionError("recording_refresh_denied: missing client context")
            uid = int(client.uid)
            ttl = int_like(data.get("ttl", 60), 60)
            return self._refresh_runtime_unlock(uid, ttl, client)

        if command_type == CommandType.LOCK_RECORDING_UNLOCK:
            if client is None:
                raise PermissionError("recording_lock_denied: missing client context")
            uid = int(client.uid)
            cleanup = bool(data.get("cleanup", False))
            return self._lock_runtime_unlock(uid, client, cleanup=cleanup)

        raise ValueError(f"Unknown command: {command_type}")

    def _ensure_sensitive_command_allowed(
        self,
        command_type: CommandType,
        client: ClientContext,
    ) -> None:
        policy = self.security_policy
        if policy is None or not policy.recording_unlock_required:
            return

        tier1_commands = {
            CommandType.START_RECORDING,
            CommandType.CAPTURE_BEGIN,
            CommandType.CAPTURE_READ,
            CommandType.CAPTURE_END,
            CommandType.CAPTURE_COMBO,
        }

        tier2_commands = {
            CommandType.MACRO_GET,
            CommandType.MACRO_CREATE,
            CommandType.MACRO_UPDATE,
            CommandType.MACRO_SAVE_RECORDING,
        }

        requires_unlock = command_type in tier1_commands
        if not requires_unlock and policy.macro_edit_requires_unlock:
            requires_unlock = command_type in tier2_commands

        if not requires_unlock:
            return

        self._ensure_sensitive_owner(command_type, client)

        unlocked, expires_at, source = self._recording_unlocked_for_uid(client.uid)
        if unlocked:
            return

        if source == "none":
            raise PermissionError(
                "recording_locked: unlock required for capture/recording features"
            )
        raise PermissionError(f"recording_locked: unlock lease expired at {expires_at}")

    def _recording_unlocked_for_uid(self, uid: int) -> tuple[bool, int, str]:
        now_mono = time.monotonic()
        cached = self._unlock_cache.get(uid)

        if cached is not None:
            checked_mono, unlocked, expires_at, source = cached
            if unlocked and (expires_at == 0 or expires_at >= int(time.time())):
                return unlocked, expires_at, source
            if (now_mono - checked_mono) < self._unlock_cache_interval_s:
                return unlocked, expires_at, source

        status = resolve_unlock_status(uid)
        unlocked = bool(status.get("unlocked", False))
        expires_at = int(status.get("expires_at", 0) or 0)
        source = str(status.get("source", "none") or "none")
        self._unlock_cache[uid] = (now_mono, unlocked, expires_at, source)
        self._log_unlock_state_change(uid, unlocked, source, expires_at, reason="status_probe")
        return unlocked, expires_at, source

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

    def _ensure_sensitive_owner(self, command_type: CommandType, client: ClientContext) -> None:
        sensitive_commands = {
            CommandType.START_RECORDING,
            CommandType.CAPTURE_BEGIN,
            CommandType.CAPTURE_READ,
            CommandType.CAPTURE_END,
            CommandType.CAPTURE_COMBO,
            CommandType.MACRO_GET,
            CommandType.MACRO_CREATE,
            CommandType.MACRO_UPDATE,
        }
        if command_type not in sensitive_commands:
            return

        owner = self._recording_refresh_owners.get(int(client.uid))
        if owner is None:
            self._recording_refresh_owners[int(client.uid)] = (
                int(client.pid),
                int(client.connection_id),
            )
            return

        owner_pid, owner_connection_id = owner
        if owner_pid == int(client.pid) and owner_connection_id == int(client.connection_id):
            return

        log.warning(
            (
                "Sensitive command owner mismatch uid=%s pid=%s connection=%s "
                "owner_pid=%s owner_connection=%s command=%s"
            ),
            client.uid,
            client.pid,
            client.connection_id,
            owner_pid,
            owner_connection_id,
            command_type.value,
        )

        raise PermissionError("sensitive_command_denied: caller is not active session owner")

    def _refresh_runtime_unlock(self, uid: int, ttl: int, client: ClientContext) -> JsonObject:
        owner = self._recording_refresh_owners.get(uid)
        if owner is None:
            self._recording_refresh_owners[uid] = (int(client.pid), int(client.connection_id))
        else:
            owner_pid, owner_connection_id = owner
            if owner_pid != int(client.pid) or owner_connection_id != int(client.connection_id):
                log.warning(
                    (
                        "Recording refresh owner mismatch uid=%s pid=%s connection=%s "
                        "owner_pid=%s owner_connection=%s"
                    ),
                    client.uid,
                    client.pid,
                    client.connection_id,
                    owner_pid,
                    owner_connection_id,
                )
                raise PermissionError(
                    "recording_refresh_denied: caller is not active session owner"
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
            mode=0o644,
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

        self._unlock_cache[uid] = (time.monotonic(), False, 0, "none")
        self._recording_refresh_owners.pop(uid, None)
        self._log_unlock_state_change(uid, False, "none", 0, reason=reason)

    def _clear_all_runtime_unlocks(self, *, reason: str) -> None:
        runtime_uids = {int(uid) for uid in self._recording_refresh_owners}
        try:
            for path in RECORDING_UNLOCK_RUNTIME_DIR.glob("recording-unlock-*"):
                suffix = path.name.removeprefix("recording-unlock-")
                try:
                    runtime_uids.add(int(suffix))
                except ValueError:
                    continue
        except FileNotFoundError:
            pass

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

    def _lock_runtime_unlock(
        self,
        uid: int,
        client: ClientContext,
        *,
        cleanup: bool = False,
    ) -> JsonObject:
        if not cleanup:
            owner = self._recording_refresh_owners.get(uid)
            if owner is None:
                raise PermissionError("recording_lock_denied: no active session owner")

            owner_pid, owner_connection_id = owner
            if owner_pid != int(client.pid) or owner_connection_id != int(client.connection_id):
                log.warning(
                    (
                        "Recording lock owner mismatch uid=%s pid=%s connection=%s "
                        "owner_pid=%s owner_connection=%s"
                    ),
                    client.uid,
                    client.pid,
                    client.connection_id,
                    owner_pid,
                    owner_connection_id,
                )
                raise PermissionError("recording_lock_denied: caller is not active session owner")

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

    async def _on_client_disconnect(self) -> None:
        log.info("Client disconnected, clearing runtime unlocks and releasing all devices")
        await asyncio.to_thread(self._clear_all_runtime_unlocks, reason="session_disconnect")
        await self.recording_manager.discard_all_pending_recordings()
        await self.device_manager.release_all_devices()

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
            raise RuntimeError(
                f"Failed to remove daemon socket path {SOCKET_PATH}: {exc}"
            ) from exc

    def _validate_peer(self, peer: PeerCredentials) -> tuple[bool, str, str]:
        if self.security_policy is None:
            return False, "unknown", "security policy not loaded"

        if not uid_allowed(peer.uid, self.security_policy.daemon_allowed_uids):
            return False, "unknown", f"uid {peer.uid} is not allowed by daemon policy"

        return True, "session", "peer uid allowed"


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
    except Exception as e:
        log.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
