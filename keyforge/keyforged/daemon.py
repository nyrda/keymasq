import argparse
import asyncio
import logging
import os
import signal
import socket
import stat
import sys
import time
import uuid
from collections.abc import Sequence
from typing import Protocol, cast

from keyforge.common.ipc import CommandType
from keyforge.common.paths import (
    RECORDING_UNLOCK_RUNTIME_DIR,
    RUN_DIR,
    SECURITY_POLICY_PATH,
    SOCKET_PATH,
    STATE_DIR,
)
from keyforge.common.recording_guard import (
    resolve_unlock_status,
    runtime_unlock_path,
    write_unlock_expires_at,
)
from keyforge.common.security import (
    PeerCredentials,
    SecurityPolicy,
    command_allowed,
    load_security_policy,
    uid_allowed,
)
from keyforge.keyforged.capture_manager import CaptureManager
from keyforge.keyforged.device_manager import DeviceManager
from keyforge.keyforged.macro_store import MacroStore
from keyforge.keyforged.recording import RecordingManager
from keyforge.keyforged.socket_server import ClientContext, SocketServer

log = logging.getLogger("keyforged")
type JsonObject = dict[str, object]
type JsonObjectList = list[JsonObject]


class _GrabbedDeviceRef(Protocol):
    path: str


class _DaemonDeviceManager(Protocol):
    broadcast_callback: object | None
    recording_manager: object | None
    grabbed_devices: dict[str, list[_GrabbedDeviceRef]]

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
        force_grab_unmapped: bool = False,
    ) -> JsonObject: ...

    async def release_device(
        self, hardware_id: str, immediate: bool = False, grace_s: float | None = None
    ) -> JsonObject: ...

    async def set_mapping(self, hardware_id: str, mapping: JsonObject) -> JsonObject: ...

    async def set_combos(self, combos: Sequence[object]) -> JsonObject: ...

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

    def _authorize_combo_capture(self) -> object: ...

    def register_combo_notifier(
        self, token: str, loop: asyncio.AbstractEventLoop, notify_event: asyncio.Event
    ) -> None: ...

    def read_combo_nowait(self, token: str) -> JsonObject: ...

    def end(self, token: str) -> JsonObject: ...


def _int_like(value: object, default: int) -> int:
    return default if value in {None, ""} else int(cast(int | float | str, value))


def _float_like(value: object, default: float) -> float:
    return default if value in {None, ""} else float(cast(int | float | str, value))


def _str_value(value: object, default: str = "") -> str:
    return str(value or default)


def _json_object(value: object) -> JsonObject:
    return cast(JsonObject, value)


def _json_object_list(value: object) -> JsonObjectList:
    return cast(JsonObjectList, value)


def _str_list(value: object) -> list[str]:
    return cast(list[str], value)


def _str_dict(value: object) -> dict[str, str]:
    return cast(dict[str, str], value)


