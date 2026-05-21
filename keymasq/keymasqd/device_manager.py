import asyncio
import contextlib
import errno
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import evdev

from keymasq.common import devices as common_devices
from keymasq.common.combos import (
    EMERGENCY_CANCEL_COMBO_EVDEVS,
    is_emergency_cancel_combo_evdevs,
    normalize_combo_restore_keys,
)
from keymasq.common.devices import (
    clear_device_path_cache,
    detect_input_classes,
    get_interface_id,
    primary_input_class,
    resolve_stable_path,
)
from keymasq.common.ipc import CommandType
from keymasq.common.models import (
    ActionType,
    DeviceType,
    MappingAction,
    SuperkeyMode,
)
from keymasq.common.virtual_devices import (
    DEFAULT_VIRTUAL_GAMEPADS,
    clamp_virtual_gamepad_count,
    is_virtual_gamepad_output_id,
)
from keymasq.keymasqd.combo_engine import (
    ComboDecision,
    RuntimeCombo,
    RuntimeComboBinding,
    RuntimeComboStep,
)
from keymasq.keymasqd.output_helpers import emit_mouse_move, resolve_output_code
from keymasq.keymasqd.recording import RecordingManager
from keymasq.keymasqd.runtime import actions as runtime_actions
from keymasq.keymasqd.runtime import adapters as runtime_adapters
from keymasq.keymasqd.runtime import combos as runtime_combos
from keymasq.keymasqd.runtime import grab_lifecycle as runtime_grab_lifecycle
from keymasq.keymasqd.runtime import grabbed_device as runtime_grabbed_device
from keymasq.keymasqd.runtime import macros as runtime_macros
from keymasq.keymasqd.runtime import outputs as runtime_outputs
from keymasq.keymasqd.runtime import topology as runtime_topology
from keymasq.keymasqd.superkey_state import SuperkeyActionData, SuperkeyConfig

log = logging.getLogger("keymasqd.devices")
ACTIVE_KEY_IDLE_LOG_INTERVAL_S = 1.0
ACTIVE_KEY_IDLE_MAX_WAIT_S = 300.0
COMBO_HELD_REARM_MODIFIERS = frozenset({"shift", "ctrl", "alt", "meta"})
EMERGENCY_CANCEL_COMBO_ID_PREFIX = "__keymasq_emergency_cancel:"
EMERGENCY_CANCEL_COMBO_NAME = "Keymasq Emergency Cancel"
EMERGENCY_CANCEL_COMBO_PROFILE = "__keymasq_internal"
EMERGENCY_CANCEL_DOUBLE_TAP_WINDOW_MS = 200
TOPOLOGY_POLL_INTERVAL_S = 0.5
TOPOLOGY_DEBOUNCE_S = 0.5
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


ASYNCIO_RUNTIME = runtime_adapters.ASYNCIO_RUNTIME


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


def _uinput_device_path(uinput_dev: object | None) -> str | None:
    input_device = getattr(uinput_dev, "device", None)
    path = getattr(input_device, "path", None)
    return path if isinstance(path, str) and path else None


def _is_virtual_input(device: object) -> bool:
    phys = str(getattr(device, "phys", "") or "").lower()
    name = str(getattr(device, "name", "") or "").lower()
    return phys == "py-evdev-uinput" or name.startswith("keymasq-")


def _fire_and_observe(coro: Awaitable[object], label: str) -> asyncio.Task[object]:
    task = asyncio.ensure_future(coro)

    def _log_task_result(done: asyncio.Task[object]) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            exc = done.exception()
            if exc is not None:
                log.warning("%s failed: %s", label, exc)

    task.add_done_callback(_log_task_result)
    return task


def _topology_runtime_deps() -> runtime_topology.TopologyRuntimeDeps:
    return runtime_topology.TopologyRuntimeDeps(
        asyncio_mod=ASYNCIO_RUNTIME,
        clear_device_path_cache_fn=clear_device_path_cache,
        device_paths_fn=_device_paths,
        device_input_fn=_device_input,
        resolve_stable_path_fn=resolve_stable_path,
        get_interface_id_fn=get_interface_id,
        release_interface_fn=runtime_grab_lifecycle.release_interface_unlocked,
    )


def _macro_runtime_deps() -> runtime_macros.MacroRuntimeDeps:
    return runtime_macros.MacroRuntimeDeps(
        asyncio_mod=ASYNCIO_RUNTIME,
        evdev_mod=evdev,
        uinput_writer=runtime_adapters.identity_uinput_writer,
        log=log,
        int_value_fn=_int_value,
        str_value_fn=_str_value,
    )


def _combo_runtime_deps(
    *,
    resolve_code_fn: runtime_combos.ResolveCodeFn = resolve_output_code,
    fire_and_observe_fn: runtime_combos.FireAndObserve = _fire_and_observe,
) -> runtime_combos.ComboRuntimeDeps:
    return runtime_combos.ComboRuntimeDeps(
        asyncio_mod=ASYNCIO_RUNTIME,
        evdev_mod=runtime_adapters.COMBO_EVDEV_RUNTIME,
        uinput_writer=runtime_adapters.identity_uinput_writer,
        emit_mouse_move_fn=runtime_adapters.combo_emit_mouse_move,
        resolve_code_fn=resolve_code_fn,
        fire_and_observe_fn=fire_and_observe_fn,
    )


