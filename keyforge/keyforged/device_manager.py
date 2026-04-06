import asyncio
import contextlib
import errno
import logging
import os
import queue
import random
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import evdev

from keyforge.common.devices import (
    clear_device_path_cache,
    detect_input_classes,
    get_interface_id,
    primary_input_class,
    resolve_stable_path,
)
from keyforge.common.ipc import CommandType
from keyforge.common.models import (
    ActionType,
    DeviceType,
    MappingAction,
)
from keyforge.keyforged.combo_engine import (
    ComboDecision,
    RuntimeCombo,
    RuntimeComboBinding,
    RuntimeComboStep,
)
from keyforge.keyforged.output_helpers import emit_mouse_move, get_trigger_axis, resolve_output_code
from keyforge.keyforged.recording import RecordingManager
from keyforge.keyforged.runtime import actions as runtime_actions
from keyforge.keyforged.runtime import combos as runtime_combos
from keyforge.keyforged.runtime import grab_lifecycle as runtime_grab_lifecycle
from keyforge.keyforged.runtime import grabbed_device as runtime_grabbed_device
from keyforge.keyforged.runtime import macros as runtime_macros
from keyforge.keyforged.runtime import topology as runtime_topology

log = logging.getLogger("keyforged.devices")
ACTIVE_KEY_IDLE_LOG_INTERVAL_S = 1.0
ACTIVE_KEY_IDLE_MAX_WAIT_S = 300.0
COMBO_HELD_REARM_MODIFIERS = frozenset({"shift", "ctrl", "alt", "meta"})
TOPOLOGY_POLL_INTERVAL_S = 0.5
TOPOLOGY_DEBOUNCE_S = 0.5
TEST_UINPUT_ENV = "KEYFORGE_TEST_UINPUT"
TEST_UINPUT_PREFIX = "keyforge-test"
TEST_UINPUT_VENDOR = 0x4B46
TEST_UINPUT_PRODUCTS = {
    "keyboard": 0x1001,
    "mouse": 0x1002,
    "gamepad": 0x1003,
    "passthrough": 0x1004,
}
type JsonObject = dict[str, object]
type BroadcastCallback = Callable[[CommandType, JsonObject], Awaitable[None]]
type MappingGetter = Callable[[], dict[str, MappingAction]]
type DeviceEventCallback = Callable[..., Awaitable[ComboDecision | bool | None]]
type MacroPlayer = Callable[..., Awaitable[JsonObject]]
type RapidfireTaskFactory = Callable[[], asyncio.Task[None]]


class _DeviceInfo(Protocol):
    vendor: int
    product: int


class _ManagedInputDevice(Protocol):
    path: str
    name: str | None
    info: _DeviceInfo

    def grab(self) -> None: ...

    def ungrab(self) -> None: ...

    def capabilities(self) -> dict[int, Sequence[object]]: ...

    def async_read_loop(self) -> AsyncIterator[evdev.InputEvent]: ...

    def fileno(self) -> int: ...

    def read_one(self) -> evdev.InputEvent | None: ...

    def active_keys(self) -> Sequence[int]: ...

    def close(self) -> None: ...


class _WritableUInput(Protocol):
    def write(self, event_type: int, code: int, value: int) -> None: ...

    def syn(self) -> None: ...

    def close(self) -> None: ...


def _json_object(value: object) -> JsonObject | None:
    return cast(JsonObject, value) if isinstance(value, dict) else None