def _int_dict(value: object) -> dict[str, int]:
    return cast(dict[str, int], value)


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
        await asyncio.to_thread(self._prepare_macro_store)
        log.info(
            "Security policy loaded from %s",
            SECURITY_POLICY_PATH,
        )

        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()

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

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._signal_handler)

        log.info(f"Starting keyforged (socket: {SOCKET_PATH})")

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
        log.info("Stopping keyforged")
        self.running = False

        await self.device_manager.stop_topology_watcher()
        await self.device_manager.cancel_macro_playback()
        await self.device_manager.release_all_devices()

        if self.socket_server:
            await self.socket_server.stop()

        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()

    def _prepare_macro_store(self) -> None:
        self.macro_store.ensure()
        self._register_internal_macros()

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

        if command_type == CommandType.GRAB_DEVICE:
            return await self.device_manager.grab_device(
                hardware_id=_str_value(data["hardware_id"]),
                evdev_paths=_str_list(data["evdev_paths"]),
                button_map=_str_dict(data.get("button_map", {})),
                button_codes=_int_dict(data.get("button_codes", {})),
                force_grab_unmapped=bool(data.get("force_grab_unmapped", False)),
            )

        elif command_type == CommandType.RELEASE_DEVICE:
            grace_s = data.get("grace_s")
            return await self.device_manager.release_device(
                hardware_id=_str_value(data["hardware_id"]),
                immediate=bool(data.get("immediate", False)),
                grace_s=_float_like(grace_s, 0.0) if grace_s is not None else None,
            )

        elif command_type == CommandType.SET_MAPPING:
            mapping = await self._resolve_mapping_macros(_json_object(data["mapping"]))
            return await self.device_manager.set_mapping(
                hardware_id=_str_value(data["hardware_id"]),
                mapping=mapping,
            )

        elif command_type == CommandType.SET_COMBOS:
            combos = await self._resolve_combo_macros(_json_object_list(data.get("combos", [])))
            return await self.device_manager.set_combos(combos)

        elif command_type == CommandType.LIST_DEVICES:
            return await self.device_manager.list_devices()

        elif command_type == CommandType.PING:
            return {"pong": True}

        elif command_type == CommandType.START_RECORDING:
            return await self.recording_manager.start(
                _json_object_list(data.get("devices", [])),
                include_mouse_movement=bool(data.get("include_mouse_movement", False)),
                include_mouse_clicks=bool(data.get("include_mouse_clicks", False)),
            )

        elif command_type == CommandType.STOP_RECORDING:
            return await self.recording_manager.stop()

        elif command_type == CommandType.PLAY_MACRO:
            macro_events = _json_object_list(data.get("macro_events", []))
            macro_name = _str_value(data.get("macro_name", ""))
            loop_mode = _str_value(data.get("loop_mode", "none"), "none") or "none"
            loop_count = _int_like(data.get("loop_count", 1), 1)
            move_to_start = bool(data.get("move_to_start", False))
            start_x = _int_like(data.get("start_x", 0), 0)
            start_y = _int_like(data.get("start_y", 0), 0)
            block_mouse_movement = bool(data.get("block_mouse_movement", False))

            if macro_name and not macro_events:
                macro_data = await asyncio.to_thread(self.macro_store.get, macro_name)
                macro_events = _json_object_list(macro_data.get("events", []))
                loop_mode = (
                    _str_value(macro_data.get("loop_mode", loop_mode), loop_mode) or loop_mode
                )
                loop_count = _int_like(macro_data.get("loop_count", loop_count), loop_count)
                move_to_start = bool(macro_data.get("move_to_start", move_to_start))
                start_x = _int_like(macro_data.get("start_x", start_x), start_x)
                start_y = _int_like(macro_data.get("start_y", start_y), start_y)
                block_mouse_movement = bool(
                    macro_data.get("block_mouse_movement", block_mouse_movement)
                )

            return await self.device_manager.play_macro(
                macro_events=macro_events,
                macro_name=macro_name,
                replay_mouse_movement=bool(data.get("replay_mouse_movement", True)),
                replay_mouse_clicks=bool(data.get("replay_mouse_clicks", True)),
                speed=_float_like(data.get("speed", 1.0), 1.0),
                loop_mode=loop_mode,
                loop_count=loop_count,
                move_to_start=move_to_start,
                start_x=start_x,
                start_y=start_y,
                block_mouse_movement=block_mouse_movement,
            )

        elif command_type == CommandType.MACRO_LIST_META:
            macros = await asyncio.to_thread(self.macro_store.list_meta)
            return {"macros": macros}

        elif command_type == CommandType.MACRO_GET:
            name = _str_value(data.get("name", ""))
            macro = await asyncio.to_thread(self.macro_store.get, name)
            return {"macro": macro}

        elif command_type == CommandType.MACRO_CREATE:
            raw_payload = data.get("macro", {})
            if not isinstance(raw_payload, dict):
                raise ValueError("macro payload must be an object")
            payload = cast(JsonObject, raw_payload)
            macro = await asyncio.to_thread(self.macro_store.create, payload)
            return {"macro": macro}

        elif command_type == CommandType.MACRO_UPDATE:
            name = _str_value(data.get("name", ""))
            raw_payload = data.get("macro", {})
            if not isinstance(raw_payload, dict):
                raise ValueError("macro payload must be an object")
            payload = cast(JsonObject, raw_payload)
            expected_revision = data.get("expected_revision")
            revision = _int_like(expected_revision, 0) if expected_revision is not None else None
            macro = await asyncio.to_thread(
                self.macro_store.update,
                name,
                payload,
                revision,
            )
            return {"macro": macro}

        elif command_type == CommandType.MACRO_RENAME:
            old_name = _str_value(data.get("old_name", ""))
            new_name = _str_value(data.get("new_name", ""))
            expected_revision = data.get("expected_revision")
            revision = _int_like(expected_revision, 0) if expected_revision is not None else None
            macro = await asyncio.to_thread(self.macro_store.rename, old_name, new_name, revision)
            return {"macro": macro}

        elif command_type == CommandType.MACRO_DELETE:
            name = _str_value(data.get("name", ""))
            expected_revision = data.get("expected_revision")
            revision = _int_like(expected_revision, 0) if expected_revision is not None else None
            await asyncio.to_thread(self.macro_store.delete, name, revision)
            return {"status": "ok"}

        elif command_type == CommandType.MACRO_PLAY_BY_NAME:
            name = _str_value(data.get("name", ""))
            macro_data = await asyncio.to_thread(self.macro_store.get, name)
            return await self.device_manager.play_macro(
                macro_events=_json_object_list(macro_data.get("events", [])),
                macro_name=name,
                replay_mouse_movement=bool(data.get("replay_mouse_movement", True)),
                replay_mouse_clicks=bool(data.get("replay_mouse_clicks", True)),
                speed=_float_like(data.get("speed", 1.0), 1.0),
                loop_mode=_str_value(macro_data.get("loop_mode", "none"), "none") or "none",
                loop_count=_int_like(macro_data.get("loop_count", 1), 1),
                move_to_start=bool(macro_data.get("move_to_start", False)),
                start_x=_int_like(macro_data.get("start_x", 0), 0),
                start_y=_int_like(macro_data.get("start_y", 0), 0),
                block_mouse_movement=bool(macro_data.get("block_mouse_movement", False)),
            )

        elif command_type == CommandType.CANCEL_MACRO_PLAYBACK:
            return await self.device_manager.cancel_macro_playback()

        elif command_type == CommandType.MACRO_EXEC_COMPLETE:
            wait_id = _str_value(data.get("wait_id", ""))
            returncode = _int_like(data.get("returncode", 0), 0)
            return self.device_manager.complete_macro_exec_wait(wait_id, returncode)

        elif command_type == CommandType.CAPTURE_BEGIN:
            hardware_id = str(data.get("hardware_id", ""))
            return await asyncio.to_thread(self.capture_manager.begin, hardware_id)

        elif command_type == CommandType.CAPTURE_READ:
            token = str(data.get("token", ""))
            return await asyncio.to_thread(self.capture_manager.read, token)

        elif command_type == CommandType.CAPTURE_END:
            token = str(data.get("token", ""))
            return await asyncio.to_thread(self.capture_manager.end, token)

        elif command_type == CommandType.CAPTURE_COMBO:
            hardware_ids = {
                str(hardware_id).lower()
                for hardware_id in _str_list(data.get("hardware_ids", []))
                if str(hardware_id).strip()
            }
            timeout_s = _float_like(data.get("timeout_s", 15.0), 15.0)
            return await self._capture_combo(hardware_ids, timeout_s)

        elif command_type == CommandType.SET_DIAGNOSTICS:
            enabled = bool(data.get("enabled", False))
            interval = _float_like(data.get("interval", 5.0), 5.0)
            return await self.device_manager.set_diagnostics(enabled, interval)

        elif command_type == CommandType.REFRESH_RECORDING_UNLOCK:
            if client is None:
                raise PermissionError("recording_refresh_denied: missing client context")
            uid = int(client.uid)
            ttl = _int_like(data.get("ttl", 60), 60)
            return self._refresh_runtime_unlock(uid, ttl, client)

        elif command_type == CommandType.LOCK_RECORDING_UNLOCK:
            if client is None:
                raise PermissionError("recording_lock_denied: missing client context")
            uid = int(client.uid)
            cleanup = bool(data.get("cleanup", False))
            return self._lock_runtime_unlock(uid, client, cleanup=cleanup)

        else:
            raise ValueError(f"Unknown command: {command_type}")

    async def _capture_combo(self, hardware_ids: set[str], timeout_s: float) -> JsonObject:
        if not hardware_ids:
            raise ValueError("capture_combo requires at least one hardware_id")

        token = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        notify_event = asyncio.Event()
        grabbed_paths = {
            device.path
            for hardware_id, devices in self.device_manager.grabbed_devices.items()
            if hardware_id.lower() in hardware_ids
            for device in devices
        }
        self.device_manager.begin_combo_capture(token, hardware_ids, notify_event)
        try:
            authorization = self.capture_manager._authorize_combo_capture()
            capture_result = await asyncio.to_thread(
                self.capture_manager.begin_combo,
                token,
                grabbed_paths,
                True,
                hardware_ids,
                authorization,
            )
            self.capture_manager.register_combo_notifier(token, loop, notify_event)
            warnings = _str_list(capture_result.get("warnings", []))
            deadline = loop.time() + max(1.0, float(timeout_s))
            pressed: set[str] = set()
            events: list[dict[str, str]] = []

            while loop.time() < deadline:
                event = await self._read_capture_combo_event(token, notify_event, deadline)
                if not isinstance(event, dict):
                    continue

                evdev_name = str(event.get("evdev", "") or "")
                raw_value = event.get("value")
                value = _int_like(raw_value, -1) if raw_value is not None else -1
                if not evdev_name.startswith(("key_", "btn_")) or value not in {0, 1}:
                    continue

                if value == 1:
                    event_key = "|".join(
                        [
                            str(event.get("hardware_id", "") or ""),
                            str(event.get("source", "") or ""),
                            evdev_name,
                        ]
                    )
                    pressed.add(event_key)
                    if not any(
                        existing.get("evdev") == evdev_name
                        and existing.get("hardware_id") == str(event.get("hardware_id", "") or "")
                        and existing.get("source") == str(event.get("source", "") or "")
                        for existing in events
                    ):
                        events.append(
                            {
                                "evdev": evdev_name,
                                "hardware_id": str(event.get("hardware_id", "") or ""),
                                "source": str(event.get("source", "") or ""),
                            }
                        )
                    continue

                if not events:
                    continue
                event_key = "|".join(
                    [
                        str(event.get("hardware_id", "") or ""),
                        str(event.get("source", "") or ""),
                        evdev_name,
                    ]
                )
                pressed.discard(event_key)
                if not pressed:
                    return {
                        "events": events,
                        "warnings": warnings,
                    }

            raise TimeoutError("Combo capture timed out")
        finally:
            self.device_manager.end_combo_capture(token)
            await asyncio.to_thread(self.capture_manager.end, token)

    async def _read_capture_combo_event(
        self,
        token: str,
        notify_event: asyncio.Event,
        deadline: float,
    ) -> JsonObject | None:
        loop = asyncio.get_running_loop()

        while loop.time() < deadline:
            event = self._drain_capture_combo_event_sources(token)
            if event is not None:
                return event

            remaining = deadline - loop.time()
            if remaining <= 0:
                return None

            if notify_event.is_set():
                notify_event.clear()
                continue

            try:
                await asyncio.wait_for(notify_event.wait(), timeout=remaining)
            except TimeoutError:
                return None
            notify_event.clear()

        return None

    def _drain_capture_combo_event_sources(self, token: str) -> JsonObject | None:
        event = self.device_manager.read_combo_capture(token).get("event")
        if isinstance(event, dict):
            return cast(JsonObject, event)

        passive_event = self.capture_manager.read_combo_nowait(token).get("event")
        if isinstance(passive_event, dict):
            return cast(JsonObject, passive_event)

        return None

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

    def _validate_peer(self, peer: PeerCredentials) -> tuple[bool, str, str]:
        if self.security_policy is None:
            return False, "unknown", "security policy not loaded"

        if not uid_allowed(peer.uid, self.security_policy.daemon_allowed_uids):
            return False, "unknown", f"uid {peer.uid} is not allowed by daemon policy"

        return True, "session", "peer uid allowed"

    async def _load_macro_definitions(self, macro_names: set[str]) -> dict[str, JsonObject]:
        if not macro_names:
            return {}

        async def load_macro(name: str) -> tuple[str, JsonObject | None]:
            try:
                macro = await asyncio.to_thread(self.macro_store.get, name)
            except Exception:
                return name, None
            return name, macro

        loaded = await asyncio.gather(*(load_macro(name) for name in sorted(macro_names)))
        return {name: macro for name, macro in loaded if isinstance(macro, dict)}

    def _apply_macro_definition(self, action_data: JsonObject, macro: JsonObject) -> JsonObject:
        updated: JsonObject = dict(action_data)
        updated["macro_events"] = _json_object_list(macro.get("events", []))
        updated["macro_loop_mode"] = _str_value(macro.get("loop_mode", "none"), "none") or "none"
        updated["macro_loop_count"] = _int_like(macro.get("loop_count", 1), 1)
        updated["macro_move_to_start"] = bool(macro.get("move_to_start", False))
        updated["macro_start_x"] = _int_like(macro.get("start_x", 0), 0)
        updated["macro_start_y"] = _int_like(macro.get("start_y", 0), 0)
        updated["macro_block_mouse_movement"] = bool(macro.get("block_mouse_movement", False))
        return updated

    async def _resolve_mapping_macros(self, mapping: JsonObject) -> JsonObject:
        macro_names: set[str] = set()
        for action_raw in mapping.values():
            if not isinstance(action_raw, dict):
                continue
            action_data = cast(JsonObject, action_raw)
            if (
                action_data.get("action") == "macro"
                and action_data.get("macro_name")
                and not action_data.get("macro_events")
            ):
                macro_names.add(str(action_data["macro_name"]))
        macros = await self._load_macro_definitions(macro_names)

        resolved: JsonObject = {}
        for button_id, action_data in mapping.items():
            if not isinstance(action_data, dict):
                resolved[button_id] = action_data
                continue

            updated: JsonObject = dict(cast(JsonObject, action_data))
            macro_name = str(updated.get("macro_name", "") or "")
            if (
                updated.get("action") == "macro"
                and macro_name
                and not updated.get("macro_events")
                and macro_name in macros
            ):
                try:
                    updated = self._apply_macro_definition(updated, macros[macro_name])
                except (TypeError, ValueError):
                    pass

            resolved[button_id] = updated

        return resolved

    async def _resolve_combo_macros(self, combos: JsonObjectList) -> JsonObjectList:
        macro_names: set[str] = set()
        for combo in combos:
            action_raw = combo.get("action")
            if not isinstance(action_raw, dict):
                continue
            action_data = cast(JsonObject, action_raw)
            if (
                action_data.get("action") == "macro"
                and action_data.get("macro_name")
                and not action_data.get("macro_events")
            ):
                macro_names.add(str(action_data["macro_name"]))
        macros = await self._load_macro_definitions(macro_names)

        resolved: JsonObjectList = []
        for combo in combos:
            updated: JsonObject = dict(combo)
            action_data = updated.get("action")
            if not isinstance(action_data, dict):
                resolved.append(updated)
                continue

            action: JsonObject = dict(cast(JsonObject, action_data))
            macro_name = str(action.get("macro_name", "") or "")
            if (
                action.get("action") == "macro"
                and macro_name
                and not action.get("macro_events")
                and macro_name in macros
            ):
                try:
                    action = self._apply_macro_definition(action, macros[macro_name])
                except (TypeError, ValueError):
                    pass

            updated["action"] = action
            resolved.append(updated)

        return resolved


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="keyforged",
        description="Keyforge Input Remapping Daemon",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Enable debug logging (-v) or trace logging (-vv)",
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

    if args.verbose >= 2:
        log.info("Trace logging enabled (-vv)")

    if os.geteuid() == 0 and not args.allow_root:
        log.error("keyforged should not run as root. Use --allow-root to override.")
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
