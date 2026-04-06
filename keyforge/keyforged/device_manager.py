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
    ComboActionTransition,
    ComboDecision,
    ComboEngine,
    ComboInputEvent,
    ComboSyntheticEvent,
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
from keyforge.keyforged.runtime import outputs as runtime_outputs
from keyforge.keyforged.runtime import topology as runtime_topology
from keyforge.keyforged.superkey_state import (
    SuperkeyActionData,
    SuperkeyConfig,
)

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
type ComboCaptureQueue = tuple[queue.SimpleQueue[JsonObject], set[str], asyncio.Event | None]
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

        self._device_count = 0
        self._keyboard_uinput: evdev.UInput | None = None
        self._mouse_uinput: evdev.UInput | None = None
        self._gamepad_uinput: evdev.UInput | None = None
        self.recording_manager: RecordingManager | None = None
        self._macro_tasks: dict[int, asyncio.Task[None]] = {}
        self._macro_instance_meta: dict[int, dict[str, str]] = {}
        self._macro_instance_seq = 0
        self._macro_instance_held: dict[int, set[tuple[str, int]]] = {}
        self._macro_held_refcount: dict[tuple[str, int], int] = {}
        self._macro_cancel_instance_ids: set[int] = set()
        self._macro_mouse_inhibit_count = 0
        self._macro_exec_waiters: dict[str, asyncio.Future[int]] = {}
        self._mouse_rel_suppressed = False
        self._mouse_rel_suppression_watchdog_task: asyncio.Task[None] | None = None
        self._op_lock = asyncio.Lock()
        self._diagnostics_enabled = False
        self._diagnostics_interval = 5.0
        self._diagnostics_task: asyncio.Task[None] | None = None
        self._diag_samples: dict[str, deque[float]] = {}
        self._release_grace_s = max(0.01, float(release_grace_s))
        self._held_release_retry_s = max(0.01, float(held_release_retry_s))
        self._topology_poll_s = max(0.05, float(topology_poll_s))
        self._topology_debounce_s = max(0.05, float(topology_debounce_s))
        self._desired_paths: dict[str, set[str]] = {}
        self._desired_grabs: dict[str, DesiredGrabConfig] = {}
        self._pending_interface_release: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._pending_hardware_release: dict[str, asyncio.Task[None]] = {}
        self._combo_capture_queues: dict[str, ComboCaptureQueue] = {}
        self.active_combos: list[RuntimeCombo] = []
        self._combo_engine = ComboEngine()
        self._combo_timeout_task: asyncio.Task[None] | None = None
        self._active_combo_actions: dict[str, dict[str, object]] = {}
        self._topology_task: asyncio.Task[None] | None = None
        self._topology_reconcile_task: asyncio.Task[None] | None = None
        self._live_topology_snapshot: dict[str, LiveInterfaceInfo] = {}
        self._reconciled_topology_snapshot: dict[str, LiveInterfaceInfo] = {}
        self._command_type = CommandType
        self._desired_grab_config_cls = DesiredGrabConfig
        self._device_input = _device_input

    def _create_global_uinputs(self) -> None:
        runtime_outputs.create_global_uinputs(
            self,
            evdev_mod=evdev,
            log=log,
            uinput_writer=_uinput_writer,
        )

    def _destroy_global_uinputs(self) -> None:
        runtime_outputs.destroy_global_uinputs(self, log=log)

    async def start_topology_watcher(self) -> None:
        await runtime_topology.start_topology_watcher(self, asyncio_mod=asyncio)

    async def stop_topology_watcher(self) -> None:
        await runtime_topology.stop_topology_watcher(
            self,
            asyncio_mod=asyncio,
            contextlib_mod=contextlib,
        )

    async def _topology_watch_loop(self) -> None:
        await runtime_topology.topology_watch_loop(self, asyncio_mod=asyncio, log=log)

    def _schedule_topology_reconcile(self, snapshot: dict[str, LiveInterfaceInfo]) -> None:
        runtime_topology.schedule_topology_reconcile(
            self,
            snapshot,
            asyncio_mod=asyncio,
            log=log,
        )

    async def _reconcile_topology(self, snapshot: dict[str, LiveInterfaceInfo]) -> None:
        await runtime_topology.reconcile_topology(self, snapshot, log=log)

    async def _reconcile_topology_unlocked(
        self,
        snapshot: dict[str, LiveInterfaceInfo],
    ) -> None:
        await runtime_topology.reconcile_topology_unlocked(self, snapshot)

    def _build_topology_events(
        self,
        previous: dict[str, LiveInterfaceInfo],
        current: dict[str, LiveInterfaceInfo],
        desired_hardware_ids: set[str],
    ) -> list[tuple[CommandType, JsonObject]]:
        return cast(
            list[tuple[CommandType, JsonObject]],
            runtime_topology.build_topology_events(
                self,
                previous,
                current,
                desired_hardware_ids,
            ),
        )

    def _live_interface_payload(self, info: LiveInterfaceInfo) -> JsonObject:
        return runtime_topology.live_interface_payload(info)

    def _scan_live_interfaces_sync(self) -> dict[str, LiveInterfaceInfo]:
        return cast(
            dict[str, LiveInterfaceInfo],
            runtime_topology.scan_live_interfaces_sync(
                live_interface_info_cls=LiveInterfaceInfo,
                clear_device_path_cache_fn=clear_device_path_cache,
                device_paths_fn=_device_paths,
                device_input_fn=_device_input,
                resolve_stable_path_fn=resolve_stable_path,
                get_interface_id_fn=get_interface_id,
                log=log,
            ),
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
            return await self._grab_device_unlocked(
                hardware_id,
                evdev_paths,
                button_map,
                button_codes=button_codes,
                force_grab_unmapped=force_grab_unmapped,
            )

    async def _grab_device_unlocked(
        self,
        hardware_id: str,
        evdev_paths: list[str],
        button_map: dict[str, str],
        button_codes: dict[str, int] | None = None,
        force_grab_unmapped: bool = False,
        *,
        update_desired: bool = True,
    ) -> JsonObject:
        return await runtime_grab_lifecycle.grab_device_unlocked(
            self,
            hardware_id,
            evdev_paths,
            button_map,
            button_codes,
            force_grab_unmapped,
            update_desired=update_desired,
            clear_device_path_cache_fn=clear_device_path_cache,
            resolve_stable_path_fn=resolve_stable_path,
            primary_input_class_fn=primary_input_class,
            grabbed_device_cls=GrabbedDevice,
            log=log,
            errno_mod=errno,
        )

    async def _grab_with_retry(self, device: "GrabbedDevice", path: str) -> None:
        await runtime_grab_lifecycle.grab_with_retry(
            self,
            device,
            path,
            asyncio_mod=asyncio,
            log=log,
            errno_mod=errno,
        )

    def _device_has_mapped_buttons(
        self,
        caps: dict[int, Sequence[object]],
        mapped_evdev_names: set[str],
        mapped_codes: set[int] | None = None,
    ) -> bool:
        return runtime_grab_lifecycle.device_has_mapped_buttons(
            caps,
            mapped_evdev_names,
            mapped_codes,
            evdev_mod=evdev,
        )

    async def release_device(
        self,
        hardware_id: str,
        immediate: bool = False,
        grace_s: float | None = None,
    ) -> JsonObject:
        async with self._op_lock:
            if immediate:
                return await self._release_device_unlocked(hardware_id)
            return self._schedule_hardware_release_unlocked(hardware_id, grace_s=grace_s)

    async def _release_device_unlocked(self, hardware_id: str) -> JsonObject:
        return await runtime_grab_lifecycle.release_device_unlocked(self, hardware_id, log=log)

    def _schedule_hardware_release_unlocked(
        self,
        hardware_id: str,
        grace_s: float | None = None,
    ) -> JsonObject:
        return runtime_grab_lifecycle.schedule_hardware_release_unlocked(
            self,
            hardware_id,
            grace_s,
            asyncio_mod=asyncio,
            log=log,
        )

    async def _delayed_hardware_release(self, hardware_id: str, delay: float) -> None:
        await runtime_grab_lifecycle.delayed_hardware_release(
            self,
            hardware_id,
            delay,
            asyncio_mod=asyncio,
            log=log,
        )

    def _hardware_has_held_inputs(self, hardware_id: str) -> bool:
        return runtime_grab_lifecycle.hardware_has_held_inputs(self, hardware_id)

    def _cancel_pending_hardware_release(self, hardware_id: str) -> None:
        runtime_grab_lifecycle.cancel_pending_hardware_release(self, hardware_id)

    def _cancel_pending_interface_release(self, hardware_id: str, path: str) -> None:
        runtime_grab_lifecycle.cancel_pending_interface_release(self, hardware_id, path)

    def _cancel_pending_interface_releases_for_hardware(self, hardware_id: str) -> None:
        runtime_grab_lifecycle.cancel_pending_interface_releases_for_hardware(self, hardware_id)

    def _schedule_interface_release(self, hardware_id: str, path: str) -> None:
        runtime_grab_lifecycle.schedule_interface_release(
            self,
            hardware_id,
            path,
            asyncio_mod=asyncio,
            log=log,
        )

    async def _delayed_interface_release(self, hardware_id: str, path: str, delay: float) -> None:
        await runtime_grab_lifecycle.delayed_interface_release(
            self,
            hardware_id,
            path,
            delay,
            asyncio_mod=asyncio,
        )

    async def _release_interface_unlocked(self, hardware_id: str, path: str) -> None:
        await runtime_grab_lifecycle.release_interface_unlocked(self, hardware_id, path)

    async def release_all_devices(self) -> None:
        await runtime_grab_lifecycle.release_all_devices(self)

    async def set_mapping(
        self,
        hardware_id: str,
        mapping: JsonObject,
    ) -> JsonObject:
        return await runtime_grab_lifecycle.set_mapping(
            self,
            hardware_id,
            mapping,
            json_object_fn=_json_object,
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
                        action=self._parse_action(parsed_action_data),
                        profile_name=_str_value(combo_dict.get("profile_name"), ""),
                    )
                )

            self.active_combos = parsed
            await self._clear_combo_runtime()
            self._combo_engine.set_combos(parsed)
            self._refresh_combo_timeout_watchdog()
            log.info("Updated combos (%d active)", len(parsed))
            return {"updated": True, "combo_count": len(parsed)}

    async def set_diagnostics(self, enabled: bool, interval: float = 5.0) -> JsonObject:
        self._diagnostics_enabled = bool(enabled)
        self._diagnostics_interval = max(0.5, float(interval or 5.0))

        if not self._diagnostics_enabled:
            if self._diagnostics_task:
                self._diagnostics_task.cancel()
                try:
                    await self._diagnostics_task
                except asyncio.CancelledError:
                    pass
                self._diagnostics_task = None
            self._diag_samples.clear()
            log.info("Diagnostics disabled")
            return {"enabled": False, "interval": self._diagnostics_interval}

        if self._diagnostics_task is None or self._diagnostics_task.done():
            self._diagnostics_task = asyncio.create_task(self._diagnostics_loop())
        log.info("Diagnostics enabled (interval %.2fs)", self._diagnostics_interval)
        return {"enabled": True, "interval": self._diagnostics_interval}

    def _record_diagnostic(self, label: str, duration_us: float) -> None:
        if not self._diagnostics_enabled:
            return
        bucket = self._diag_samples.setdefault(label, deque(maxlen=20000))
        bucket.append(float(duration_us))

    async def _diagnostics_loop(self) -> None:
        try:
            while self._diagnostics_enabled:
                await asyncio.sleep(self._diagnostics_interval)
                snapshot = {
                    label: list(samples) for label, samples in self._diag_samples.items() if samples
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

    def _parse_action(self, action_data: JsonObject | str) -> MappingAction:
        return runtime_actions.parse_action(
            self,
            action_data,
            str_value=_str_value,
            optional_str=_optional_str,
            int_value=_int_value,
            int_or_none=_int_or_none,
            float_value=_float_value,
        )

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
            asyncio_mod=asyncio,
        )

    async def cancel_macro_playback(self) -> JsonObject:
        return await runtime_macros.cancel_macro_playback(self)

    def _running_macro_instance_ids(self) -> list[int]:
        return runtime_macros.running_macro_instance_ids(self)

    def _find_matching_macro_instances(
        self,
        *,
        loop_mode: str | None = None,
        source_key: tuple[str, str] | None = None,
    ) -> list[int]:
        return runtime_macros.find_matching_macro_instances(
            self,
            loop_mode=loop_mode,
            source_key=source_key,
        )

    async def _cancel_macro_instances(self, instance_ids: list[int]) -> int:
        return await runtime_macros.cancel_macro_instances(
            self,
            instance_ids,
            asyncio_mod=asyncio,
            contextlib_mod=contextlib,
        )

    def _complete_all_macro_exec_waiters(self, returncode: int) -> None:
        runtime_macros.complete_all_macro_exec_waiters(self, returncode)

    async def _play_macro_task(
        self,
        instance_id: int,
        macro_events: list[JsonObject],
        macro_name: str,
        replay_mouse_movement: bool,
        replay_mouse_clicks: bool,
        speed: float,
        loop_mode: str,
        loop_count: int,
        move_to_start: bool,
        start_x: int,
        start_y: int,
        block_mouse_movement: bool,
    ) -> None:
        await runtime_macros.play_macro_task(
            self,
            instance_id,
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
            asyncio_mod=asyncio,
            evdev_mod=evdev,
            log=log,
            int_value_fn=_int_value,
            str_value_fn=_str_value,
            uinput_writer=_uinput_writer,
        )

    def _track_macro_key_press(self, instance_id: int, device_class: str, code: int) -> None:
        runtime_macros.track_macro_key_press(self, instance_id, device_class, code)

    def _track_macro_key_release(self, instance_id: int, device_class: str, code: int) -> None:
        runtime_macros.track_macro_key_release(self, instance_id, device_class, code)

    def _release_macro_held_for_instance(self, instance_id: int) -> None:
        runtime_macros.release_macro_held_for_instance(
            self,
            instance_id,
            evdev_mod=evdev,
            uinput_writer=_uinput_writer,
        )

    def _acquire_macro_mouse_inhibit(self, timeout_s: float) -> None:
        runtime_macros.acquire_macro_mouse_inhibit(self, timeout_s)

    def _release_macro_mouse_inhibit(self) -> None:
        runtime_macros.release_macro_mouse_inhibit(self)

    def _emit_absolute_mouse_move(self, x: int, y: int) -> None:
        runtime_macros.emit_absolute_mouse_move(
            self,
            x,
            y,
            evdev_mod=evdev,
            uinput_writer=_uinput_writer,
        )

    async def _run_macro_control_action(self, ev: JsonObject, speed: float) -> None:
        await runtime_macros.run_macro_control_action(
            self,
            ev,
            speed,
            asyncio_mod=asyncio,
            contextlib_mod=contextlib,
            random_mod=random,
            uuid_mod=uuid,
            command_type=CommandType,
            str_value_fn=_str_value,
            int_value_fn=_int_value,
        )

    def complete_macro_exec_wait(self, wait_id: str, returncode: int) -> JsonObject:
        return runtime_macros.complete_macro_exec_wait(self, wait_id, returncode)

    def begin_mouse_rel_suppression(self, timeout_s: float = 6.0) -> None:
        runtime_macros.begin_mouse_rel_suppression(self, timeout_s, asyncio_mod=asyncio)

    def end_mouse_rel_suppression(self) -> None:
        runtime_macros.end_mouse_rel_suppression(self)

    async def _mouse_rel_suppression_watchdog(self, timeout_s: float) -> None:
        await runtime_macros.mouse_rel_suppression_watchdog(
            self,
            timeout_s,
            asyncio_mod=asyncio,
        )

    def _parse_superkey_config(self, data: object) -> SuperkeyConfig:
        return runtime_actions.parse_superkey_config(
            self,
            data,
            json_object=_json_object,
            str_value=_str_value,
            int_value=_int_value,
            parse_superkey_action=runtime_actions.parse_superkey_action,
        )

    def _parse_superkey_action(self, data: object | None) -> SuperkeyActionData | None:
        return runtime_actions.parse_superkey_action(
            self,
            data,
            json_object=_json_object,
            str_value=_str_value,
            optional_str=_optional_str,
            int_or_none=_int_or_none,
            int_value=_int_value,
        )

    async def _on_device_event(
        self,
        hardware_id: str,
        evdev_path: str,
        event_type: int,
        event_code: int,
        event_value: int,
        stable_path: str | None = None,
        source: str | None = None,
    ) -> ComboDecision | bool | None:
        return await runtime_combos.on_device_event(
            self,
            hardware_id,
            evdev_path,
            event_type,
            event_code,
            event_value,
            stable_path,
            source,
        )

    def _build_combo_event_payload(
        self,
        hardware_id: str,
        evdev_path: str,
        event_type: int,
        event_code: int,
        event_value: int,
        *,
        stable_path: str | None = None,
        source: str | None = None,
    ) -> JsonObject | None:
        return runtime_combos.build_combo_event_payload(
            hardware_id,
            evdev_path,
            event_type,
            event_code,
            event_value,
            stable_path=stable_path,
            source=source,
            evdev_mod=evdev,
            resolve_stable_path_fn=resolve_stable_path,
            get_interface_id_fn=get_interface_id,
        )

    def _queue_combo_capture_event(self, payload: JsonObject | None) -> bool:
        return runtime_combos.queue_combo_capture_event(self, payload, str_value_fn=_str_value)

    async def _process_runtime_combo_event(
        self, payload: JsonObject | None
    ) -> ComboDecision | None:
        return cast(
            ComboDecision | None,
            await runtime_combos.process_runtime_combo_event(
                self,
                payload,
                combo_binding_cls=RuntimeComboBinding,
                combo_input_event_cls=ComboInputEvent,
                int_value_fn=_int_value,
                str_value_fn=_str_value,
                time_mod=time,
            ),
        )

    def _emit_combo_recalls(self, recall_events: list[ComboSyntheticEvent]) -> None:
        runtime_combos.emit_combo_recalls(self, recall_events)

    def _find_grabbed_device_for_binding(
        self,
        binding: RuntimeComboBinding,
    ) -> "GrabbedDevice | None":
        return cast(
            GrabbedDevice | None, runtime_combos.find_grabbed_device_for_binding(self, binding)
        )

    def _held_combo_modifier_bindings_for_scope(
        self,
        hardware_id: str,
        source: str,
    ) -> set[RuntimeComboBinding]:
        return cast(
            set[RuntimeComboBinding],
            runtime_combos.held_combo_modifier_bindings_for_scope(
                self,
                hardware_id,
                source,
                combo_binding_cls=RuntimeComboBinding,
            ),
        )

    async def _apply_combo_action_transition(self, transition: ComboActionTransition) -> None:
        await runtime_combos.apply_combo_action_transition(self, transition)

    async def _broadcast_combo_action(self, data: JsonObject) -> None:
        await runtime_combos.broadcast_combo_action(
            self,
            data,
            fire_and_observe_fn=_fire_and_observe,
            command_type=CommandType,
        )

    def _emit_combo_mouse_move(self, action: MappingAction) -> None:
        emit_mouse_move(
            self._mouse_uinput,
            int(action.move_x),
            int(action.move_y),
            absolute=action.action_type == ActionType.MOUSE_MOVE_ABS,
        )

    def _prune_combo_action_task(self, combo_id: str, task: asyncio.Task[object] | None) -> None:
        runtime_combos.prune_combo_action_task(self, combo_id, task)

    async def _combo_tap_key(
        self,
        combo_id: str,
        uinput_dev: evdev.UInput | None,
        code: int,
        hold_ms: int,
    ) -> None:
        await runtime_combos.combo_tap_key(
            self,
            combo_id,
            uinput_dev,
            code,
            hold_ms,
            asyncio_mod=asyncio,
        )

    async def _combo_tap_trigger(self, combo_id: str, axis_code: int, hold_ms: int) -> None:
        await runtime_combos.combo_tap_trigger(
            self,
            combo_id,
            axis_code,
            hold_ms,
            asyncio_mod=asyncio,
        )

    async def start_combo_action(
        self,
        combo_id: str,
        action: MappingAction | None,
        trigger_binding: RuntimeComboBinding,
    ) -> None:
        await runtime_combos.start_combo_action(
            self,
            combo_id,
            action,
            trigger_binding,
            asyncio_mod=asyncio,
            log=log,
            action_type_enum=ActionType,
        )

    async def _start_combo_key_action(
        self,
        combo_id: str,
        action: MappingAction,
        uinput_dev: evdev.UInput | None,
    ) -> None:
        await runtime_combos.start_combo_key_action(
            self,
            combo_id,
            action,
            uinput_dev,
            asyncio_mod=asyncio,
        )

    async def stop_combo_action(self, combo_id: str) -> None:
        await runtime_combos.stop_combo_action(
            self,
            combo_id,
            asyncio_mod=asyncio,
            contextlib_mod=contextlib,
            mapping_action_cls=MappingAction,
        )

    async def _clear_combo_runtime(self) -> None:
        await runtime_combos.clear_combo_runtime(
            self,
            asyncio_mod=asyncio,
            contextlib_mod=contextlib,
        )

    async def _clear_combo_runtime_for_binding_scope(
        self,
        hardware_id: str,
        source: str | None = None,
    ) -> None:
        await runtime_combos.clear_combo_runtime_for_binding_scope(self, hardware_id, source)

    def _refresh_combo_timeout_watchdog(self) -> None:
        runtime_combos.refresh_combo_timeout_watchdog(self, asyncio_mod=asyncio)

    async def _combo_timeout_watchdog(self, deadline: float) -> None:
        await runtime_combos.combo_timeout_watchdog(
            self,
            deadline,
            asyncio_mod=asyncio,
            time_mod=time,
        )

    async def _combo_rapidfire_key(
        self,
        combo_id: str,
        uinput_dev: evdev.UInput | None,
        code: int,
        hold_ms: int,
        wait_ms: int,
    ) -> None:
        await runtime_combos.combo_rapidfire_key(
            self,
            combo_id,
            uinput_dev,
            code,
            hold_ms,
            wait_ms,
            asyncio_mod=asyncio,
        )

    async def _combo_rapidfire_trigger(
        self,
        combo_id: str,
        axis_code: int,
        hold_ms: int,
        wait_ms: int,
    ) -> None:
        await runtime_combos.combo_rapidfire_trigger(
            self,
            combo_id,
            axis_code,
            hold_ms,
            wait_ms,
            asyncio_mod=asyncio,
        )

    def _write_combo_key(
        self,
        uinput_dev: evdev.UInput | None,
        code: int,
        value: int,
    ) -> None:
        runtime_combos.write_combo_key(
            uinput_dev,
            code,
            value,
            evdev_mod=evdev,
            uinput_writer=_uinput_writer,
        )

    def _write_combo_trigger(self, axis_code: int, value: int) -> None:
        runtime_combos.write_combo_trigger(
            self,
            axis_code,
            value,
            evdev_mod=evdev,
            uinput_writer=_uinput_writer,
        )

    def _resolve_code(self, key_name: str) -> int | None:
        return resolve_output_code(key_name)

    def _get_trigger_axis(self, target: str) -> tuple[bool, int | None]:
        return get_trigger_axis(target)

    def begin_combo_capture(
        self,
        token: str,
        hardware_ids: set[str],
        notify_event: asyncio.Event | None = None,
    ) -> JsonObject:
        return runtime_combos.begin_combo_capture(
            self,
            token,
            hardware_ids,
            notify_event,
            queue_mod=queue,
        )

    def read_combo_capture(self, token: str) -> JsonObject:
        return runtime_combos.read_combo_capture(self, token, queue_mod=queue)

    def end_combo_capture(self, token: str) -> JsonObject:
        return runtime_combos.end_combo_capture(self, token)


GrabbedDevice = runtime_grabbed_device.GrabbedDevice