def _json_list(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def _str_value(value: object, default: str = "") -> str:
    return default if value is None else str(value)


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _int_value(value: object, default: int = 0) -> int:
    return default if value is None else int(cast(int | float | str, value))


def _int_or_none(value: object) -> int | None:
    return None if value is None else _int_value(value)


def _float_value(value: object, default: float = 0.0) -> float:
    return default if value is None else float(cast(int | float | str, value))


def _device_input(path: str) -> _ManagedInputDevice:
    return cast(_ManagedInputDevice, evdev.InputDevice(path))


def _device_paths() -> list[str]:
    return cast(Callable[[], list[str]], evdev.list_devices)()


def _uinput_writer(device: evdev.UInput | None) -> _WritableUInput | None:
    return cast(_WritableUInput | None, device)


def _test_uinput_enabled() -> bool:
    value = str(os.environ.get(TEST_UINPUT_ENV, "")).strip().lower()
    return value not in {"", "0", "false", "no"}


def _uinput_identity(
    normal_name: str,
    kind: str,
    *,
    test_name: str | None = None,
) -> tuple[str, int | None, int | None]:
    if not _test_uinput_enabled():
        return normal_name, None, None
    return (
        f"{TEST_UINPUT_PREFIX}-{test_name or kind}",
        TEST_UINPUT_VENDOR,
        TEST_UINPUT_PRODUCTS[kind],
    )


def _fire_and_observe(coro: Awaitable[object], label: str) -> asyncio.Task[object]:
    task = asyncio.ensure_future(coro)

    def _log_task_result(done: asyncio.Task[object]) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            exc = done.exception()
            if exc is not None:
                log.warning("%s failed: %s", label, exc)

    task.add_done_callback(_log_task_result)
    return task


@dataclass(frozen=True)
class LiveInterfaceInfo:
    hardware_id: str
    vendor_id: str
    product_id: str
    stable_path: str
    path: str
    interface_id: str


@dataclass
class DesiredGrabConfig:
    paths: set[str]
    button_map: dict[str, str]
    button_codes: dict[str, int] = field(default_factory=dict)
    force_grab_unmapped: bool = False


@dataclass
class OutputRuntimeState:
    device_count: int = 0
    keyboard_uinput: evdev.UInput | None = None
    mouse_uinput: evdev.UInput | None = None
    gamepad_uinput: evdev.UInput | None = None


@dataclass
class MacroRuntimeState:
    tasks: dict[int, asyncio.Task[None]] = field(default_factory=dict)
    instance_meta: dict[int, dict[str, str]] = field(default_factory=dict)
    instance_seq: int = 0
    instance_held: dict[int, set[tuple[str, int]]] = field(default_factory=dict)
    held_refcount: dict[tuple[str, int], int] = field(default_factory=dict)
    cancel_instance_ids: set[int] = field(default_factory=set)
    mouse_inhibit_count: int = 0
    exec_waiters: dict[str, asyncio.Future[int]] = field(default_factory=dict)
    mouse_rel_suppressed: bool = False
    mouse_rel_suppression_watchdog_task: asyncio.Task[None] | None = None


@dataclass
class DiagnosticsState:
    enabled: bool = False
    interval: float = 5.0
    task: asyncio.Task[None] | None = None
    samples: dict[str, deque[float]] = field(default_factory=dict)


@dataclass
class GrabRuntimeState:
    release_grace_s: float
    held_release_retry_s: float
    desired_paths: dict[str, set[str]] = field(default_factory=dict)
    desired_grabs: dict[str, DesiredGrabConfig] = field(default_factory=dict)
    pending_interface_release: dict[tuple[str, str], asyncio.Task[None]] = field(
        default_factory=dict
    )
    pending_hardware_release: dict[str, asyncio.Task[None]] = field(default_factory=dict)


@dataclass
class TopologyRuntimeState:
    poll_s: float
    debounce_s: float
    watcher_task: asyncio.Task[None] | None = None
    reconcile_task: asyncio.Task[None] | None = None
    live_snapshot: dict[str, LiveInterfaceInfo] = field(default_factory=dict)
    reconciled_snapshot: dict[str, LiveInterfaceInfo] = field(default_factory=dict)


class DeviceManager:
    def __init__(
        self,
        verbosity: int = 0,
        broadcast_callback: BroadcastCallback | None = None,
        release_grace_s: float = 60.0,
        held_release_retry_s: float = 10.0,
        topology_poll_s: float = TOPOLOGY_POLL_INTERVAL_S,
        topology_debounce_s: float = TOPOLOGY_DEBOUNCE_S,
    ) -> None:
        self.grabbed_devices: dict[str, list[GrabbedDevice]] = {}
        self.active_mappings: dict[str, dict[str, MappingAction]] = {}
        self.verbosity = verbosity
        self.broadcast_callback = broadcast_callback

        self.output_state = OutputRuntimeState()
        self.recording_manager: RecordingManager | None = None
        self.macro_state = MacroRuntimeState()
        self._op_lock = asyncio.Lock()
        self.diagnostics_state = DiagnosticsState()
        self.grab_state = GrabRuntimeState(
            release_grace_s=max(0.01, float(release_grace_s)),
            held_release_retry_s=max(0.01, float(held_release_retry_s)),
        )
        self.combo_state = runtime_combos.ComboRuntimeState()
        self.topology_state = TopologyRuntimeState(
            poll_s=max(0.05, float(topology_poll_s)),
            debounce_s=max(0.05, float(topology_debounce_s)),
        )
        self._command_type = CommandType
        self._desired_grab_config_cls = DesiredGrabConfig
        self._device_input = _device_input

    @property
    def active_combos(self) -> list[RuntimeCombo]:
        return self.combo_state.active_combos

    @active_combos.setter
    def active_combos(self, value: list[RuntimeCombo]) -> None:
        self.combo_state.active_combos = value

    async def start_topology_watcher(self) -> None:
        await runtime_topology.start_topology_watcher(
            cast(Any, self),
            asyncio_mod=cast(Any, asyncio),
            cancelled_error=asyncio.CancelledError,
            log=log,
            live_interface_info_cls=cast(Any, LiveInterfaceInfo),
            clear_device_path_cache_fn=clear_device_path_cache,
            device_paths_fn=_device_paths,
            device_input_fn=cast(Any, _device_input),
            resolve_stable_path_fn=resolve_stable_path,
            get_interface_id_fn=get_interface_id,
        )

    async def stop_topology_watcher(self) -> None:
        await runtime_topology.stop_topology_watcher(
            cast(Any, self),
            asyncio_mod=cast(Any, asyncio),
            cancelled_error=asyncio.CancelledError,
            contextlib_mod=contextlib,
        )

    async def grab_device(
        self,
        hardware_id: str,
        evdev_paths: list[str],
        button_map: dict[str, str],
        button_codes: dict[str, int] | None = None,
        force_grab_unmapped: bool = False,
    ) -> JsonObject:
        async with self._op_lock:
            return await runtime_grab_lifecycle.grab_device_unlocked(
                cast(Any, self),
                hardware_id,
                evdev_paths,
                button_map,
                button_codes,
                force_grab_unmapped,
                update_desired=True,
                desired_grab_config_cls=DesiredGrabConfig,
                clear_device_path_cache_fn=clear_device_path_cache,
                resolve_stable_path_fn=resolve_stable_path,
                primary_input_class_fn=primary_input_class,
                grabbed_device_cls=GrabbedDevice,
                get_interface_id_fn=get_interface_id,
                str_value_fn=_str_value,
                optional_str_fn=_optional_str,
                int_value_fn=_int_value,
                int_or_none_fn=_int_or_none,
                float_value_fn=_float_value,
                fire_and_observe_fn=_fire_and_observe,
                errno_mod=errno,
            )

    async def release_device(
        self,
        hardware_id: str,
        immediate: bool = False,
        grace_s: float | None = None,
    ) -> JsonObject:
        async with self._op_lock:
            if immediate:
                return await runtime_grab_lifecycle.release_device_unlocked(
                    cast(Any, self),
                    hardware_id,
                    log=log,
                )
            return runtime_grab_lifecycle.schedule_hardware_release_unlocked(
                cast(Any, self),
                hardware_id,
                grace_s,
                asyncio_mod=asyncio,
                log=log,
            )

    async def release_all_devices(self) -> None:
        await runtime_grab_lifecycle.release_all_devices(
            cast(Any, self),
            fire_and_observe_fn=_fire_and_observe,
        )

    async def set_mapping(
        self,
        hardware_id: str,
        mapping: JsonObject,
    ) -> JsonObject:
        return await runtime_grab_lifecycle.set_mapping(
            cast(Any, self),
            hardware_id,
            mapping,
            json_object_fn=_json_object,
            str_value_fn=_str_value,
            optional_str_fn=_optional_str,
            int_value_fn=_int_value,
            int_or_none_fn=_int_or_none,
            float_value_fn=_float_value,
            log=log,
        )

    async def set_combos(self, combos: Sequence[object]) -> JsonObject:
        async with self._op_lock:
            parsed: list[RuntimeCombo] = []
            for combo_data in combos:
                combo_dict = _json_object(combo_data)
                if combo_dict is None:
                    continue
                action_data = combo_dict.get("action")
                action_dict = _json_object(action_data)
                if isinstance(action_data, str):
                    parsed_action_data: JsonObject | str = action_data
                elif action_dict is not None:
                    parsed_action_data = action_dict
                else:
                    continue

                steps_data = _json_list(combo_dict.get("steps"))
                if not steps_data:
                    continue

                steps: list[RuntimeComboStep] = []
                for step_data in steps_data:
                    step_dict = _json_object(step_data)
                    if step_dict is None:
                        continue
                    events_data = _json_list(step_dict.get("events"))
                    if not events_data:
                        continue
                    bindings: list[RuntimeComboBinding] = []
                    for event_data in events_data:
                        event_dict = _json_object(event_data)
                        if event_dict is None:
                            continue
                        hardware_id = _str_value(event_dict.get("hardware_id"), "").lower()
                        evdev_name = _str_value(event_dict.get("evdev"), "").lower()
                        source = _str_value(event_dict.get("source"), "").lower()
                        if not hardware_id or not evdev_name:
                            continue
                        bindings.append(
                            RuntimeComboBinding(
                                hardware_id=hardware_id,
                                evdev=evdev_name,
                                source=source,
                            )
                        )
                    if bindings:
                        timeout_raw = step_dict.get("timeout_ms")
                        timeout_ms = _int_value(timeout_raw) if timeout_raw is not None else None
                        steps.append(
                            RuntimeComboStep(
                                bindings=tuple(bindings),
                                timeout_ms=timeout_ms,
                            )
                        )

                if not steps:
                    continue

                parsed.append(
                    RuntimeCombo(
                        id=_str_value(combo_dict.get("id"), ""),
                        name=_str_value(combo_dict.get("name"), ""),
                        steps=steps,
                        action=runtime_actions.parse_action(
                            self,
                            parsed_action_data,
                            str_value=_str_value,
                            optional_str=_optional_str,
                            int_value=_int_value,
                            int_or_none=_int_or_none,
                            float_value=_float_value,
                        ),
                        profile_name=_str_value(combo_dict.get("profile_name"), ""),
                    )
                )

            self.active_combos = parsed
            await runtime_combos.clear_combo_runtime(
                cast(Any, self),
                asyncio_mod=cast(Any, asyncio),
                contextlib_mod=contextlib,
                mapping_action_cls=MappingAction,
                evdev_mod=cast(Any, evdev),
                uinput_writer=cast(Any, _uinput_writer),
                emit_mouse_move_fn=cast(Any, emit_mouse_move),
                get_trigger_axis_fn=get_trigger_axis,
                resolve_code_fn=resolve_output_code,
                fire_and_observe_fn=_fire_and_observe,
                command_type=CommandType,
                action_type_enum=ActionType,
                time_mod=time,
            )
            self.combo_state.engine.set_combos(parsed)
            runtime_combos.refresh_combo_timeout_watchdog(
                cast(Any, self),
                asyncio_mod=cast(Any, asyncio),
                time_mod=time,
                action_type_enum=ActionType,
                mapping_action_cls=MappingAction,
                emit_mouse_move_fn=cast(Any, emit_mouse_move),
                get_trigger_axis_fn=get_trigger_axis,
                resolve_code_fn=resolve_output_code,
                fire_and_observe_fn=_fire_and_observe,
                command_type=CommandType,
                contextlib_mod=contextlib,
                evdev_mod=cast(Any, evdev),
                uinput_writer=cast(Any, _uinput_writer),
            )
            log.info("Updated combos (%d active)", len(parsed))
            return {"updated": True, "combo_count": len(parsed)}

    async def set_diagnostics(self, enabled: bool, interval: float = 5.0) -> JsonObject:
        self.diagnostics_state.enabled = bool(enabled)
        self.diagnostics_state.interval = max(0.5, float(interval or 5.0))

        if not self.diagnostics_state.enabled:
            if self.diagnostics_state.task:
                self.diagnostics_state.task.cancel()
                try:
                    await self.diagnostics_state.task
                except asyncio.CancelledError:
                    pass
                self.diagnostics_state.task = None
            self.diagnostics_state.samples.clear()
            log.info("Diagnostics disabled")
            return {"enabled": False, "interval": self.diagnostics_state.interval}

        if self.diagnostics_state.task is None or self.diagnostics_state.task.done():
            self.diagnostics_state.task = asyncio.create_task(self._diagnostics_loop())
        log.info("Diagnostics enabled (interval %.2fs)", self.diagnostics_state.interval)
        return {"enabled": True, "interval": self.diagnostics_state.interval}

    def _record_diagnostic(self, label: str, duration_us: float) -> None:
        if not self.diagnostics_state.enabled:
            return
        bucket = self.diagnostics_state.samples.setdefault(label, deque(maxlen=20000))
        bucket.append(float(duration_us))

    async def _diagnostics_loop(self) -> None:
        try:
            while self.diagnostics_state.enabled:
                await asyncio.sleep(self.diagnostics_state.interval)
                snapshot = {
                    label: list(samples)
                    for label, samples in self.diagnostics_state.samples.items()
                    if samples
                }
                if snapshot:
                    await asyncio.to_thread(self._log_diagnostics_snapshot, snapshot)
        except asyncio.CancelledError:
            raise

    def _log_diagnostics_snapshot(self, snapshot: dict[str, list[float]]) -> None:
        if not snapshot:
            return

        def pct(values: list[float], p: float) -> float:
            if not values:
                return 0.0
            idx = int((len(values) - 1) * p)
            return values[max(0, min(idx, len(values) - 1))]

        for label, samples in snapshot.items():
            if not samples:
                continue
            values = sorted(samples)
            p50 = pct(values, 0.50)
            p95 = pct(values, 0.95)
            p99 = pct(values, 0.99)
            max_v = values[-1]
            log.info(
                "diagnostics[%s]: n=%d p50=%.2fus p95=%.2fus p99=%.2fus max=%.2fus",
                label,
                len(values),
                p50,
                p95,
                p99,
                max_v,
            )

    async def list_devices(self) -> JsonObject:
        return await asyncio.to_thread(self._list_devices_sync)

    def _list_devices_sync(self) -> JsonObject:
        clear_device_path_cache()
        devices: list[JsonObject] = []

        for path in _device_paths():
            try:
                device = _device_input(path)
                info = device.info

                capabilities: list[str] = []
                for ev_type, codes in device.capabilities().items():
                    for code in codes:
                        if isinstance(code, tuple):
                            capabilities.append(f"{evdev.ecodes.EV[ev_type]}_{code[0]}")
                        else:
                            capabilities.append(f"{evdev.ecodes.EV[ev_type]}_{code}")

                device_types = self._detect_device_types(device)
                device_type = primary_input_class(device_types)

                devices.append(
                    {
                        "path": path,
                        "name": device.name,
                        "vendor_id": f"{info.vendor:04x}",
                        "product_id": f"{info.product:04x}",
                        "capabilities": capabilities,
                        "device_types": device_types,
                        "device_type": device_type.value,
                    }
                )
            except Exception as e:
                log.debug(f"Could not read device {path}: {e}")

        return {"devices": devices}

    def _detect_device_types(self, device: _ManagedInputDevice) -> list[str]:
        return detect_input_classes(cast(Any, device))

    def _detect_device_type(self, device: _ManagedInputDevice) -> DeviceType:
        return primary_input_class(self._detect_device_types(device))

    async def play_macro(
        self,
        macro_events: list[JsonObject],
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
    ) -> JsonObject:
        return await runtime_macros.play_macro(
            self,
            macro_events,
            macro_name,
            replay_mouse_movement,
            replay_mouse_clicks,
            speed,
            loop_mode,
            loop_count,
            move_to_start,
            start_x,
            start_y,
            block_mouse_movement,
            source_device,
            source_button,
            trigger_value,
            asyncio_mod=cast(Any, asyncio),
            contextlib_mod=contextlib,
            evdev_mod=evdev,
            log=log,
            int_value_fn=_int_value,
            str_value_fn=_str_value,
            uinput_writer=cast(Any, _uinput_writer),
            random_mod=random,
            uuid_mod=uuid,
            command_type=cast(Any, CommandType),
        )

    async def cancel_macro_playback(self) -> JsonObject:
        return await runtime_macros.cancel_macro_playback(
            self,
            asyncio_mod=cast(Any, asyncio),
            evdev_mod=evdev,
            contextlib_mod=contextlib,
            uinput_writer=cast(Any, _uinput_writer),
        )

    def complete_macro_exec_wait(self, wait_id: str, returncode: int) -> JsonObject:
        return runtime_macros.complete_macro_exec_wait(self, wait_id, returncode)

    def begin_combo_capture(
        self,
        token: str,
        hardware_ids: set[str],
        notify_event: asyncio.Event | None = None,
    ) -> JsonObject:
        return runtime_combos.begin_combo_capture(
            cast(Any, self),
            token,
            hardware_ids,
            notify_event,
            queue_mod=cast(Any, queue),
        )

    def read_combo_capture(self, token: str) -> JsonObject:
        return runtime_combos.read_combo_capture(cast(Any, self), token, queue_mod=cast(Any, queue))

    def end_combo_capture(self, token: str) -> JsonObject:
        return runtime_combos.end_combo_capture(cast(Any, self), token)


GrabbedDevice = runtime_grabbed_device.GrabbedDevice