def combo_runtime_signature(combo: RuntimeCombo) -> tuple[object, ...]:
    steps = tuple(
        (
            tuple(
                sorted(
                    (
                        str(binding.hardware_id or "").lower(),
                        str(binding.source or "").lower(),
                        str(binding.evdev or "").lower(),
                    )
                    for binding in step.bindings
                )
            ),
            step.timeout_ms,
        )
        for step in combo.steps
    )
    return (
        str(combo.id or ""),
        str(combo.profile_name or ""),
        steps,
        combo.action,
        bool(combo.recall_trigger_keys),
        tuple(combo.restore_trigger_keys),
    )


def combo_runtime_signatures(combos: Sequence[RuntimeCombo]) -> dict[str, tuple[object, ...]]:
    return {
        combo.id: combo_runtime_signature(combo)
        for combo in combos
        if combo.id
    }


def unchanged_combo_ids(
    old_signatures: dict[str, tuple[object, ...]],
    new_combos: Sequence[RuntimeCombo],
) -> set[str]:
    preserved: set[str] = set()
    seen: set[str] = set()
    for combo in new_combos:
        if not combo.id or combo.id in seen:
            continue
        seen.add(combo.id)
        if old_signatures.get(combo.id) == combo_runtime_signature(combo):
            preserved.add(combo.id)
    return preserved


def _capability_device(
    device: _ManagedInputDevice,
) -> common_devices._CapabilityDevice:  # pyright: ignore[reportPrivateUsage]
    return cast(common_devices._CapabilityDevice, device)  # pyright: ignore[reportPrivateUsage]


LiveInterfaceInfo = runtime_topology.LiveInterfaceInfo


@dataclass
class DesiredGrabConfig:
    paths: set[str]
    button_map: dict[str, str]
    button_codes: dict[str, int] = field(default_factory=dict)
    button_values: dict[str, int] = field(default_factory=dict)
    analog_inputs: dict[str, object] = field(default_factory=dict)
    force_grab_unmapped: bool = False


@dataclass
class OutputRuntimeState:
    device_count: int = 0
    keyboard_uinput: runtime_adapters.WritableUInput | None = None
    mouse_uinput: runtime_adapters.WritableUInput | None = None
    virtual_gamepad_uinputs: dict[str, runtime_adapters.WritableUInput] = field(
        default_factory=dict
    )
    virtual_gamepad_count: int = DEFAULT_VIRTUAL_GAMEPADS

    @property
    def gamepad_uinput(self) -> runtime_adapters.WritableUInput | None:
        return self.virtual_gamepad_uinputs.get("virtual-gamepad-1")

    @gamepad_uinput.setter
    def gamepad_uinput(self, value: runtime_adapters.WritableUInput | None) -> None:
        if value is None:
            self.virtual_gamepad_uinputs.pop("virtual-gamepad-1", None)
        else:
            self.virtual_gamepad_uinputs["virtual-gamepad-1"] = value


@dataclass(frozen=True)
class GamepadOutputTarget:
    output_id: str
    uinput: object
    bucket: str
    is_virtual: bool
    analog_inputs: dict[str, object] = field(default_factory=dict)


@dataclass
class MacroRuntimeState:
    tasks: dict[int, asyncio.Task[None]] = field(default_factory=dict)
    instance_meta: dict[int, dict[str, object]] = field(default_factory=dict)
    instance_seq: int = 0
    instance_held: dict[int, set[tuple[str, int]]] = field(default_factory=dict)
    held_refcount: dict[tuple[str, int], int] = field(default_factory=dict)
    instance_held_abs: dict[int, set[tuple[str, int]]] = field(default_factory=dict)
    held_abs_refcount: dict[tuple[str, int], int] = field(default_factory=dict)
    cancel_instance_ids: set[int] = field(default_factory=set)
    mouse_inhibit_count: int = 0
    exec_waiters: dict[str, asyncio.Future[int]] = field(default_factory=dict)
    mouse_rel_suppressed: bool = False
    mouse_rel_suppression_watchdog_task: asyncio.Task[None] | None = None


@dataclass
class DiagnosticsState:
    enabled: bool = False
    interval: float = 5.0
    categories: set[str] = field(default_factory=lambda: {"mainline"})
    task: asyncio.Task[None] | None = None
    samples: dict[str, deque[float]] = field(default_factory=dict)


DIAGNOSTICS_CATEGORIES = frozenset({"mainline", "combo", "internal"})
DEFAULT_DIAGNOSTICS_CATEGORIES = frozenset({"mainline"})


def _normalize_diagnostics_categories(categories: Sequence[object] | None) -> set[str]:
    if not categories:
        return set(DEFAULT_DIAGNOSTICS_CATEGORIES)

    normalized = {
        str(category or "").strip().lower()
        for category in categories
        if str(category or "").strip()
    }
    if "all" in normalized:
        return set(DIAGNOSTICS_CATEGORIES)
    selected = normalized & DIAGNOSTICS_CATEGORIES
    return selected or set(DEFAULT_DIAGNOSTICS_CATEGORIES)


def _diagnostics_label_enabled(label: str, categories: set[str]) -> bool:
    label = str(label or "").lower()
    if "internal" in categories and _diagnostics_label_is_internal(label):
        return True
    if "combo" in categories and _diagnostics_label_is_combo(label):
        return True
    return "mainline" in categories and _diagnostics_label_is_mainline(label)


def _diagnostics_label_is_mainline(label: str) -> bool:
    return (
        label.startswith("action_")
        or label
        in {
            "passthrough_fast",
            "passthrough_mapped",
            "passthrough_other",
            "passthrough_syn",
        }
        or label == "wheel_passthrough"
    )


def _diagnostics_label_is_combo(label: str) -> bool:
    return label.startswith("combo_") and not label.startswith("combo_recalled_")


def _diagnostics_label_is_internal(label: str) -> bool:
    return label == "syn" or label in {
        "combo_recalled_repeat_suppressed",
        "combo_recalled_release_suppressed",
        "wheel_high_res_suppressed",
    }


def _diagnostics_int(value: object) -> int:
    if isinstance(value, (int, float, str, bytes)):
        return int(value)
    return 0


def _diagnostics_float(value: object) -> float:
    if isinstance(value, (int, float, str, bytes)):
        return float(value)
    return 0.0


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
        self.macro_store: Any | None = None
        self.macro_exec_timeout_max_ms = 30000
        self.emergency_cancel_combo_enabled = True
        self.macro_state = MacroRuntimeState()
        self._op_lock = asyncio.Lock()
        self.diagnostics_state = DiagnosticsState()
        self.grab_state = GrabRuntimeState(
            release_grace_s=max(0.01, float(release_grace_s)),
            held_release_retry_s=max(0.01, float(held_release_retry_s)),
        )
        self.combo_state = runtime_combos.ComboRuntimeState()
        self._configured_combos: list[RuntimeCombo] = []
        self.device_inspector_active_hardware_ids: set[str] = set()
        self.device_inspector_suppressed_hardware_ids: set[str] = set()
        self._device_inspector_event_sequence = 0
        self.topology_state = TopologyRuntimeState(
            poll_s=max(0.05, float(topology_poll_s)),
            debounce_s=max(0.05, float(topology_debounce_s)),
        )
        self._command_type = CommandType
        self._desired_grab_config_cls = DesiredGrabConfig
        self._device_input = _device_input
        self._gamepad_output_warning_at: dict[tuple[str, str], float] = {}

    def initialize_output_devices(self) -> None:
        runtime_outputs.create_global_uinputs(
            cast(Any, self),
            evdev_mod=evdev,  # pyright: ignore[reportArgumentType]
            log=log,
            uinput_writer=runtime_adapters.identity_uinput_writer,
        )

    def shutdown_output_devices(self) -> None:
        runtime_outputs.destroy_global_uinputs(cast(Any, self), log=log)

    async def set_virtual_gamepads(self, count: object) -> JsonObject:
        clamped_count = clamp_virtual_gamepad_count(count)
        async with self._op_lock:
            if clamped_count == self.output_state.virtual_gamepad_count:
                return {"status": "ok", "count": clamped_count}

            cancelled_rapidfire_tasks: list[asyncio.Task[None]] = []
            await runtime_combos.clear_combo_runtime(
                self,
                deps=runtime_grab_lifecycle.combo_runtime_deps(),
            )
            for devices in self.grabbed_devices.values():
                for device in devices:
                    cancelled_rapidfire_tasks.extend(
                        task
                        for task in list(device.state.rapidfire_tasks.values())
                        if not task.done()
                    )
                    device.release_tracked_outputs()
                    await device.reset_mapping_runtime_state()

            if cancelled_rapidfire_tasks:
                unique_tasks = list(dict.fromkeys(cancelled_rapidfire_tasks))
                for task in unique_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*unique_tasks, return_exceptions=True)

            if self.output_state.device_count > 0:
                runtime_outputs.configure_virtual_gamepads(
                    cast(Any, self),
                    clamped_count,
                    evdev_mod=evdev,  # pyright: ignore[reportArgumentType]
                    log=log,
                    uinput_writer=runtime_adapters.identity_uinput_writer,
                )
            else:
                self.output_state.virtual_gamepad_count = clamped_count
            log.info("Configured %d virtual gamepad output(s)", clamped_count)
            return {"status": "ok", "count": clamped_count}

    def resolve_gamepad_output(
        self,
        output_id: str | None,
        *,
        context: str = "",
    ) -> GamepadOutputTarget | None:
        explicit = output_id is not None
        resolved_id = str(output_id or "virtual-gamepad-1").strip()
        if not resolved_id:
            resolved_id = "virtual-gamepad-1"

        if is_virtual_gamepad_output_id(resolved_id):
            uinput = self.output_state.virtual_gamepad_uinputs.get(resolved_id)
            if uinput is None:
                reason = "virtual output is not configured"
                self._warn_gamepad_output_unavailable(resolved_id, reason, context, explicit)
                return None
            return GamepadOutputTarget(
                output_id=resolved_id,
                uinput=uinput,
                bucket=f"gamepad:{resolved_id}",
                is_virtual=True,
            )

        devices = self.grabbed_devices.get(resolved_id)
        if not devices:
            reason = "target hardware is not grabbed"
            self._warn_gamepad_output_unavailable(resolved_id, reason, context, explicit)
            return None

        for device in devices:
            device_type = getattr(device, "device_type", None)
            raw_device_types = getattr(device, "device_types", None)
            device_types: set[object] = set()
            if isinstance(raw_device_types, Sequence):
                device_types = set(cast(Sequence[object], raw_device_types))
            if device_type == DeviceType.GAMEPAD or DeviceType.GAMEPAD in device_types:
                uinput = getattr(device, "uinput", None)
                if uinput is not None:
                    return GamepadOutputTarget(
                        output_id=resolved_id,
                        uinput=uinput,
                        bucket=f"gamepad:{resolved_id}",
                        is_virtual=False,
                        analog_inputs=dict(getattr(device, "analog_inputs", {}) or {}),
                    )
        reason = "target hardware has no grabbed gamepad passthrough output"
        self._warn_gamepad_output_unavailable(resolved_id, reason, context, explicit)
        return None

    def _warn_gamepad_output_unavailable(
        self,
        output_id: str,
        reason: str,
        context: str,
        explicit: bool,
    ) -> None:
        key = (output_id, reason)
        now = time.monotonic()
        last = self._gamepad_output_warning_at.get(key)
        if last is not None and now - last < 5.0:
            return
        self._gamepad_output_warning_at[key] = now
        prefix = f"Gamepad output target {output_id} unavailable"
        context_text = f" for {context}" if context else ""
        mode_text = "" if explicit else " default"
        log.warning("%s%s%s: %s; dropping output", prefix, mode_text, context_text, reason)

    @property
    def active_combos(self) -> list[RuntimeCombo]:
        return self.combo_state.active_combos

    @active_combos.setter
    def active_combos(self, value: list[RuntimeCombo]) -> None:
        self.combo_state.active_combos = value

    async def start_topology_watcher(self) -> None:
        await runtime_topology.start_topology_watcher(
            self,
            log=log,
            deps=_topology_runtime_deps(),
        )

    async def stop_topology_watcher(self) -> None:
        await runtime_topology.stop_topology_watcher(
            self,
            deps=_topology_runtime_deps(),
        )

    async def grab_device(
        self,
        hardware_id: str,
        evdev_paths: list[str],
        button_map: dict[str, str],
        button_codes: dict[str, int] | None = None,
        button_values: dict[str, int] | None = None,
        analog_inputs: dict[str, object] | None = None,
        force_grab_unmapped: bool = False,
    ) -> JsonObject:
        async with self._op_lock:
            result = await runtime_grab_lifecycle.grab_device_unlocked(
                self,
                hardware_id,
                evdev_paths,
                button_map,
                button_codes,
                button_values,
                analog_inputs,
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
            await self._refresh_combo_runtime_preserving_unchanged()
            return result

    async def release_device(
        self,
        hardware_id: str,
        immediate: bool = False,
        grace_s: float | None = None,
    ) -> JsonObject:
        async with self._op_lock:
            if immediate:
                result = await runtime_grab_lifecycle.release_device_unlocked(
                    self,
                    hardware_id,
                    log=log,
                )
                await self._refresh_combo_runtime_preserving_unchanged()
                return result
            return runtime_grab_lifecycle.schedule_hardware_release_unlocked(
                self,
                hardware_id,
                grace_s,
                asyncio_mod=runtime_grab_lifecycle.ASYNCIO_RUNTIME,
                log=log,
            )

    async def release_all_devices(self) -> None:
        await runtime_grab_lifecycle.release_all_devices(
            self,
            fire_and_observe_fn=_fire_and_observe,
        )
        async with self._op_lock:
            await self._refresh_combo_runtime_unlocked()

    async def emergency_reset(self) -> JsonObject:
        await self.release_all_devices()
        self._broadcast_runtime_event(
            CommandType.RUNTIME_RESET,
            {"reason": "emergency_reset"},
        )
        return {"status": "ok", "reset": True}

    def _broadcast_runtime_event(
        self,
        event_type: CommandType,
        data: JsonObject,
    ) -> None:
        if self.broadcast_callback is None:
            return
        _fire_and_observe(
            self.broadcast_callback(event_type, data),
            f"{event_type.value} broadcast",
        )

    def device_inspector_active(self, hardware_id: str) -> bool:
        return str(hardware_id or "").strip() in self.device_inspector_active_hardware_ids

    def device_inspector_suppressed(self, hardware_id: str) -> bool:
        return str(hardware_id or "").strip() in self.device_inspector_suppressed_hardware_ids

    def device_inspector_suppressed_hardware_ids_snapshot(self) -> set[str]:
        return set(self.device_inspector_suppressed_hardware_ids)

    def broadcast_device_inspector_event(self, payload: JsonObject) -> None:
        hardware_id = str(payload.get("hardware_id", "") or "").strip()
        if not hardware_id or not self.device_inspector_active(hardware_id):
            return
        self._device_inspector_event_sequence += 1
        event_payload = dict(payload)
        event_payload["sequence"] = self._device_inspector_event_sequence
        self._broadcast_runtime_event(CommandType.DEVICE_INSPECTOR_EVENT, event_payload)

    def _broadcast_device_inspector_status(self, hardware_id: str, reason: str) -> None:
        normalized_hardware_id = str(hardware_id or "").strip()
        self._broadcast_runtime_event(
            CommandType.DEVICE_INSPECTOR_STATUS,
            {
                "hardware_id": normalized_hardware_id,
                "active": self.device_inspector_active(normalized_hardware_id),
                "suppressed": self.device_inspector_suppressed(normalized_hardware_id),
                "reason": str(reason or ""),
            },
        )

    async def start_device_inspector(self, hardware_id: str) -> JsonObject:
        normalized_hardware_id = str(hardware_id or "").strip()
        if not normalized_hardware_id:
            raise ValueError("hardware_id required")
        async with self._op_lock:
            self.device_inspector_active_hardware_ids.add(normalized_hardware_id)
            self._broadcast_device_inspector_status(normalized_hardware_id, "start")
        return {
            "status": "ok",
            "hardware_id": normalized_hardware_id,
            "active": True,
            "suppressed": self.device_inspector_suppressed(normalized_hardware_id),
        }

    async def stop_device_inspector(self, hardware_id: str) -> JsonObject:
        normalized_hardware_id = str(hardware_id or "").strip()
        if not normalized_hardware_id:
            raise ValueError("hardware_id required")
        async with self._op_lock:
            was_suppressed = self.device_inspector_suppressed(normalized_hardware_id)
            self.device_inspector_suppressed_hardware_ids.discard(normalized_hardware_id)
            self.device_inspector_active_hardware_ids.discard(normalized_hardware_id)
            if was_suppressed:
                await self._reset_device_inspector_runtime_unlocked(normalized_hardware_id)
                await self._refresh_combo_runtime_unlocked()
            self._broadcast_device_inspector_status(normalized_hardware_id, "stop")
        return {
            "status": "ok",
            "hardware_id": normalized_hardware_id,
            "active": False,
            "suppressed": False,
        }

    async def enable_device_inspector_suppression(self, hardware_id: str) -> JsonObject:
        normalized_hardware_id = str(hardware_id or "").strip()
        if not normalized_hardware_id:
            raise ValueError("hardware_id required")
        async with self._op_lock:
            self.device_inspector_active_hardware_ids.add(normalized_hardware_id)
            self.device_inspector_suppressed_hardware_ids.add(normalized_hardware_id)
            await self._reset_device_inspector_runtime_unlocked(normalized_hardware_id)
            await self._refresh_combo_runtime_unlocked()
            self._broadcast_device_inspector_status(normalized_hardware_id, "enable_suppression")
        return {
            "status": "ok",
            "hardware_id": normalized_hardware_id,
            "active": True,
            "suppressed": True,
        }

    async def disable_device_inspector_suppression(
        self,
        hardware_id: str,
        reason: str = "manual",
    ) -> JsonObject:
        normalized_hardware_id = str(hardware_id or "").strip()
        if not normalized_hardware_id:
            raise ValueError("hardware_id required")
        async with self._op_lock:
            self.device_inspector_active_hardware_ids.add(normalized_hardware_id)
            was_suppressed = normalized_hardware_id in self.device_inspector_suppressed_hardware_ids
            self.device_inspector_suppressed_hardware_ids.discard(normalized_hardware_id)
            if was_suppressed:
                await self._reset_device_inspector_runtime_unlocked(normalized_hardware_id)
                await self._refresh_combo_runtime_unlocked()
            self._broadcast_device_inspector_status(normalized_hardware_id, reason)
        return {
            "status": "ok",
            "hardware_id": normalized_hardware_id,
            "active": True,
            "suppressed": False,
            "reason": str(reason or ""),
        }

    async def _reset_device_inspector_runtime_unlocked(self, hardware_id: str) -> None:
        for device in self.grabbed_devices.get(hardware_id, []):
            release_tracked_outputs = getattr(device, "release_tracked_outputs", None)
            if callable(release_tracked_outputs):
                release_tracked_outputs()
            reset_mapping_runtime_state = getattr(device, "reset_mapping_runtime_state", None)
            if callable(reset_mapping_runtime_state):
                await cast(Awaitable[object], reset_mapping_runtime_state())

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
                        recall_trigger_keys=bool(combo_dict.get("recall_trigger_keys", False)),
                        restore_trigger_keys=normalize_combo_restore_keys(
                            _json_list(combo_dict.get("restore_trigger_keys"))
                        ),
                    )
                )

            old_active_signatures = combo_runtime_signatures(self.active_combos)
            new_active_combos = self._with_emergency_cancel_combos(parsed)
            unchanged_ids = unchanged_combo_ids(old_active_signatures, new_active_combos)
            preserve_combo_ids = unchanged_ids if unchanged_ids else None
            self._configured_combos = parsed
            active_combos = await self._refresh_combo_runtime_unlocked(
                preserve_combo_ids=preserve_combo_ids,
            )
            log.info(
                "Updated combos (%d active, %d configured)",
                len(active_combos),
                len(parsed),
            )
            return {"updated": True, "combo_count": len(active_combos)}

    async def _refresh_combo_runtime_unlocked(
        self,
        *,
        preserve_combo_ids: set[str] | None = None,
    ) -> list[RuntimeCombo]:
        active_combos = self._with_emergency_cancel_combos(self._configured_combos)
        self.active_combos = active_combos
        if preserve_combo_ids is None:
            await runtime_combos.clear_combo_runtime(
                self,
                deps=_combo_runtime_deps(),
            )
            self.combo_state.engine.set_combos(active_combos)
        else:
            await runtime_combos.clear_combo_runtime_except(
                self,
                preserve_combo_ids,
                deps=_combo_runtime_deps(),
            )
            self.combo_state.engine.set_combos(
                active_combos,
                preserve_candidate_ids=preserve_combo_ids,
            )
        runtime_combos.prime_combo_engine_with_held_bindings(
            self,
        )
        runtime_combos.refresh_combo_timeout_watchdog(
            self,
            deps=_combo_runtime_deps(),
        )
        return active_combos

    async def _refresh_combo_runtime_preserving_unchanged(self) -> list[RuntimeCombo]:
        unchanged_ids = unchanged_combo_ids(
            combo_runtime_signatures(self.active_combos),
            self._with_emergency_cancel_combos(self._configured_combos),
        )
        return await self._refresh_combo_runtime_unlocked(
            preserve_combo_ids=unchanged_ids if unchanged_ids else None,
        )

    def _with_emergency_cancel_combos(
        self,
        combos: list[RuntimeCombo],
    ) -> list[RuntimeCombo]:
        if not self.emergency_cancel_combo_enabled:
            return combos

        hardware_ids = self._grabbed_keyboard_hardware_ids()
        if not hardware_ids:
            return combos

        hardware_id_set = set(hardware_ids)
        user_combos = [
            combo
            for combo in combos
            if not self._is_emergency_cancel_duplicate(combo, hardware_id_set)
        ]
        emergency_combos = [
            self._emergency_cancel_combo(hardware_id) for hardware_id in hardware_ids
        ]
        return [*emergency_combos, *user_combos]

    def _grabbed_keyboard_hardware_ids(self) -> list[str]:
        hardware_ids: list[str] = []
        for raw_hardware_id, devices in self.grabbed_devices.items():
            hardware_id = str(raw_hardware_id or "").lower()
            if not hardware_id:
                continue
            if any(self._grabbed_device_is_keyboard(device) for device in devices):
                hardware_ids.append(hardware_id)
        return sorted(set(hardware_ids))

    def _grabbed_device_is_keyboard(self, device: object) -> bool:
        device_type = getattr(device, "device_type", None)
        if device_type == DeviceType.KEYBOARD:
            return True
        if str(getattr(device_type, "value", device_type) or "").lower() == "keyboard":
            return True

        raw_types = getattr(device, "device_types", ())
        if not isinstance(raw_types, (list, tuple, set, frozenset)):
            return False
        raw_type_items = cast(Sequence[object] | set[object] | frozenset[object], raw_types)
        return any(str(raw_type or "").lower() == "keyboard" for raw_type in raw_type_items)

    def _emergency_cancel_combo(self, hardware_id: str) -> RuntimeCombo:
        bindings = tuple(
            RuntimeComboBinding(
                hardware_id=hardware_id,
                evdev=evdev_name,
                source="",
            )
            for evdev_name in EMERGENCY_CANCEL_COMBO_EVDEVS
        )
        return RuntimeCombo(
            id=f"{EMERGENCY_CANCEL_COMBO_ID_PREFIX}{hardware_id}",
            name=EMERGENCY_CANCEL_COMBO_NAME,
            steps=[RuntimeComboStep(bindings=bindings)],
            action=MappingAction(
                action_type=ActionType.SUPERKEY,
                superkey_config=cast(
                    Any,
                    SuperkeyConfig(
                        name=EMERGENCY_CANCEL_COMBO_NAME,
                        mode=SuperkeyMode.PATTERN,
                        double_tap_window_ms=EMERGENCY_CANCEL_DOUBLE_TAP_WINDOW_MS,
                        tap_actions=[
                            SuperkeyActionData(action_type=ActionType.CANCEL_MACRO_PLAYBACK.value)
                        ],
                        double_tap_actions=[
                            SuperkeyActionData(action_type=ActionType.EMERGENCY_RESET.value),
                        ],
                    ),
                ),
            ),
            profile_name=EMERGENCY_CANCEL_COMBO_PROFILE,
            recall_trigger_keys=True,
            restore_trigger_keys=[],
        )

    def _is_emergency_cancel_duplicate(
        self,
        combo: RuntimeCombo,
        keyboard_hardware_ids: set[str],
    ) -> bool:
        if len(combo.steps) != 1:
            return False
        step = combo.steps[0]
        if not step.bindings:
            return False
        if not is_emergency_cancel_combo_evdevs(binding.evdev for binding in step.bindings):
            return False
        return all(binding.hardware_id in keyboard_hardware_ids for binding in step.bindings)

    async def set_diagnostics(
        self,
        enabled: bool,
        interval: float = 5.0,
        categories: Sequence[object] | None = None,
    ) -> JsonObject:
        self.diagnostics_state.enabled = bool(enabled)
        self.diagnostics_state.interval = max(0.5, float(interval or 5.0))
        self.diagnostics_state.categories = _normalize_diagnostics_categories(categories)
        self.diagnostics_state.samples.clear()

        if not self.diagnostics_state.enabled:
            if self.diagnostics_state.task:
                self.diagnostics_state.task.cancel()
                try:
                    await self.diagnostics_state.task
                except asyncio.CancelledError:
                    pass
                self.diagnostics_state.task = None
            log.info("Diagnostics disabled")
            return {
                "enabled": False,
                "interval": self.diagnostics_state.interval,
                "categories": sorted(self.diagnostics_state.categories),
            }

        if self.diagnostics_state.task is None or self.diagnostics_state.task.done():
            self.diagnostics_state.task = asyncio.create_task(self._diagnostics_loop())
        log.info(
            "Diagnostics enabled (interval %.2fs, categories=%s)",
            self.diagnostics_state.interval,
            ",".join(sorted(self.diagnostics_state.categories)),
        )
        return {
            "enabled": True,
            "interval": self.diagnostics_state.interval,
            "categories": sorted(self.diagnostics_state.categories),
        }

    def _record_diagnostic(self, label: str, duration_us: float) -> None:
        if not self.diagnostics_state.enabled:
            return
        if not _diagnostics_label_enabled(label, self.diagnostics_state.categories):
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
                    summary = await asyncio.to_thread(
                        self._summarize_diagnostics_snapshot,
                        snapshot,
                    )
                    self._broadcast_diagnostics_snapshot(summary)
                    await asyncio.to_thread(self._log_diagnostics_summary, summary)
        except asyncio.CancelledError:
            raise

    def _summarize_diagnostics_snapshot(
        self,
        snapshot: dict[str, list[float]],
    ) -> dict[str, JsonObject]:
        if not snapshot:
            return {}

        def pct(values: list[float], p: float) -> float:
            if not values:
                return 0.0
            idx = int((len(values) - 1) * p)
            return values[max(0, min(idx, len(values) - 1))]

        summary: dict[str, JsonObject] = {}
        for label, samples in snapshot.items():
            if not samples:
                continue
            values = sorted(samples)
            summary[label] = {
                "n": len(values),
                "p50": pct(values, 0.50),
                "p95": pct(values, 0.95),
                "p99": pct(values, 0.99),
                "max": values[-1],
            }
        return summary

    def _broadcast_diagnostics_snapshot(self, summary: dict[str, JsonObject]) -> None:
        if not summary or self.broadcast_callback is None:
            return
        self._broadcast_runtime_event(
            CommandType.DIAGNOSTICS_SNAPSHOT,
            {
                "enabled": True,
                "interval": self.diagnostics_state.interval,
                "categories": sorted(self.diagnostics_state.categories),
                "samples": summary,
            },
        )

    def _log_diagnostics_snapshot(self, snapshot: dict[str, list[float]]) -> None:
        self._log_diagnostics_summary(self._summarize_diagnostics_snapshot(snapshot))

    def _log_diagnostics_summary(self, summary: dict[str, JsonObject]) -> None:
        if not summary:
            return

        for label, stats in summary.items():
            log.info(
                "diagnostics[%s]: n=%d p50=%.2fus p95=%.2fus p99=%.2fus max=%.2fus",
                label,
                _diagnostics_int(stats.get("n", 0)),
                _diagnostics_float(stats.get("p50", 0.0)),
                _diagnostics_float(stats.get("p95", 0.0)),
                _diagnostics_float(stats.get("p99", 0.0)),
                _diagnostics_float(stats.get("max", 0.0)),
            )

    async def list_devices(self) -> JsonObject:
        return await asyncio.to_thread(self._list_devices_sync)

    def _list_devices_sync(self) -> JsonObject:
        clear_device_path_cache()
        devices: list[JsonObject] = []
        virtual_metadata = self._recording_virtual_device_metadata()
        grabbed_metadata = self._recording_grabbed_source_metadata()

        for path in _device_paths():
            device: _ManagedInputDevice | None = None
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
                stable_path = resolve_stable_path(path)
                interface_id = get_interface_id(stable_path)
                metadata = virtual_metadata.get(path, {})
                grabbed_source = grabbed_metadata.get(stable_path)
                is_grabbed = grabbed_source is not None
                recording_kind = str(metadata.get("recording_kind", "") or "")
                if not recording_kind:
                    recording_kind = (
                        "physical" if not _is_virtual_input(device) else "other_virtual"
                    )
                recording_id = str(metadata.get("recording_id", "") or "")
                if not recording_id:
                    recording_id = (
                        f"physical:{stable_path}"
                        if recording_kind == "physical"
                        else f"virtual:{device.name or path}:{stable_path}"
                    )

                devices.append(
                    {
                        "path": path,
                        "open_path": path,
                        "stable_path": stable_path,
                        "interface_id": str(interface_id or ""),
                        "name": device.name,
                        "phys": _optional_str(getattr(device, "phys", None)),
                        "uniq": _optional_str(getattr(device, "uniq", None)),
                        "vendor_id": f"{info.vendor:04x}",
                        "product_id": f"{info.product:04x}",
                        "capabilities": capabilities,
                        "device_types": device_types,
                        "device_type": device_type.value,
                        "recording_id": recording_id,
                        "recording_kind": recording_kind,
                        "grabbed_by_keymasq": is_grabbed,
                        **metadata,
                        **(grabbed_source or {}),
                    }
                )
            except Exception as e:
                log.debug(f"Could not read device {path}: {e}")
            finally:
                if device is not None:
                    with contextlib.suppress(Exception):
                        device.close()

        return {"devices": devices}

    def _recording_virtual_device_metadata(self) -> dict[str, JsonObject]:
        metadata: dict[str, JsonObject] = {}
        output_devices = {
            "keyboard": self.output_state.keyboard_uinput,
            "mouse": self.output_state.mouse_uinput,
        }
        for output_class, uinput_dev in output_devices.items():
            path = _uinput_device_path(uinput_dev)
            if not path:
                continue
            metadata[path] = {
                "recording_id": f"keymasq:output:{output_class}",
                "recording_kind": "keymasq_output",
                "keymasq_output": output_class,
            }
        for output_id, uinput_dev in sorted(self.output_state.virtual_gamepad_uinputs.items()):
            path = _uinput_device_path(uinput_dev)
            if not path:
                continue
            recording_id = (
                "keymasq:output:gamepad"
                if output_id == "virtual-gamepad-1"
                else f"keymasq:output:gamepad:{output_id}"
            )
            metadata[path] = {
                "recording_id": recording_id,
                "recording_kind": "keymasq_output",
                "keymasq_output": "gamepad",
                "keymasq_output_id": output_id,
            }

        for devices in self.grabbed_devices.values():
            for grabbed in devices:
                path = _uinput_device_path(getattr(grabbed, "uinput", None))
                if not path:
                    continue
                hardware_id = str(getattr(grabbed, "hardware_id", "") or "")
                interface_id = str(getattr(grabbed, "interface_id", "") or "")
                stable_path = str(getattr(grabbed, "stable_path", "") or "")
                metadata[path] = {
                    "recording_id": f"keymasq:passthrough:{hardware_id}:{interface_id}",
                    "recording_kind": "keymasq_passthrough",
                    "source_hardware_id": hardware_id,
                    "source_interface_id": interface_id,
                    "source_stable_path": stable_path,
                    "source_path": str(getattr(grabbed, "path", "") or ""),
                }
        return metadata

    def _recording_grabbed_source_metadata(self) -> dict[str, JsonObject]:
        metadata: dict[str, JsonObject] = {}
        for devices in self.grabbed_devices.values():
            for grabbed in devices:
                stable_path = str(getattr(grabbed, "stable_path", "") or "")
                if not stable_path:
                    continue
                metadata[stable_path] = {
                    "source_hardware_id": str(getattr(grabbed, "hardware_id", "") or ""),
                    "source_interface_id": str(getattr(grabbed, "interface_id", "") or ""),
                }
        return metadata

    def _detect_device_types(self, device: _ManagedInputDevice) -> list[str]:
        return detect_input_classes(_capability_device(device))

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
        loop_stop_behavior: str = "finish_run",
        move_to_start: bool = False,
        start_x: int = 0,
        start_y: int = 0,
        block_mouse_movement: bool = False,
        source_device: str = "",
        source_button: str = "",
        trigger_value: int = 1,
        macro_event_source: runtime_macros.MacroEventSource | None = None,
    ) -> JsonObject:
        if macro_event_source is None and macro_name and not macro_events:
            macro_event_source = await self._stored_macro_event_source(macro_name)
        return await runtime_macros.play_macro(
            self,
            macro_events,
            macro_name,
            replay_mouse_movement,
            replay_mouse_clicks,
            speed,
            loop_mode,
            loop_count,
            loop_stop_behavior,
            move_to_start,
            start_x,
            start_y,
            block_mouse_movement,
            source_device,
            source_button,
            trigger_value,
            deps=_macro_runtime_deps(),
            macro_event_source=macro_event_source,
        )

    async def _stored_macro_event_source(
        self,
        macro_name: str,
    ) -> runtime_macros.MacroEventSource | None:
        store = self.macro_store
        if store is None:
            return None
        get_meta = getattr(store, "get_meta", None)
        iter_events = getattr(store, "iter_events", None)
        if not callable(get_meta) or not callable(iter_events):
            return None

        meta_raw = await asyncio.to_thread(get_meta, macro_name)
        if not isinstance(meta_raw, dict):
            return None
        meta = cast(JsonObject, meta_raw)

        def iter_stored_events() -> Iterator[JsonObject]:
            return cast(Iterator[JsonObject], iter_events(macro_name))

        return runtime_macros.MacroEventSource(
            event_count=_int_value(meta.get("event_count"), 0),
            duration_us=_int_value(meta.get("duration_us"), 0),
            iter_events=iter_stored_events,
        )

    async def set_cursor_position(self, x: int, y: int) -> JsonObject:
        if self.output_state.mouse_uinput is None:
            return {"status": "error", "message": "No mouse uinput device available"}

        emit_mouse_move(
            self.output_state.mouse_uinput,
            int(x),
            int(y),
            absolute=True,
        )
        return {"status": "ok", "x": int(x), "y": int(y)}

    async def cancel_macro_playback(self) -> JsonObject:
        result = await runtime_macros.cancel_macro_playback(
            self,
            deps=_macro_runtime_deps(),
        )
        if bool(result.get("cancelled", False)):
            self._broadcast_runtime_event(
                CommandType.MACRO_PLAYBACK_CANCELLED,
                {"reason": "cancel_macro_playback", "cancelled": True},
            )
        return result

    def complete_macro_exec_wait(self, wait_id: str, returncode: int) -> JsonObject:
        return runtime_macros.complete_macro_exec_wait(self, wait_id, returncode)

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
        )

    def read_combo_capture(self, token: str) -> JsonObject:
        return runtime_combos.read_combo_capture(self, token)

    def end_combo_capture(self, token: str) -> JsonObject:
        return runtime_combos.end_combo_capture(self, token)


GrabbedDevice = runtime_grabbed_device.GrabbedDevice
