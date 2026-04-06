import asyncio
import contextlib
import errno
import logging
import queue
import random
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import evdev

from keyforge.common.combos import normalize_combo_evdev
from keyforge.common.devices import (
    canonical_gamepad_button_name,
    classify_event_device_type,
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
from keyforge.common.models import (
    SuperkeyConfig as CommonSuperkeyConfig,
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
from keyforge.keyforged.superkey_state import (
    SuperkeyActionData,
    SuperkeyConfig,
    SuperkeyMachine,
)

log = logging.getLogger("keyforged.devices")
ACTIVE_KEY_IDLE_LOG_INTERVAL_S = 1.0
ACTIVE_KEY_IDLE_MAX_WAIT_S = 300.0
COMBO_HELD_REARM_MODIFIERS = frozenset({"shift", "ctrl", "alt", "meta"})
TOPOLOGY_POLL_INTERVAL_S = 0.5
TOPOLOGY_DEBOUNCE_S = 0.5
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

    def _create_global_uinputs(self) -> None:
        if self._device_count == 0:
            log.info("Creating global output uinput devices")

            keyboard_caps = {
                evdev.ecodes.EV_KEY: [
                    evdev.ecodes.KEY_ESC,
                    evdev.ecodes.KEY_1,
                    evdev.ecodes.KEY_2,
                    evdev.ecodes.KEY_3,
                    evdev.ecodes.KEY_4,
                    evdev.ecodes.KEY_5,
                    evdev.ecodes.KEY_6,
                    evdev.ecodes.KEY_7,
                    evdev.ecodes.KEY_8,
                    evdev.ecodes.KEY_9,
                    evdev.ecodes.KEY_0,
                    evdev.ecodes.KEY_MINUS,
                    evdev.ecodes.KEY_EQUAL,
                    evdev.ecodes.KEY_BACKSPACE,
                    evdev.ecodes.KEY_TAB,
                    evdev.ecodes.KEY_Q,
                    evdev.ecodes.KEY_W,
                    evdev.ecodes.KEY_E,
                    evdev.ecodes.KEY_R,
                    evdev.ecodes.KEY_T,
                    evdev.ecodes.KEY_Y,
                    evdev.ecodes.KEY_U,
                    evdev.ecodes.KEY_I,
                    evdev.ecodes.KEY_O,
                    evdev.ecodes.KEY_P,
                    evdev.ecodes.KEY_LEFTBRACE,
                    evdev.ecodes.KEY_RIGHTBRACE,
                    evdev.ecodes.KEY_ENTER,
                    evdev.ecodes.KEY_LEFTCTRL,
                    evdev.ecodes.KEY_A,
                    evdev.ecodes.KEY_S,
                    evdev.ecodes.KEY_D,
                    evdev.ecodes.KEY_F,
                    evdev.ecodes.KEY_G,
                    evdev.ecodes.KEY_H,
                    evdev.ecodes.KEY_J,
                    evdev.ecodes.KEY_K,
                    evdev.ecodes.KEY_L,
                    evdev.ecodes.KEY_SEMICOLON,
                    evdev.ecodes.KEY_APOSTROPHE,
                    evdev.ecodes.KEY_GRAVE,
                    evdev.ecodes.KEY_LEFTSHIFT,
                    evdev.ecodes.KEY_BACKSLASH,
                    evdev.ecodes.KEY_102ND,
                    evdev.ecodes.KEY_Z,
                    evdev.ecodes.KEY_X,
                    evdev.ecodes.KEY_C,
                    evdev.ecodes.KEY_V,
                    evdev.ecodes.KEY_B,
                    evdev.ecodes.KEY_N,
                    evdev.ecodes.KEY_M,
                    evdev.ecodes.KEY_COMMA,
                    evdev.ecodes.KEY_DOT,
                    evdev.ecodes.KEY_SLASH,
                    evdev.ecodes.KEY_RIGHTSHIFT,
                    evdev.ecodes.KEY_LEFTALT,
                    evdev.ecodes.KEY_LEFTMETA,
                    evdev.ecodes.KEY_SPACE,
                    evdev.ecodes.KEY_CAPSLOCK,
                    evdev.ecodes.KEY_F1,
                    evdev.ecodes.KEY_F2,
                    evdev.ecodes.KEY_F3,
                    evdev.ecodes.KEY_F4,
                    evdev.ecodes.KEY_F5,
                    evdev.ecodes.KEY_F6,
                    evdev.ecodes.KEY_F7,
                    evdev.ecodes.KEY_F8,
                    evdev.ecodes.KEY_F9,
                    evdev.ecodes.KEY_F10,
                    evdev.ecodes.KEY_F11,
                    evdev.ecodes.KEY_F12,
                    evdev.ecodes.KEY_RIGHTCTRL,
                    evdev.ecodes.KEY_RIGHTALT,
                    evdev.ecodes.KEY_RIGHTMETA,
                    evdev.ecodes.KEY_MENU,
                    evdev.ecodes.KEY_SYSRQ,
                    evdev.ecodes.KEY_SCROLLLOCK,
                    evdev.ecodes.KEY_PAUSE,
                    evdev.ecodes.KEY_HOME,
                    evdev.ecodes.KEY_UP,
                    evdev.ecodes.KEY_PAGEUP,
                    evdev.ecodes.KEY_LEFT,
                    evdev.ecodes.KEY_RIGHT,
                    evdev.ecodes.KEY_END,
                    evdev.ecodes.KEY_DOWN,
                    evdev.ecodes.KEY_PAGEDOWN,
                    evdev.ecodes.KEY_INSERT,
                    evdev.ecodes.KEY_DELETE,
                    evdev.ecodes.KEY_MUTE,
                    evdev.ecodes.KEY_VOLUMEDOWN,
                    evdev.ecodes.KEY_VOLUMEUP,
                    evdev.ecodes.KEY_NUMLOCK,
                    evdev.ecodes.KEY_KPSLASH,
                    evdev.ecodes.KEY_KPASTERISK,
                    evdev.ecodes.KEY_KPMINUS,
                    evdev.ecodes.KEY_KP7,
                    evdev.ecodes.KEY_KP8,
                    evdev.ecodes.KEY_KP9,
                    evdev.ecodes.KEY_KPPLUS,
                    evdev.ecodes.KEY_KP4,
                    evdev.ecodes.KEY_KP5,
                    evdev.ecodes.KEY_KP6,
                    evdev.ecodes.KEY_KP1,
                    evdev.ecodes.KEY_KP2,
                    evdev.ecodes.KEY_KP3,
                    evdev.ecodes.KEY_KPENTER,
                    evdev.ecodes.KEY_KP0,
                    evdev.ecodes.KEY_KPDOT,
                ],
                evdev.ecodes.EV_SYN: [],
            }
            self._keyboard_uinput = evdev.UInput(
                events=cast(dict[int, Sequence[int]], keyboard_caps),
                name="keyforge-keyboard",
            )

            mouse_caps = {
                evdev.ecodes.EV_KEY: [
                    evdev.ecodes.BTN_LEFT,
                    evdev.ecodes.BTN_RIGHT,
                    evdev.ecodes.BTN_MIDDLE,
                    evdev.ecodes.BTN_SIDE,
                    evdev.ecodes.BTN_EXTRA,
                    evdev.ecodes.BTN_FORWARD,
                    evdev.ecodes.BTN_BACK,
                ],
                evdev.ecodes.EV_REL: [
                    evdev.ecodes.REL_X,
                    evdev.ecodes.REL_Y,
                    evdev.ecodes.REL_WHEEL,
                    evdev.ecodes.REL_HWHEEL,
                ],
                evdev.ecodes.EV_SYN: [],
            }
            self._mouse_uinput = evdev.UInput(
                events=cast(dict[int, Sequence[int]], mouse_caps),
                name="keyforge-mouse",
            )

            gamepad_caps = {
                evdev.ecodes.EV_KEY: [
                    evdev.ecodes.BTN_SOUTH,
                    evdev.ecodes.BTN_EAST,
                    evdev.ecodes.BTN_NORTH,
                    evdev.ecodes.BTN_WEST,
                    evdev.ecodes.BTN_TL,
                    evdev.ecodes.BTN_TR,
                    evdev.ecodes.BTN_TL2,
                    evdev.ecodes.BTN_TR2,
                    evdev.ecodes.BTN_SELECT,
                    evdev.ecodes.BTN_START,
                    evdev.ecodes.BTN_MODE,
                    evdev.ecodes.BTN_THUMBL,
                    evdev.ecodes.BTN_THUMBR,
                    evdev.ecodes.BTN_DPAD_UP,
                    evdev.ecodes.BTN_DPAD_DOWN,
                    evdev.ecodes.BTN_DPAD_LEFT,
                    evdev.ecodes.BTN_DPAD_RIGHT,
                ],
                evdev.ecodes.EV_ABS: [
                    (evdev.ecodes.ABS_X, evdev.AbsInfo(0, -32768, 32767, 16, 128, 0)),
                    (evdev.ecodes.ABS_Y, evdev.AbsInfo(0, -32768, 32767, 16, 128, 0)),
                    (evdev.ecodes.ABS_RX, evdev.AbsInfo(0, -32768, 32767, 16, 128, 0)),
                    (evdev.ecodes.ABS_RY, evdev.AbsInfo(0, -32768, 32767, 16, 128, 0)),
                    (evdev.ecodes.ABS_Z, evdev.AbsInfo(0, 0, 255, 0, 0, 0)),
                    (evdev.ecodes.ABS_RZ, evdev.AbsInfo(0, 0, 255, 0, 0, 0)),
                    (evdev.ecodes.ABS_HAT0X, evdev.AbsInfo(0, -1, 1, 0, 0, 0)),
                    (evdev.ecodes.ABS_HAT0Y, evdev.AbsInfo(0, -1, 1, 0, 0, 0)),
                ],
                evdev.ecodes.EV_SYN: [],
            }
            self._gamepad_uinput = evdev.UInput(
                events=cast(dict[int, Sequence[int]], gamepad_caps),
                name="Microsoft X-Box 360 pad",
                vendor=0x045E,
                product=0x028E,
                version=0x0110,
                bustype=0x0003,
            )

            gamepad_uinput = _uinput_writer(self._gamepad_uinput)
            if gamepad_uinput is not None:
                gamepad_uinput.write(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_X, 0)
                gamepad_uinput.write(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Y, 0)
                gamepad_uinput.write(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RX, 0)
                gamepad_uinput.write(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RY, 0)
                gamepad_uinput.write(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0)
                gamepad_uinput.write(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RZ, 0)
                gamepad_uinput.write(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_HAT0X, 0)
                gamepad_uinput.write(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_HAT0Y, 0)
                gamepad_uinput.syn()

        self._device_count += 1

    def _destroy_global_uinputs(self) -> None:
        self._device_count = max(0, self._device_count - 1)

        if self._device_count == 0:
            log.info("Destroying global output uinput devices")

            for uinput_dev in [self._keyboard_uinput, self._mouse_uinput, self._gamepad_uinput]:
                if uinput_dev:
                    try:
                        uinput_dev.close()
                    except Exception as e:
                        log.warning(f"Failed to close global uinput device: {e}")

            self._keyboard_uinput = None
            self._mouse_uinput = None
            self._gamepad_uinput = None

    async def start_topology_watcher(self) -> None:
        if self._topology_task is not None and not self._topology_task.done():
            return
        snapshot = await asyncio.to_thread(self._scan_live_interfaces_sync)
        self._live_topology_snapshot = dict(snapshot)
        self._reconciled_topology_snapshot = dict(snapshot)
        self._topology_task = asyncio.create_task(self._topology_watch_loop())

    async def stop_topology_watcher(self) -> None:
        task = self._topology_task
        self._topology_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        reconcile_task = self._topology_reconcile_task
        self._topology_reconcile_task = None
        if reconcile_task is not None and not reconcile_task.done():
            reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reconcile_task

    async def _topology_watch_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._topology_poll_s)
                try:
                    snapshot = await asyncio.to_thread(self._scan_live_interfaces_sync)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("Topology scan failed: %s", e)
                    continue

                if snapshot != self._live_topology_snapshot:
                    self._live_topology_snapshot = dict(snapshot)
                    self._schedule_topology_reconcile(snapshot)
                    continue

                if snapshot != self._reconciled_topology_snapshot and (
                    self._topology_reconcile_task is None or self._topology_reconcile_task.done()
                ):
                    self._schedule_topology_reconcile(snapshot)
        except asyncio.CancelledError:
            raise

    def _schedule_topology_reconcile(self, snapshot: dict[str, LiveInterfaceInfo]) -> None:
        task = self._topology_reconcile_task
        if task is not None and not task.done():
            task.cancel()

        async def _run() -> None:
            try:
                await asyncio.sleep(self._topology_debounce_s)
                await self._reconcile_topology(snapshot)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Topology reconcile failed: %s", e)
            finally:
                current = self._topology_reconcile_task
                if current is asyncio.current_task():
                    self._topology_reconcile_task = None

        self._topology_reconcile_task = asyncio.create_task(_run())

    async def _reconcile_topology(self, snapshot: dict[str, LiveInterfaceInfo]) -> None:
        async with self._op_lock:
            previous = dict(self._reconciled_topology_snapshot)
            desired_hardware_ids = set(self._desired_grabs)
            events = self._build_topology_events(previous, snapshot, desired_hardware_ids)
            await self._reconcile_topology_unlocked(snapshot)
            self._reconciled_topology_snapshot = dict(snapshot)

        for event_type, payload in events:
            if self.broadcast_callback is None:
                continue
            try:
                await self.broadcast_callback(event_type, payload)
            except Exception as e:
                log.warning("Failed to broadcast topology event %s: %s", event_type.value, e)

    async def _reconcile_topology_unlocked(
        self,
        snapshot: dict[str, LiveInterfaceInfo],
    ) -> None:
        live_paths = set(snapshot)
        removed: list[tuple[str, str]] = []

        for hardware_id, devices in self.grabbed_devices.items():
            for device in devices:
                stable_path = str(getattr(device, "stable_path", "") or device.path)
                if stable_path not in live_paths:
                    removed.append((hardware_id, device.path))

        for hardware_id, path in removed:
            await self._release_interface_unlocked(hardware_id, path)

    def _build_topology_events(
        self,
        previous: dict[str, LiveInterfaceInfo],
        current: dict[str, LiveInterfaceInfo],
        desired_hardware_ids: set[str],
    ) -> list[tuple[CommandType, JsonObject]]:
        events: list[tuple[CommandType, JsonObject]] = []

        for stable_path in sorted(previous.keys() - current.keys()):
            info = previous[stable_path]
            if info.hardware_id not in desired_hardware_ids:
                continue
            events.append((CommandType.DEVICE_DISCONNECTED, self._live_interface_payload(info)))

        for stable_path in sorted(current.keys() - previous.keys()):
            info = current[stable_path]
            if info.hardware_id not in desired_hardware_ids:
                continue
            events.append((CommandType.DEVICE_CONNECTED, self._live_interface_payload(info)))

        return events

    def _live_interface_payload(self, info: LiveInterfaceInfo) -> JsonObject:
        return {
            "hardware_id": info.hardware_id,
            "vendor_id": info.vendor_id,
            "product_id": info.product_id,
            "path": info.path,
            "stable_path": info.stable_path,
            "interface_id": info.interface_id,
        }

    def _scan_live_interfaces_sync(self) -> dict[str, LiveInterfaceInfo]:
        clear_device_path_cache()
        snapshot: dict[str, LiveInterfaceInfo] = {}

        for path in _device_paths():
            try:
                device = _device_input(path)
                info = device.info
                vendor_id = f"{info.vendor:04x}"
                product_id = f"{info.product:04x}"
                hardware_id = f"{vendor_id}:{product_id}"
                stable_path = resolve_stable_path(path)
                snapshot[stable_path] = LiveInterfaceInfo(
                    hardware_id=hardware_id,
                    vendor_id=vendor_id,
                    product_id=product_id,
                    stable_path=stable_path,
                    path=path,
                    interface_id=str(get_interface_id(stable_path) or "").lower(),
                )
            except Exception as e:
                log.debug("Could not read live topology device %s: %s", path, e)

        return snapshot

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
        clear_device_path_cache()
        self._cancel_pending_hardware_release(hardware_id)

        requested_paths = {
            resolve_stable_path(str(path)) for path in evdev_paths if str(path or "").strip()
        }
        mapped_evdev_names = set(name.lower() for name in button_map.values())
        resolved_button_codes = {
            button_id: int(code) for button_id, code in (button_codes or {}).items()
        }
        mapped_codes = set(resolved_button_codes.values())
        if update_desired:
            self._desired_paths[hardware_id] = set(requested_paths)
            self._desired_grabs[hardware_id] = DesiredGrabConfig(
                paths=set(requested_paths),
                button_map=dict(button_map),
                button_codes=dict(resolved_button_codes),
                force_grab_unmapped=bool(force_grab_unmapped),
            )
        log.info(
            "Grab request for %s: paths=%d mapped_evdev_names=%d mapped_codes=%d",
            hardware_id,
            len(requested_paths),
            len(mapped_evdev_names),
            len(mapped_codes),
        )

        existing_by_path = {
            device.path: device for device in self.grabbed_devices.get(hardware_id, [])
        }
        for device in existing_by_path.values():
            device.update_button_map(button_map, resolved_button_codes)

        devices: list[GrabbedDevice] = list(existing_by_path.values())
        grabbed_count = 0
        skipped_count = 0
        available_count = 0
        created_global_uinputs = False

        for path in existing_by_path.keys():
            if path in requested_paths:
                self._cancel_pending_interface_release(hardware_id, path)

        for path in sorted(existing_by_path.keys() - requested_paths):
            self._schedule_interface_release(hardware_id, path)

        for path in sorted(requested_paths):
            if path in existing_by_path:
                continue
            try:
                raw_device = _device_input(path)
                available_count += 1
                caps = raw_device.capabilities()

                has_mapped_buttons = self._device_has_mapped_buttons(
                    caps,
                    mapped_evdev_names,
                    mapped_codes,
                )

                if has_mapped_buttons or force_grab_unmapped:
                    if hardware_id not in self.grabbed_devices and not created_global_uinputs:
                        self._create_global_uinputs()
                        created_global_uinputs = True
                    detected_types = self._detect_device_types(raw_device)
                    detected_type = primary_input_class(detected_types)

                    def mapping_getter(hid: str = hardware_id) -> dict[str, MappingAction]:
                        return self.active_mappings.get(hid, {})

                    device = GrabbedDevice(
                        path=path,
                        hardware_id=hardware_id,
                        button_map=button_map,
                        button_codes=resolved_button_codes,
                        mapping_getter=mapping_getter,
                        event_callback=self._on_device_event,
                        device_type=detected_type,
                        device_types=detected_types,
                        verbosity=self.verbosity,
                        keyboard_uinput=self._keyboard_uinput,
                        mouse_uinput=self._mouse_uinput,
                        gamepad_uinput=self._gamepad_uinput,
                        broadcast_callback=self.broadcast_callback,
                        recording_manager=self.recording_manager,
                        macro_player=self.play_macro,
                        suppress_rel_getter=lambda: self._mouse_rel_suppressed,
                        mouse_rel_suppression_start_callback=self.begin_mouse_rel_suppression,
                        diagnostics_recorder=self._record_diagnostic,
                        runtime_cleanup_callback=self._clear_combo_runtime_for_binding_scope,
                    )
                    await self._grab_with_retry(device, path)
                    devices.append(device)
                    grabbed_count += 1
                    if self.verbosity >= 1:
                        reason = "mapped buttons" if has_mapped_buttons else "forced for combos"
                        log.debug("  %s - grabbed (%s)", path, reason)
                else:
                    skipped_count += 1
                    if self.verbosity >= 1:
                        log.debug(
                            "  %s - skipped (no matching mapped button names/codes)",
                            path,
                        )
                    if self.verbosity >= 1:
                        log.debug(f"  {path} - skipped (no mapped buttons)")
            except OSError as e:
                if e.errno in {errno.ENOENT, errno.ENODEV}:
                    log.info("Skipping unavailable interface for %s: %s", hardware_id, path)
                    continue
                log.error(f"Failed to grab {path}: {e}")
                for d in devices:
                    if d.path in existing_by_path:
                        continue
                    await d.release()
                if created_global_uinputs:
                    self._destroy_global_uinputs()
                raise
            except Exception as e:
                log.error(f"Failed to grab {path}: {e}")
                for d in devices:
                    if d.path in existing_by_path:
                        continue
                    await d.release()
                if created_global_uinputs:
                    self._destroy_global_uinputs()
                raise

        waiting_for_device = bool(requested_paths and available_count == 0 and not devices)
        if (
            not waiting_for_device
            and hardware_id not in self.grabbed_devices
            and requested_paths
            and (mapped_evdev_names or mapped_codes)
            and grabbed_count == 0
        ):
            if created_global_uinputs:
                self._destroy_global_uinputs()
            raise ValueError(
                f"No interfaces for {hardware_id} matched mapped buttons "
                f"(paths={len(requested_paths)}, mapped_names={len(mapped_evdev_names)}, "
                f"mapped_codes={len(mapped_codes)})"
            )

        if devices:
            self.grabbed_devices[hardware_id] = devices
        else:
            self.grabbed_devices.pop(hardware_id, None)

        log.info(
            "Configured device %s: total_interfaces=%d newly_grabbed=%d skipped=%d",
            hardware_id,
            len(devices),
            grabbed_count,
            skipped_count,
        )
        return {
            "grabbed": True,
            "hardware_id": hardware_id,
            "grabbed_count": len(devices),
            "skipped_count": skipped_count,
            "waiting_for_device": waiting_for_device,
        }

    async def _grab_with_retry(self, device: "GrabbedDevice", path: str) -> None:
        delays = [0.05, 0.10, 0.20, 0.40, 0.80]
        last_error: Exception | None = None
        for attempt, delay in enumerate(delays, start=1):
            try:
                await device.grab()
                return
            except OSError as e:
                last_error = e
                if e.errno != errno.EBUSY:
                    raise
                if attempt >= len(delays):
                    break
                log.warning(
                    "Device %s busy during grab (attempt %d/%d), retrying in %.2fs",
                    path,
                    attempt,
                    len(delays),
                    delay,
                )
                await asyncio.sleep(delay)
            except Exception as e:
                last_error = e
                raise

        if last_error is not None:
            raise last_error

    def _device_has_mapped_buttons(
        self,
        caps: dict[int, Sequence[object]],
        mapped_evdev_names: set[str],
        mapped_codes: set[int] | None = None,
    ) -> bool:
        mapped_code_set = {int(code) for code in (mapped_codes or set())}
        for ev_type, codes in caps.items():
            if ev_type == evdev.ecodes.EV_SYN:
                continue

            for code in codes:
                if isinstance(code, tuple):
                    if not code or not isinstance(code[0], int):
                        continue
                    code_val = code[0]
                elif isinstance(code, int):
                    code_val = code
                else:
                    continue

                if code_val in mapped_code_set:
                    return True

                try:
                    code_name = evdev.ecodes.bytype[ev_type].get(code_val, str(code_val))
                    if isinstance(code_name, (tuple, list)):
                        code_name = code_name[0] if code_name else str(code_val)
                    if code_name.lower() in mapped_evdev_names:
                        return True
                except Exception:
                    pass

        return False

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
        self._cancel_pending_hardware_release(hardware_id)
        self._cancel_pending_interface_releases_for_hardware(hardware_id)
        await self._clear_combo_runtime_for_binding_scope(hardware_id)
        self._desired_grabs.pop(hardware_id, None)
        devices = self.grabbed_devices.pop(hardware_id, [])

        for device in devices:
            await device.release()

        self._destroy_global_uinputs()
        self.active_mappings.pop(hardware_id, None)
        self._desired_paths.pop(hardware_id, None)
        log.info(f"Released device {hardware_id}")
        return {"released": True, "hardware_id": hardware_id}

    def _schedule_hardware_release_unlocked(
        self,
        hardware_id: str,
        grace_s: float | None = None,
    ) -> JsonObject:
        devices = self.grabbed_devices.get(hardware_id, [])
        if not devices:
            self._desired_grabs.pop(hardware_id, None)
            self.active_mappings.pop(hardware_id, None)
            self._desired_paths.pop(hardware_id, None)
            return {"released": True, "hardware_id": hardware_id}

        self.active_mappings[hardware_id] = {}
        self._desired_paths[hardware_id] = set()

        delay = max(0.01, float(self._release_grace_s if grace_s is None else grace_s))
        self._cancel_pending_hardware_release(hardware_id)
        self._pending_hardware_release[hardware_id] = asyncio.create_task(
            self._delayed_hardware_release(hardware_id, delay)
        )
        log.info(
            "Scheduled hardware release for %s in %.1fs",
            hardware_id,
            delay,
        )
        return {
            "released": False,
            "scheduled": True,
            "hardware_id": hardware_id,
            "grace_s": delay,
        }

    async def _delayed_hardware_release(self, hardware_id: str, delay: float) -> None:
        next_delay = float(delay)
        try:
            while True:
                await asyncio.sleep(next_delay)
                async with self._op_lock:
                    task = self._pending_hardware_release.get(hardware_id)
                    if task is not asyncio.current_task():
                        return
                    if self._desired_paths.get(hardware_id):
                        return
                    if self._hardware_has_held_inputs(hardware_id):
                        next_delay = self._held_release_retry_s
                        log.info(
                            "Deferred release for %s: source button still held, retrying in %.1fs",
                            hardware_id,
                            next_delay,
                        )
                        continue
                    await self._release_device_unlocked(hardware_id)
                    return
        except asyncio.CancelledError:
            pass
        finally:
            task = self._pending_hardware_release.get(hardware_id)
            if task is asyncio.current_task():
                self._pending_hardware_release.pop(hardware_id, None)

    def _hardware_has_held_inputs(self, hardware_id: str) -> bool:
        for device in self.grabbed_devices.get(hardware_id, []):
            if device.has_held_source_inputs():
                return True
        return False

    def _cancel_pending_hardware_release(self, hardware_id: str) -> None:
        task = self._pending_hardware_release.pop(hardware_id, None)
        if task and not task.done():
            task.cancel()

    def _cancel_pending_interface_release(self, hardware_id: str, path: str) -> None:
        key = (hardware_id, path)
        task = self._pending_interface_release.pop(key, None)
        if task and not task.done():
            task.cancel()

    def _cancel_pending_interface_releases_for_hardware(self, hardware_id: str) -> None:
        for key in list(self._pending_interface_release.keys()):
            if key[0] != hardware_id:
                continue
            task = self._pending_interface_release.pop(key)
            if not task.done():
                task.cancel()

    def _schedule_interface_release(self, hardware_id: str, path: str) -> None:
        self._cancel_pending_interface_release(hardware_id, path)
        delay = self._release_grace_s
        self._pending_interface_release[(hardware_id, path)] = asyncio.create_task(
            self._delayed_interface_release(hardware_id, path, delay)
        )
        log.info(
            "Scheduled interface release for %s (%s) in %.1fs",
            hardware_id,
            path,
            delay,
        )

    async def _delayed_interface_release(self, hardware_id: str, path: str, delay: float) -> None:
        key = (hardware_id, path)
        try:
            await asyncio.sleep(delay)
            async with self._op_lock:
                task = self._pending_interface_release.get(key)
                if task is not asyncio.current_task():
                    return
                if path in self._desired_paths.get(hardware_id, set()):
                    return
                await self._release_interface_unlocked(hardware_id, path)
        except asyncio.CancelledError:
            pass
        finally:
            task = self._pending_interface_release.get(key)
            if task is asyncio.current_task():
                self._pending_interface_release.pop(key, None)

    async def _release_interface_unlocked(self, hardware_id: str, path: str) -> None:
        devices = self.grabbed_devices.get(hardware_id, [])
        keep: list[GrabbedDevice] = []
        removed: GrabbedDevice | None = None
        for device in devices:
            if removed is None and device.path == path:
                removed = device
                continue
            keep.append(device)

        if removed is None:
            return

        await self._clear_combo_runtime_for_binding_scope(
            hardware_id,
            str(getattr(removed, "interface_id", "") or "").lower(),
        )
        removed.release_tracked_outputs()
        await removed.release()

        if keep:
            self.grabbed_devices[hardware_id] = keep
        else:
            self.grabbed_devices.pop(hardware_id, None)
            if not self._desired_paths.get(hardware_id):
                self.active_mappings.pop(hardware_id, None)
                self._desired_paths.pop(hardware_id, None)
                self._desired_grabs.pop(hardware_id, None)
            self._destroy_global_uinputs()

    async def release_all_devices(self) -> None:
        async with self._op_lock:
            await self.cancel_macro_playback()
            await self._clear_combo_runtime()
            hardware_ids = set(self.grabbed_devices) | set(self._desired_grabs)
            for hardware_id in list(hardware_ids):
                await self._release_device_unlocked(hardware_id)

    async def set_mapping(
        self,
        hardware_id: str,
        mapping: JsonObject,
    ) -> JsonObject:
        async with self._op_lock:
            self._cancel_pending_hardware_release(hardware_id)
            if hardware_id not in self.grabbed_devices:
                raise ValueError(f"Device {hardware_id} not grabbed")

            parsed_mapping: dict[str, MappingAction] = {}
            for button_id, action_data in mapping.items():
                action_dict = _json_object(action_data)
                if isinstance(action_data, str):
                    parsed_mapping[button_id] = self._parse_action(action_data)
                elif action_dict is not None:
                    parsed_mapping[button_id] = self._parse_action(action_dict)

            self.active_mappings[hardware_id] = parsed_mapping
            for device in self.grabbed_devices.get(hardware_id, []):
                await device.reset_mapping_runtime_state()
            log.info(f"Updated mapping for {hardware_id} ({len(parsed_mapping)} buttons)")
            return {"updated": True, "hardware_id": hardware_id}

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
        if isinstance(action_data, str):
            return MappingAction(action_type=ActionType.KEYBOARD, target=action_data)

        action_type_str = _str_value(action_data.get("action"), "passthrough")
        if action_type_str == "hyprland_dispatch":
            action_data = dict(action_data)
            action_data.setdefault("compositor", "hyprland")
            action_type_str = "compositor_dispatch"
        action_type = ActionType(action_type_str)

        superkey_config = None
        if action_type == ActionType.SUPERKEY and "superkey" in action_data:
            superkey_config = self._parse_superkey_config(action_data["superkey"])

        target = action_data.get("target")
        cmd = action_data.get("cmd")
        macro_name = action_data.get("macro_name")
        profile_name = action_data.get("profile_name")
        compositor_id = action_data.get("compositor")
        compositor_dispatcher = action_data.get("dispatcher")
        compositor_args = action_data.get("args")

        return MappingAction(
            action_type=action_type,
            target=_optional_str(target),
            keys=cast(list[str] | None, action_data.get("keys")),
            cmd=_optional_str(cmd),
            exec_ref=_int_or_none(action_data.get("exec_ref")),
            superkey_config=cast(CommonSuperkeyConfig | None, superkey_config),
            macro_name=_optional_str(macro_name),
            macro_events=cast(list[JsonObject] | None, action_data.get("macro_events")),
            macro_replay_mouse_movement=bool(action_data.get("macro_replay_mouse_movement", True)),
            macro_replay_mouse_clicks=bool(action_data.get("macro_replay_mouse_clicks", True)),
            macro_speed=_float_value(action_data.get("macro_speed"), 1.0),
            macro_loop_mode=_str_value(action_data.get("macro_loop_mode"), "none") or "none",
            macro_loop_count=_int_value(action_data.get("macro_loop_count"), 1),
            macro_move_to_start=bool(action_data.get("macro_move_to_start", False)),
            macro_start_x=_int_value(action_data.get("macro_start_x"), 0),
            macro_start_y=_int_value(action_data.get("macro_start_y"), 0),
            macro_block_mouse_movement=bool(action_data.get("macro_block_mouse_movement", False)),
            profile_name=_optional_str(profile_name),
            compositor_id=_optional_str(compositor_id),
            compositor_dispatcher=_optional_str(compositor_dispatcher),
            compositor_args=_optional_str(compositor_args),
            move_x=_int_value(action_data.get("x"), 0),
            move_y=_int_value(action_data.get("y"), 0),
            move_speed=_float_value(action_data.get("speed"), 1.0),
            move_jitter=_float_value(action_data.get("jitter"), 0.3),
            rapidfire_enabled=bool(action_data.get("rapidfire_enabled", False)),
            rapidfire_hold_ms=_int_value(action_data.get("rapidfire_hold_ms"), 20),
            rapidfire_wait_ms=_int_value(action_data.get("rapidfire_wait_ms"), 20),
            tap_enabled=bool(action_data.get("tap_enabled", False)),
            tap_hold_ms=_int_value(action_data.get("tap_hold_ms"), 10),
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
        if not (self._keyboard_uinput or self._mouse_uinput or self._gamepad_uinput):
            return {"status": "error", "message": "No output uinput devices available"}

        normalized_loop = str(loop_mode or "none").lower()
        if normalized_loop not in {"none", "count", "hold", "toggle"}:
            normalized_loop = "none"
        count = max(1, int(loop_count or 1))

        source_key = (str(source_device), str(source_button))

        if int(trigger_value) == 0:
            hold_instances = self._find_matching_macro_instances(
                loop_mode="hold",
                source_key=source_key,
            )
            if hold_instances:
                cancelled = await self._cancel_macro_instances(hold_instances)
                return {"status": "ok", "cancelled": cancelled > 0}
            return {"status": "ok", "cancelled": False}

        if int(trigger_value) != 1:
            return {"status": "ok"}

        if normalized_loop == "toggle":
            toggle_instances = self._find_matching_macro_instances(
                loop_mode="toggle",
                source_key=source_key,
            )
            if toggle_instances:
                cancelled = await self._cancel_macro_instances(toggle_instances)
                return {"status": "ok", "cancelled": cancelled > 0}

        if normalized_loop == "hold":
            hold_instances = self._find_matching_macro_instances(
                loop_mode="hold",
                source_key=source_key,
            )
            if hold_instances:
                return {"status": "ok", "already_running": True}

        self._macro_instance_seq += 1
        instance_id = self._macro_instance_seq
        self._macro_instance_held[instance_id] = set()
        self._macro_cancel_instance_ids.discard(instance_id)
        self._macro_instance_meta[instance_id] = {
            "loop_mode": normalized_loop,
            "source_device": source_key[0],
            "source_button": source_key[1],
            "macro_name": str(macro_name or ""),
        }

        task = asyncio.create_task(
            self._play_macro_task(
                instance_id=instance_id,
                macro_events=macro_events,
                macro_name=macro_name,
                replay_mouse_movement=replay_mouse_movement,
                replay_mouse_clicks=replay_mouse_clicks,
                speed=max(0.01, speed),
                loop_mode=normalized_loop,
                loop_count=count,
                move_to_start=move_to_start,
                start_x=int(start_x),
                start_y=int(start_y),
                block_mouse_movement=block_mouse_movement,
            )
        )
        self._macro_tasks[instance_id] = task
        return {"status": "ok"}

    async def cancel_macro_playback(self) -> JsonObject:
        running_ids = self._running_macro_instance_ids()
        cancelled = await self._cancel_macro_instances(running_ids)
        for devices in self.grabbed_devices.values():
            for device in devices:
                device.release_tracked_outputs()
        self._complete_all_macro_exec_waiters(-1)
        self._macro_mouse_inhibit_count = 0
        self.end_mouse_rel_suppression()
        return {"status": "ok", "cancelled": cancelled > 0}

    def _running_macro_instance_ids(self) -> list[int]:
        return [instance_id for instance_id, task in self._macro_tasks.items() if not task.done()]

    def _find_matching_macro_instances(
        self,
        *,
        loop_mode: str | None = None,
        source_key: tuple[str, str] | None = None,
    ) -> list[int]:
        ids: list[int] = []
        for instance_id, task in self._macro_tasks.items():
            if task.done():
                continue
            meta = self._macro_instance_meta.get(instance_id, {})
            if loop_mode is not None and meta.get("loop_mode") != loop_mode:
                continue
            if source_key is not None and (
                meta.get("source_device") != source_key[0]
                or meta.get("source_button") != source_key[1]
            ):
                continue
            ids.append(instance_id)
        return ids

    async def _cancel_macro_instances(self, instance_ids: list[int]) -> int:
        unique_ids = list(dict.fromkeys(int(i) for i in instance_ids))
        if not unique_ids:
            return 0

        for instance_id in unique_ids:
            self._macro_cancel_instance_ids.add(instance_id)

        for instance_id in unique_ids:
            self._release_macro_held_for_instance(instance_id)

        tasks = [
            task
            for instance_id, task in self._macro_tasks.items()
            if instance_id in unique_ids and not task.done()
        ]
        for task in tasks:
            task.cancel()

        if tasks:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=1.0,
                )

        for instance_id in unique_ids:
            self._macro_cancel_instance_ids.discard(instance_id)
            self._macro_tasks.pop(instance_id, None)
            self._macro_instance_meta.pop(instance_id, None)

        return len(tasks)

    def _complete_all_macro_exec_waiters(self, returncode: int) -> None:
        for wait_id, waiter in list(self._macro_exec_waiters.items()):
            if waiter.done():
                self._macro_exec_waiters.pop(wait_id, None)
                continue
            waiter.set_result(int(returncode))

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
        mouse_btn_codes = frozenset(range(0x110, 0x118))
        pending_abs_moves: dict[str, dict[str, int]] = {}

        if self.verbosity >= 1:
            log.debug("Macro playback started: %s", macro_name or "<unnamed>")

        macro_duration_us = (
            max(_int_value(ev.get("t_us"), 0) for ev in macro_events) if macro_events else 0
        )
        suppression_timeout_s = max(2.0, (macro_duration_us / max(speed, 0.01)) / 1_000_000.0 + 1.0)
        try:
            if block_mouse_movement:
                self._acquire_macro_mouse_inhibit(timeout_s=suppression_timeout_s)

            iterations = 0
            while True:
                if instance_id in self._macro_cancel_instance_ids:
                    break
                iterations += 1
                pending_abs_moves.clear()
                if move_to_start:
                    self._emit_absolute_mouse_move(int(start_x), int(start_y))

                prev_t_us = 0
                for idx, ev in enumerate(macro_events):
                    if instance_id in self._macro_cancel_instance_ids:
                        break
                    if (idx & 127) == 127:
                        await asyncio.sleep(0)

                    t_us = _int_value(ev.get("t_us"), 0)
                    delay_us = max(0, t_us - prev_t_us)
                    prev_t_us = t_us
                    scaled_delay_us = int(delay_us / speed)
                    if scaled_delay_us >= 500:
                        await asyncio.sleep(scaled_delay_us / 1_000_000)

                    action_type = str(ev.get("macro_action", "") or "")
                    if action_type:
                        await self._run_macro_control_action(ev, speed)
                        continue

                    event_type = _int_value(ev.get("type"), 0)
                    event_code = _int_value(ev.get("code"), 0)
                    event_value = _int_value(ev.get("value"), 0)
                    device_type = _str_value(ev.get("device_type"), "other")

                    if (
                        event_type == evdev.ecodes.EV_REL
                        and ev.get("synthetic_move")
                        and ev.get("move_mode") == "abs"
                    ):
                        move_id = _str_value(ev.get("move_id"), "")
                        if move_id:
                            slot = pending_abs_moves.setdefault(move_id, {})
                            if ev.get("move_step") == 1:
                                if event_code == evdev.ecodes.REL_X:
                                    slot["x"] = event_value
                                elif event_code == evdev.ecodes.REL_Y:
                                    slot["y"] = event_value
                                if "x" in slot and "y" in slot:
                                    self._emit_absolute_mouse_move(slot["x"], slot["y"])
                                    pending_abs_moves.pop(move_id, None)
                        continue

                    if event_type == evdev.ecodes.EV_SYN:
                        continue
                    if event_type == evdev.ecodes.EV_REL and not replay_mouse_movement:
                        continue
                    if (
                        event_type == evdev.ecodes.EV_KEY
                        and event_code in mouse_btn_codes
                        and not replay_mouse_clicks
                    ):
                        continue

                    if device_type == "keyboard":
                        uinput = self._keyboard_uinput
                        output_class = "keyboard"
                    elif device_type == "mouse":
                        uinput = self._mouse_uinput
                        output_class = "mouse"
                    elif device_type == "gamepad":
                        uinput = self._gamepad_uinput
                        output_class = "gamepad"
                    else:
                        if event_type == evdev.ecodes.EV_KEY:
                            uinput = self._keyboard_uinput
                            output_class = "keyboard"
                        elif event_type in (evdev.ecodes.EV_REL, evdev.ecodes.EV_ABS):
                            uinput = self._mouse_uinput
                            output_class = "mouse"
                        else:
                            continue

                    if not uinput:
                        continue

                    output = _uinput_writer(uinput)
                    if output is None:
                        continue
                    output.write(event_type, event_code, event_value)
                    output.syn()
                    if event_type == evdev.ecodes.EV_KEY:
                        if event_value == 1:
                            self._track_macro_key_press(instance_id, output_class, event_code)
                        elif event_value == 0:
                            self._track_macro_key_release(instance_id, output_class, event_code)

                if not macro_events and loop_mode in {"hold", "toggle"}:
                    await asyncio.sleep(0.01)
                else:
                    await asyncio.sleep(0)

                if loop_mode == "count":
                    if iterations >= max(1, loop_count):
                        break
                elif loop_mode == "none":
                    break
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning("Macro playback aborted: %s", exc)
        finally:
            self._macro_cancel_instance_ids.discard(instance_id)
            self._release_macro_held_for_instance(instance_id)
            self._macro_tasks.pop(instance_id, None)
            self._macro_instance_meta.pop(instance_id, None)
            if block_mouse_movement:
                self._release_macro_mouse_inhibit()
            if self.verbosity >= 1:
                log.debug("Macro playback finished: %s", macro_name or "<unnamed>")

    def _track_macro_key_press(self, instance_id: int, device_class: str, code: int) -> None:
        key = (device_class, int(code))
        held = self._macro_instance_held.setdefault(instance_id, set())
        if key in held:
            return
        held.add(key)
        self._macro_held_refcount[key] = self._macro_held_refcount.get(key, 0) + 1

    def _track_macro_key_release(self, instance_id: int, device_class: str, code: int) -> None:
        key = (device_class, int(code))
        held = self._macro_instance_held.get(instance_id)
        if not held or key not in held:
            return
        held.remove(key)
        count = self._macro_held_refcount.get(key, 0)
        if count <= 1:
            self._macro_held_refcount.pop(key, None)
        else:
            self._macro_held_refcount[key] = count - 1

    def _release_macro_held_for_instance(self, instance_id: int) -> None:
        held = self._macro_instance_held.pop(instance_id, set())
        if not held:
            return

        uinputs: dict[str, _WritableUInput | None] = {
            "keyboard": _uinput_writer(self._keyboard_uinput),
            "mouse": _uinput_writer(self._mouse_uinput),
            "gamepad": _uinput_writer(self._gamepad_uinput),
        }
        synced: set[str] = set()

        for key in held:
            count = self._macro_held_refcount.get(key, 0)
            if count <= 1:
                self._macro_held_refcount.pop(key, None)
                device_class, code = key
                uinput = uinputs.get(device_class)
                if not uinput:
                    continue
                try:
                    uinput.write(evdev.ecodes.EV_KEY, int(code), 0)
                    synced.add(device_class)
                except Exception:
                    continue
            else:
                self._macro_held_refcount[key] = count - 1

        for device_class in synced:
            uinput = uinputs.get(device_class)
            if not uinput:
                continue
            try:
                uinput.syn()
            except Exception:
                pass

    def _acquire_macro_mouse_inhibit(self, timeout_s: float) -> None:
        self._macro_mouse_inhibit_count += 1
        self.begin_mouse_rel_suppression(timeout_s=max(0.1, timeout_s))

    def _release_macro_mouse_inhibit(self) -> None:
        if self._macro_mouse_inhibit_count > 0:
            self._macro_mouse_inhibit_count -= 1
        if self._macro_mouse_inhibit_count == 0:
            self.end_mouse_rel_suppression()

    def _emit_absolute_mouse_move(self, x: int, y: int) -> None:
        mouse_uinput = _uinput_writer(self._mouse_uinput)
        if mouse_uinput is None:
            return
        try:
            mouse_uinput.write(evdev.ecodes.EV_REL, evdev.ecodes.REL_X, -2147483648)
            mouse_uinput.write(evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, -2147483648)
            mouse_uinput.syn()
            mouse_uinput.write(evdev.ecodes.EV_REL, evdev.ecodes.REL_X, int(x))
            mouse_uinput.write(evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, int(y))
            mouse_uinput.syn()
        except Exception:
            pass

    async def _run_macro_control_action(self, ev: JsonObject, speed: float) -> None:
        action_type = _str_value(ev.get("macro_action"), "")
        if action_type == "wait_fixed":
            duration_ms = max(0, _int_value(ev.get("duration_ms"), 0))
            scaled = duration_ms / max(speed, 0.01)
            if scaled > 0:
                await asyncio.sleep(scaled / 1000.0)
            return

        if action_type == "wait_random":
            min_ms = max(0, _int_value(ev.get("min_ms"), 0))
            max_ms = max(min_ms, _int_value(ev.get("max_ms"), min_ms))
            sampled_ms = random.randint(min_ms, max_ms)
            scaled = sampled_ms / max(speed, 0.01)
            if scaled > 0:
                await asyncio.sleep(scaled / 1000.0)
            return

        if action_type == "exec_async":
            command = _str_value(ev.get("command"), "").strip()
            if not command:
                return
            if self.broadcast_callback:
                await self.broadcast_callback(
                    CommandType.ACTION_TRIGGER,
                    {
                        "action_type": "exec",
                        "cmd": command,
                        "macro_exec_async": True,
                    },
                )
            return

        if action_type == "exec_sync":
            command = _str_value(ev.get("command"), "").strip()
            if not command:
                return

            timeout_ms = max(1, _int_value(ev.get("timeout_ms"), 30000))
            inhibit_mouse = bool(ev.get("inhibit_mouse", False))
            if inhibit_mouse:
                self._acquire_macro_mouse_inhibit(timeout_s=max(1.0, timeout_ms / 1000.0 + 1.0))

            wait_id = uuid.uuid4().hex
            try:
                loop = asyncio.get_running_loop()
                waiter: asyncio.Future[int] = loop.create_future()
                self._macro_exec_waiters[wait_id] = waiter

                if self.broadcast_callback:
                    await self.broadcast_callback(
                        CommandType.ACTION_TRIGGER,
                        {
                            "action_type": "exec",
                            "cmd": command,
                            "macro_exec_wait_id": wait_id,
                        },
                    )
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(waiter, timeout=max(0.1, timeout_ms / 1000.0))
            finally:
                self._macro_exec_waiters.pop(wait_id, None)
                if inhibit_mouse:
                    self._release_macro_mouse_inhibit()

    def complete_macro_exec_wait(self, wait_id: str, returncode: int) -> JsonObject:
        wait_key = str(wait_id or "").strip()
        if not wait_key:
            return {"status": "error", "message": "missing wait_id"}

        waiter = self._macro_exec_waiters.get(wait_key)
        if waiter and not waiter.done():
            waiter.set_result(int(returncode))
            return {"status": "ok", "matched": True}
        return {"status": "ok", "matched": False}

    def begin_mouse_rel_suppression(self, timeout_s: float = 6.0) -> None:
        self._mouse_rel_suppressed = True
        if (
            self._mouse_rel_suppression_watchdog_task
            and not self._mouse_rel_suppression_watchdog_task.done()
        ):
            self._mouse_rel_suppression_watchdog_task.cancel()
        self._mouse_rel_suppression_watchdog_task = asyncio.create_task(
            self._mouse_rel_suppression_watchdog(timeout_s)
        )

    def end_mouse_rel_suppression(self) -> None:
        self._mouse_rel_suppressed = False
        if (
            self._mouse_rel_suppression_watchdog_task
            and not self._mouse_rel_suppression_watchdog_task.done()
        ):
            self._mouse_rel_suppression_watchdog_task.cancel()
        self._mouse_rel_suppression_watchdog_task = None

    async def _mouse_rel_suppression_watchdog(self, timeout_s: float) -> None:
        try:
            await asyncio.sleep(timeout_s)
            self._mouse_rel_suppressed = False
        except asyncio.CancelledError:
            pass

    def _parse_superkey_config(self, data: object) -> SuperkeyConfig:
        config = _json_object(data)
        if config is None:
            raise TypeError("superkey config must be an object")
        return SuperkeyConfig(
            name=_str_value(config.get("name"), ""),
            tap_timeout_ms=_int_value(config.get("tap_timeout_ms"), 200),
            double_tap_window_ms=_int_value(config.get("double_tap_window_ms"), 300),
            hold_threshold_ms=_int_value(config.get("hold_threshold_ms"), 300),
            tap_action=self._parse_superkey_action(config.get("tap_action")),
            double_tap_action=self._parse_superkey_action(config.get("double_tap_action")),
            hold_action=self._parse_superkey_action(config.get("hold_action")),
            tap_hold_action=self._parse_superkey_action(config.get("tap_hold_action")),
        )

    def _parse_superkey_action(self, data: object | None) -> SuperkeyActionData | None:
        if data is None:
            return None
        action = _json_object(data)
        if action is None:
            raise TypeError("superkey action must be an object")

        return SuperkeyActionData(
            action_type=_str_value(action.get("action"), "keyboard"),
            target=_optional_str(action.get("target")),
            cmd=_optional_str(action.get("cmd")),
            exec_ref=_int_or_none(action.get("exec_ref")),
            macro_name=_optional_str(action.get("macro_name")),
            rapidfire_enabled=bool(action.get("rapidfire_enabled", False)),
            rapidfire_hold_ms=_int_value(action.get("rapidfire_hold_ms"), 20),
            rapidfire_wait_ms=_int_value(action.get("rapidfire_wait_ms"), 20),
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
        combo_payload = self._build_combo_event_payload(
            hardware_id,
            evdev_path,
            event_type,
            event_code,
            event_value,
            stable_path=stable_path,
            source=source,
        )
        capture_active = self._queue_combo_capture_event(combo_payload)
        if capture_active:
            return True
        return await self._process_runtime_combo_event(combo_payload)

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
        if event_type != evdev.ecodes.EV_KEY or int(event_value) not in {0, 1, 2}:
            return None

        code_name = evdev.ecodes.bytype.get(event_type, {}).get(event_code, str(event_code))
        if isinstance(code_name, tuple):
            code_name = code_name[0] if code_name else str(event_code)
        evdev_name = code_name.lower()
        if not evdev_name.startswith(("key_", "btn_")):
            return None

        resolved_stable_path = stable_path or resolve_stable_path(evdev_path)
        return {
            "evdev": evdev_name,
            "code": int(event_code),
            "value": int(event_value),
            "source": str(source or get_interface_id(resolved_stable_path) or "").lower(),
            "stable_path": resolved_stable_path,
            "device_path": evdev_path,
            "hardware_id": str(hardware_id).lower(),
        }

    def _queue_combo_capture_event(self, payload: JsonObject | None) -> bool:
        if payload is None or not self._combo_capture_queues:
            return False
        hardware_id = _str_value(payload.get("hardware_id"), "")
        for capture_queue, hardware_ids, notify_event in self._combo_capture_queues.values():
            if hardware_ids and hardware_id not in hardware_ids:
                continue
            capture_queue.put(dict(payload))
            if notify_event is not None:
                notify_event.set()
        return True

    async def _process_runtime_combo_event(
        self, payload: JsonObject | None
    ) -> ComboDecision | None:
        if payload is None or not self.active_combos:
            return None

        raw_value = payload.get("value")
        value = _int_value(raw_value, -1) if raw_value is not None else -1
        if value not in {0, 1, 2}:
            return None

        binding = RuntimeComboBinding(
            hardware_id=_str_value(payload.get("hardware_id"), ""),
            evdev=_str_value(payload.get("evdev"), ""),
            source=_str_value(payload.get("source"), ""),
        )
        if value == 1:
            held_modifiers = self._held_combo_modifier_bindings_for_scope(
                binding.hardware_id,
                binding.source,
            )
            if binding in held_modifiers:
                held_modifiers.discard(binding)
            self._combo_engine.prime_held_bindings(held_modifiers)
        decision = self._combo_engine.handle_event(
            ComboInputEvent(binding=binding, value=value),
            time.monotonic(),
        )
        if decision.recall_events:
            # this can lead to double release event. Should be OK.
            self._emit_combo_recalls(decision.recall_events)
        if decision.action_transition is not None:
            await self._apply_combo_action_transition(decision.action_transition)
        for transition in decision.extra_action_transitions:
            await self._apply_combo_action_transition(transition)
        self._refresh_combo_timeout_watchdog()
        if (
            decision.consume_current_event
            or decision.passthrough_current_event
            or decision.recall_events
            or decision.action_transition is not None
            or decision.extra_action_transitions
            or decision.reset_candidates
        ):
            return decision
        return None

    def _emit_combo_recalls(self, recall_events: list[ComboSyntheticEvent]) -> None:
        for event in recall_events:
            device = self._find_grabbed_device_for_binding(event.binding)
            if device is not None:
                device.emit_combo_release(event.binding.evdev)

    def _find_grabbed_device_for_binding(
        self,
        binding: RuntimeComboBinding,
    ) -> "GrabbedDevice | None":
        for device in self.grabbed_devices.get(binding.hardware_id, []):
            if binding.source and device.interface_id != binding.source:
                continue
            return device
        return None

    def _held_combo_modifier_bindings_for_scope(
        self,
        hardware_id: str,
        source: str,
    ) -> set[RuntimeComboBinding]:
        held: set[RuntimeComboBinding] = set()
        for device in self.grabbed_devices.get(hardware_id, []):
            if source and device.interface_id != source:
                continue
            modifier_getter = getattr(device, "combo_passthrough_held_modifiers", None)
            if not callable(modifier_getter):
                continue
            for evdev_name in cast(Callable[[], set[str]], modifier_getter)():
                held.add(
                    RuntimeComboBinding(
                        hardware_id=hardware_id,
                        evdev=evdev_name,
                        source=device.interface_id,
                    )
                )
        return held

    async def _apply_combo_action_transition(self, transition: ComboActionTransition) -> None:
        if transition.kind == "press":
            await self.start_combo_action(
                transition.combo_id,
                transition.action,
                transition.trigger_binding,
            )
        elif transition.kind == "release":
            await self.stop_combo_action(transition.combo_id)

    async def _broadcast_combo_action(self, data: JsonObject) -> None:
        if self.broadcast_callback is None:
            return
        _fire_and_observe(
            self.broadcast_callback(CommandType.ACTION_TRIGGER, data),
            "combo action broadcast",
        )

    def _emit_combo_mouse_move(self, action: MappingAction) -> None:
        emit_mouse_move(
            self._mouse_uinput,
            int(action.move_x),
            int(action.move_y),
            absolute=action.action_type == ActionType.MOUSE_MOVE_ABS,
        )

    def _prune_combo_action_task(
        self, combo_id: str, task: asyncio.Task[object] | None
    ) -> None:
        if task is None:
            return
        state = self._active_combo_actions.get(combo_id)
        if state is not None and state.get("task") is task:
            self._active_combo_actions.pop(combo_id, None)

    async def _combo_tap_key(
        self,
        combo_id: str,
        uinput_dev: evdev.UInput | None,
        code: int,
        hold_ms: int,
    ) -> None:
        task = cast(asyncio.Task[object] | None, asyncio.current_task())
        pressed = False

        try:
            self._write_combo_key(uinput_dev, code, 1)
            pressed = True
            await asyncio.sleep(max(0.001, float(hold_ms) / 1000.0))
        except asyncio.CancelledError:
            raise
        finally:
            if pressed:
                self._write_combo_key(uinput_dev, code, 0)
            self._prune_combo_action_task(combo_id, task)

    async def _combo_tap_trigger(self, combo_id: str, axis_code: int, hold_ms: int) -> None:
        task = cast(asyncio.Task[object] | None, asyncio.current_task())
        pressed = False

        try:
            self._write_combo_trigger(axis_code, 255)
            pressed = True
            await asyncio.sleep(max(0.001, float(hold_ms) / 1000.0))
        except asyncio.CancelledError:
            raise
        finally:
            if pressed:
                self._write_combo_trigger(axis_code, 0)
            self._prune_combo_action_task(combo_id, task)

    async def start_combo_action(
        self,
        combo_id: str,
        action: MappingAction | None,
        trigger_binding: RuntimeComboBinding,
    ) -> None:
        if action is None:
            return

        await self.stop_combo_action(combo_id)
        trigger_name = f"combo:{combo_id}"

        if action.action_type == ActionType.SUPERKEY:
            log.warning("Superkey combo actions are not supported yet: %s", combo_id)
            return

        if action.action_type == ActionType.KEYBOARD and action.target:
            await self._start_combo_key_action(
                combo_id,
                action,
                self._keyboard_uinput,
            )
            return

        if action.action_type == ActionType.MOUSE and action.target:
            await self._start_combo_key_action(
                combo_id,
                action,
                self._mouse_uinput,
            )
            return

        if action.action_type == ActionType.GAMEPAD and action.target:
            is_trigger, axis_code = self._get_trigger_axis(action.target)
            if is_trigger and axis_code is not None:
                if action.tap_enabled:
                    task = asyncio.create_task(
                        self._combo_tap_trigger(combo_id, axis_code, action.tap_hold_ms)
                    )
                    self._active_combo_actions[combo_id] = {
                        "kind": "tap_trigger",
                        "axis_code": axis_code,
                        "task": task,
                    }
                    return
                if action.rapidfire_enabled:
                    self._active_combo_actions[combo_id] = {
                        "kind": "rapidfire_trigger",
                        "axis_code": axis_code,
                        "active": True,
                    }
                    self._active_combo_actions[combo_id]["task"] = asyncio.create_task(
                        self._combo_rapidfire_trigger(
                            combo_id,
                            axis_code,
                            action.rapidfire_hold_ms,
                            action.rapidfire_wait_ms,
                        )
                    )
                    return
                self._write_combo_trigger(axis_code, 255)
                self._active_combo_actions[combo_id] = {
                    "kind": "trigger",
                    "axis_code": axis_code,
                }
                return
            await self._start_combo_key_action(
                combo_id,
                action,
                self._gamepad_uinput,
            )
            return

        if action.action_type in (ActionType.MOUSE_MOVE_REL, ActionType.MOUSE_MOVE_ABS):
            self._emit_combo_mouse_move(action)
            return

        if action.action_type == ActionType.MACRO:
            if action.macro_events or action.macro_name:
                await self.play_macro(
                    macro_events=action.macro_events or [],
                    macro_name=action.macro_name or "",
                    replay_mouse_movement=action.macro_replay_mouse_movement,
                    replay_mouse_clicks=action.macro_replay_mouse_clicks,
                    speed=action.macro_speed,
                    loop_mode=action.macro_loop_mode,
                    loop_count=action.macro_loop_count,
                    move_to_start=action.macro_move_to_start,
                    start_x=action.macro_start_x,
                    start_y=action.macro_start_y,
                    block_mouse_movement=action.macro_block_mouse_movement,
                    source_device="combo",
                    source_button=trigger_name,
                    trigger_value=1,
                )
                if str(action.macro_loop_mode or "none").lower() == "hold":
                    self._active_combo_actions[combo_id] = {
                        "kind": "macro_hold",
                        "action": action,
                        "source_device": "combo",
                        "source_button": trigger_name,
                    }
            return

        if action.action_type == ActionType.EXEC:
            await self._broadcast_combo_action(
                {
                    "action_type": "exec",
                    "exec_ref": action.exec_ref,
                    "source_device": trigger_binding.hardware_id,
                    "source_button": trigger_name,
                }
            )
            return

        if action.action_type == ActionType.COMPOSITOR_DISPATCH:
            await self._broadcast_combo_action(
                {
                    "action_type": "compositor_dispatch",
                    "compositor": action.compositor_id or "",
                    "dispatcher": action.compositor_dispatcher or "",
                    "args": action.compositor_args or "",
                    "source_device": trigger_binding.hardware_id,
                    "source_button": trigger_name,
                }
            )
            return

        if action.action_type in (
            ActionType.START_MACRO_RECORDING,
            ActionType.STOP_MACRO_RECORDING,
            ActionType.CANCEL_MACRO_PLAYBACK,
        ):
            await self._broadcast_combo_action(
                {
                    "action_type": action.action_type.value,
                    "source_device": trigger_binding.hardware_id,
                    "source_button": trigger_name,
                }
            )
            return

        if action.action_type in (
            ActionType.PROFILE_ENABLE,
            ActionType.PROFILE_DISABLE,
            ActionType.PROFILE_TOGGLE,
        ):
            await self._broadcast_combo_action(
                {
                    "action_type": action.action_type.value,
                    "profile_name": action.profile_name or action.target or "",
                    "source_device": trigger_binding.hardware_id,
                    "source_button": trigger_name,
                }
            )

    async def _start_combo_key_action(
        self,
        combo_id: str,
        action: MappingAction,
        uinput_dev: evdev.UInput | None,
    ) -> None:
        target = str(action.target or "")
        if not target:
            return
        code = self._resolve_code(target)
        if code is None:
            return
        if action.tap_enabled:
            task = asyncio.create_task(
                self._combo_tap_key(combo_id, uinput_dev, code, action.tap_hold_ms)
            )
            self._active_combo_actions[combo_id] = {
                "kind": "tap_key",
                "uinput": uinput_dev,
                "code": code,
                "task": task,
            }
            return
        if action.rapidfire_enabled:
            state: dict[str, object] = {
                "kind": "rapidfire_key",
                "uinput": uinput_dev,
                "code": code,
                "active": True,
            }
            state["task"] = asyncio.create_task(
                self._combo_rapidfire_key(
                    combo_id,
                    uinput_dev,
                    code,
                    action.rapidfire_hold_ms,
                    action.rapidfire_wait_ms,
                )
            )
            self._active_combo_actions[combo_id] = state
            return
        self._write_combo_key(uinput_dev, code, 1)
        self._active_combo_actions[combo_id] = {
            "kind": "key",
            "uinput": uinput_dev,
            "code": code,
        }

    async def stop_combo_action(self, combo_id: str) -> None:
        state = self._active_combo_actions.pop(combo_id, None)
        if not state:
            return
        kind = str(state.get("kind", "") or "")
        if kind == "key":
            uinput_dev = cast(evdev.UInput | None, state.get("uinput"))
            code = state.get("code")
            if isinstance(code, int):
                self._write_combo_key(
                    uinput_dev,
                    code,
                    0,
                )
            return
        if kind == "trigger":
            axis_code = state.get("axis_code")
            if isinstance(axis_code, int):
                self._write_combo_trigger(axis_code, 0)
            return
        if kind in {"tap_key", "tap_trigger", "rapidfire_key", "rapidfire_trigger"}:
            state["active"] = False
            task = state.get("task")
            if isinstance(task, asyncio.Task) and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            return
        if kind == "macro_hold":
            action = state.get("action")
            if isinstance(action, MappingAction):
                await self.play_macro(
                    macro_events=[],
                    macro_name=action.macro_name or "",
                    replay_mouse_movement=action.macro_replay_mouse_movement,
                    replay_mouse_clicks=action.macro_replay_mouse_clicks,
                    speed=action.macro_speed,
                    loop_mode=action.macro_loop_mode,
                    loop_count=action.macro_loop_count,
                    move_to_start=action.macro_move_to_start,
                    start_x=action.macro_start_x,
                    start_y=action.macro_start_y,
                    block_mouse_movement=action.macro_block_mouse_movement,
                    source_device=str(state.get("source_device", "") or ""),
                    source_button=str(state.get("source_button", "") or ""),
                    trigger_value=0,
                )

    async def _clear_combo_runtime(self) -> None:
        self._combo_engine.reset()
        for combo_id in list(self._active_combo_actions):
            await self.stop_combo_action(combo_id)
        if self._combo_timeout_task and not self._combo_timeout_task.done():
            self._combo_timeout_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._combo_timeout_task
        self._combo_timeout_task = None

    async def _clear_combo_runtime_for_binding_scope(
        self,
        hardware_id: str,
        source: str | None = None,
    ) -> None:
        active_combo_ids = self._combo_engine.drop_candidates_for_binding_scope(
            str(hardware_id or "").lower(),
            None if source is None else str(source or "").lower(),
        )
        for combo_id in active_combo_ids:
            await self.stop_combo_action(combo_id)
        self._refresh_combo_timeout_watchdog()

    def _refresh_combo_timeout_watchdog(self) -> None:
        deadline = self._combo_engine.next_deadline()
        if deadline is None:
            if self._combo_timeout_task and not self._combo_timeout_task.done():
                self._combo_timeout_task.cancel()
            self._combo_timeout_task = None
            return
        if self._combo_timeout_task and not self._combo_timeout_task.done():
            self._combo_timeout_task.cancel()
        self._combo_timeout_task = asyncio.create_task(self._combo_timeout_watchdog(deadline))

    async def _combo_timeout_watchdog(self, deadline: float) -> None:
        try:
            await asyncio.sleep(max(0.0, deadline - time.monotonic()))
            self._combo_engine.expire_timeouts(time.monotonic())
        except asyncio.CancelledError:
            raise
        finally:
            if self._combo_timeout_task is asyncio.current_task():
                self._combo_timeout_task = None
            self._refresh_combo_timeout_watchdog()

    async def _combo_rapidfire_key(
        self,
        combo_id: str,
        uinput_dev: evdev.UInput | None,
        code: int,
        hold_ms: int,
        wait_ms: int,
    ) -> None:
        try:
            while self._active_combo_actions.get(combo_id, {}).get("active") is True:
                self._write_combo_key(uinput_dev, code, 1)
                await asyncio.sleep(max(0.001, hold_ms / 1000.0))
                if self._active_combo_actions.get(combo_id, {}).get("active") is not True:
                    break
                self._write_combo_key(uinput_dev, code, 0)
                await asyncio.sleep(max(0.001, wait_ms / 1000.0))
        except asyncio.CancelledError:
            raise
        finally:
            self._write_combo_key(uinput_dev, code, 0)

    async def _combo_rapidfire_trigger(
        self,
        combo_id: str,
        axis_code: int,
        hold_ms: int,
        wait_ms: int,
    ) -> None:
        try:
            while self._active_combo_actions.get(combo_id, {}).get("active") is True:
                self._write_combo_trigger(axis_code, 255)
                await asyncio.sleep(max(0.001, hold_ms / 1000.0))
                if self._active_combo_actions.get(combo_id, {}).get("active") is not True:
                    break
                self._write_combo_trigger(axis_code, 0)
                await asyncio.sleep(max(0.001, wait_ms / 1000.0))
        except asyncio.CancelledError:
            raise
        finally:
            self._write_combo_trigger(axis_code, 0)

    def _write_combo_key(
        self,
        uinput_dev: evdev.UInput | None,
        code: int,
        value: int,
    ) -> None:
        writer = _uinput_writer(uinput_dev)
        if writer is None:
            return
        writer.write(evdev.ecodes.EV_KEY, int(code), int(value))
        writer.syn()

    def _write_combo_trigger(self, axis_code: int, value: int) -> None:
        writer = _uinput_writer(self._gamepad_uinput)
        if writer is None:
            return
        writer.write(evdev.ecodes.EV_ABS, int(axis_code), int(value))
        writer.syn()

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
        self._combo_capture_queues[token] = (
            queue.SimpleQueue[JsonObject](),
            set(hardware_ids),
            notify_event,
        )
        return {
            "token": token,
            "grabbed_devices": sum(len(devices) for devices in self.grabbed_devices.values()),
        }

    def read_combo_capture(self, token: str) -> JsonObject:
        capture_state = self._combo_capture_queues.get(token)
        if capture_state is None:
            return {"event": None}
        capture_queue, _hardware_ids, _notify_event = capture_state
        try:
            return {"event": capture_queue.get_nowait()}
        except queue.Empty:
            return {"event": None}

    def end_combo_capture(self, token: str) -> JsonObject:
        removed = self._combo_capture_queues.pop(token, None)
        return {"status": "ok", "ended": removed is not None}


class GrabbedDevice:
    def __init__(
        self,
        path: str,
        hardware_id: str,
        button_map: dict[str, str],
        mapping_getter: MappingGetter,
        event_callback: DeviceEventCallback,
        device_type: DeviceType = DeviceType.OTHER,
        device_types: list[str] | None = None,
        verbosity: int = 0,
        keyboard_uinput: evdev.UInput | None = None,
        mouse_uinput: evdev.UInput | None = None,
        gamepad_uinput: evdev.UInput | None = None,
        broadcast_callback: BroadcastCallback | None = None,
        recording_manager: RecordingManager | None = None,
        macro_player: MacroPlayer | None = None,
        suppress_rel_getter: Callable[[], bool] | None = None,
        mouse_rel_suppression_start_callback: Callable[[], None] | None = None,
        diagnostics_recorder: Callable[[str, float], None] | None = None,
        runtime_cleanup_callback: Callable[[str, str | None], Awaitable[None]] | None = None,
        button_codes: dict[str, int] | None = None,
    ) -> None:
        self.path = path
        self.hardware_id = hardware_id
        self.stable_path = resolve_stable_path(path)
        self.interface_id = str(get_interface_id(self.stable_path) or "").lower()
        self.button_map: dict[str, str] = {}
        self.evdev_to_button: dict[str, str] = {}
        self.evdev_code_to_button: dict[int, str] = {}
        self.update_button_map(button_map, button_codes)
        self.mapping_getter = mapping_getter
        self.event_callback = event_callback
        self.device_type = device_type
        self.device_types = device_types or [device_type.value]
        self.verbosity = verbosity
        self.device: _ManagedInputDevice | None = None
        self.uinput: evdev.UInput | None = None
        self.keyboard_uinput = keyboard_uinput
        self.mouse_uinput = mouse_uinput
        self.gamepad_uinput = gamepad_uinput
        self.broadcast_callback = broadcast_callback
        self.recording_manager: RecordingManager | None = recording_manager
        self.macro_player = macro_player
        self.suppress_rel_getter = suppress_rel_getter
        self.mouse_rel_suppression_start_callback = mouse_rel_suppression_start_callback
        self.diagnostics_recorder = diagnostics_recorder
        self.runtime_cleanup_callback = runtime_cleanup_callback
        self.task: asyncio.Task[None] | None = None
        self._running = False
        self._rapidfire_active: dict[str, bool] = {}
        self._rapidfire_tasks: dict[str, asyncio.Task[None]] = {}
        self._rapidfire_outputs: dict[str, dict[str, object]] = {}
        self._tap_active: dict[str, bool] = {}
        self._superkey_machines: dict[str, SuperkeyMachine] = {}
        self._combo_passthrough_held: set[str] = set()
        self._held_output_keys: dict[str, set[int]] = {
            "passthrough": set(),
            "keyboard": set(),
            "mouse": set(),
            "gamepad": set(),
        }
        self._superkey_output_refcounts: dict[str, dict[int, int]] = {
            "keyboard": {},
            "mouse": {},
            "gamepad": {},
        }
        self._held_source_actions: dict[str, MappingAction | None] = {}

    def update_button_map(
        self,
        button_map: dict[str, str],
        button_codes: dict[str, int] | None = None,
    ) -> None:
        self.button_map = dict(button_map)
        self.evdev_to_button = {v.lower(): k for k, v in button_map.items()}
        self.evdev_code_to_button = {
            int(code): button_id for button_id, code in (button_codes or {}).items()
        }

    async def reset_mapping_runtime_state(self) -> None:
        for event_name in self._combo_passthrough_held:
            self._held_source_actions.setdefault(event_name, None)
        self._combo_passthrough_held.clear()
        await self.reset_superkeys()
        self._seed_startup_held_actions()

    async def reset_superkeys(self) -> None:
        for machine in self._superkey_machines.values():
            await machine.stop()
        self._superkey_machines.clear()

    async def grab(self) -> None:
        self.device = _device_input(self.path)
        caps = self.device.capabilities()
        caps.pop(evdev.ecodes.EV_SYN, None)

        self.uinput = evdev.UInput(
            events=cast(dict[int, Sequence[int]], caps),
            name=f"keyforge-{self.hardware_id}",
        )

        try:
            await self._wait_for_active_keys_to_clear()
            self.device.grab()
        except Exception:
            if self.uinput:
                try:
                    self.uinput.close()
                except Exception:
                    pass
                self.uinput = None
            raise

        self._running = True
        self.task = asyncio.create_task(self._event_loop())

        log.info(f"Grabbed {self.path} for {self.hardware_id}")

    async def release(self) -> None:
        self._running = False
        self._release_all_keys()
        self._held_source_actions.clear()
        self._combo_passthrough_held.clear()

        await self.reset_superkeys()

        if self.task:
            self.task.cancel()
            try:
                await asyncio.wait_for(self.task, timeout=1.0)
            except (TimeoutError, asyncio.CancelledError):
                pass

        if self.device:
            try:
                self.device.ungrab()
            except Exception as e:
                log.warning(f"Failed to ungrab {self.path}: {e}")
            try:
                self.device.close()
            except Exception as e:
                log.warning(f"Failed to close input device {self.path}: {e}")

        if self.uinput:
            try:
                self.uinput.close()
            except Exception as e:
                log.warning(f"Failed to close passthrough uinput for {self.path}: {e}")

        self.device = None
        self.uinput = None

        log.info(f"Released {self.path}")

    def release_tracked_outputs(self) -> None:
        self._release_all_keys()

    def emit_combo_release(self, evdev_name: str) -> None:
        if not self.uinput:
            return
        code = self._resolve_code(evdev_name)
        if code is None:
            return
        self._write_key(self.uinput, code, 0)

    def has_held_source_inputs(self) -> bool:
        return bool(self._held_source_actions)

    def combo_passthrough_held_modifiers(self) -> set[str]:
        return {
            event_name
            for event_name in self._combo_passthrough_held
            if normalize_combo_evdev(event_name) in COMBO_HELD_REARM_MODIFIERS
        }

    async def _event_loop(self) -> None:
        error_backoff = 0.01

        device = self.device
        if device is None:
            return

        try:
            async for event in device.async_read_loop():
                if not self._running:
                    break
                try:
                    await self._process_event(event)
                    error_backoff = 0.01
                except Exception as e:
                    if self._running:
                        await self._recover_from_event_processing_error()
                        log.warning(
                            "Event processing error on %s: %s (backoff %.3fs)",
                            self.path,
                            e,
                            error_backoff,
                        )
                        await asyncio.sleep(error_backoff)
                        error_backoff = min(0.5, error_backoff * 2)
        except asyncio.CancelledError:
            pass
        except OSError as e:
            if self._running:
                await self._cleanup_runtime_failure()
                log.warning("Device read error on %s: %s", self.path, e)

    async def _cleanup_runtime_failure(self) -> None:
        if self.runtime_cleanup_callback is not None:
            try:
                await self.runtime_cleanup_callback(self.hardware_id, self.interface_id)
            except Exception as e:
                log.warning(
                    "Failed to clear combo runtime after device error on %s: %s",
                    self.path,
                    e,
                )
        try:
            await self.reset_superkeys()
        except Exception as e:
            log.warning("Failed to reset superkeys after event error on %s: %s", self.path, e)
        self._release_all_keys()

    async def _recover_from_event_processing_error(self) -> None:
        await self._cleanup_runtime_failure()

    def _get_event_name(self, event: evdev.InputEvent) -> str:
        try:
            code_name = evdev.ecodes.bytype[event.type].get(event.code, str(event.code))
            if isinstance(code_name, tuple):
                code_name = code_name[0] if code_name else str(event.code)
            return code_name.lower()
        except Exception:
            return str(event.code)

    def _get_key_name(self, code: int) -> str | None:
        try:
            code_name = evdev.ecodes.bytype[evdev.ecodes.EV_KEY].get(code, str(code))
            if isinstance(code_name, tuple):
                code_name = code_name[0] if code_name else str(code)
            return code_name.lower()
        except Exception:
            return None

    async def _broadcast_grab_status(
        self,
        state: str,
        active_names: list[str],
        *,
        waited_s: float,
    ) -> None:
        if self.broadcast_callback is None:
            return
        try:
            await self.broadcast_callback(
                CommandType.DEVICE_GRAB_STATUS,
                {
                    "hardware_id": self.hardware_id,
                    "path": self.path,
                    "state": state,
                    "active_keys": list(active_names),
                    "waited_s": float(waited_s),
                },
            )
        except Exception as e:
            log.warning("Failed to broadcast grab status for %s: %s", self.hardware_id, e)

    async def _wait_for_active_key_activity(self, timeout_s: float) -> bool:
        if self.device is None:
            return False

        loop = asyncio.get_running_loop()
        readable = asyncio.Event()
        fd = self.device.fileno()
        loop.add_reader(fd, readable.set)
        try:
            try:
                await asyncio.wait_for(readable.wait(), timeout_s)
            except TimeoutError:
                return False
        finally:
            loop.remove_reader(fd)

        while True:
            try:
                event = self.device.read_one()
            except BlockingIOError:
                break
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    break
                log.warning(
                    "[%s] failed to drain pending events before grab: %s",
                    self.hardware_id,
                    e,
                )
                break
            except Exception as e:
                log.warning(
                    "[%s] failed to drain pending events before grab: %s",
                    self.hardware_id,
                    e,
                )
                break
            if event is None:
                break

        return True

    async def _wait_for_active_keys_to_clear(self) -> None:
        if self.device is None:
            return

        started_at = time.monotonic()
        warned = False
        last_log_at = 0.0
        while True:
            try:
                active_codes = list(await asyncio.to_thread(self.device.active_keys) or [])
            except Exception as e:
                log.warning(
                    "[%s] failed to read active keys before grab: %s; proceeding with grab",
                    self.hardware_id,
                    e,
                )
                return

            now = time.monotonic()
            if not active_codes:
                if warned:
                    await self._broadcast_grab_status("ready", [], waited_s=now - started_at)
                    log.info("[%s] active keys cleared, proceeding with grab", self.hardware_id)
                return
            active_names = [
                event_name
                for code in active_codes
                if (event_name := self._get_key_name(int(code))) is not None
            ]
            summary = (
                ", ".join(active_names)
                if active_names
                else ", ".join(str(int(code)) for code in active_codes)
            )
            if now - started_at >= ACTIVE_KEY_IDLE_MAX_WAIT_S:
                await self._broadcast_grab_status(
                    "timed_out",
                    active_names,
                    waited_s=now - started_at,
                )
                log.error(
                    "[%s] timed out waiting %.1fs for active keys to clear before grab: %s",
                    self.hardware_id,
                    ACTIVE_KEY_IDLE_MAX_WAIT_S,
                    summary,
                )
                raise TimeoutError(
                    f"timed out waiting {ACTIVE_KEY_IDLE_MAX_WAIT_S:.1f}s "
                    f"for active keys to clear: {summary}"
                )
            if not warned:
                await self._broadcast_grab_status(
                    "waiting",
                    active_names,
                    waited_s=now - started_at,
                )
                log.warning(
                    "[%s] delaying grab until keys are released: %s",
                    self.hardware_id,
                    summary,
                )
                warned = True
                last_log_at = now
            elif now - last_log_at >= ACTIVE_KEY_IDLE_LOG_INTERVAL_S:
                await self._broadcast_grab_status(
                    "waiting",
                    active_names,
                    waited_s=now - started_at,
                )
                log.info(
                    "[%s] still waiting to grab; active keys still down: %s",
                    self.hardware_id,
                    summary,
                )
                last_log_at = now

            next_heartbeat_at = last_log_at + ACTIVE_KEY_IDLE_LOG_INTERVAL_S
            wait_timeout = min(
                ACTIVE_KEY_IDLE_MAX_WAIT_S - (now - started_at),
                max(0.0, next_heartbeat_at - now),
            )
            if wait_timeout <= 0.0:
                continue
            await self._wait_for_active_key_activity(wait_timeout)

    def _seed_startup_held_actions(self) -> None:
        if self.device is None:
            return

        try:
            active_codes = list(self.device.active_keys() or [])
        except Exception:
            return

        mapping = self.mapping_getter()
        for code in active_codes:
            event_name = self._get_key_name(int(code))
            if not event_name or event_name in self._held_source_actions:
                continue
            action = self._find_action_for_code(int(code), event_name, mapping)
            self._held_source_actions[event_name] = action
            self._reconcile_startup_held_action(action)

    def _reconcile_startup_held_action(self, action: MappingAction | None) -> None:
        if action is None or not action.target:
            return

        if action.action_type == ActionType.KEYBOARD:
            code = self._resolve_code(action.target)
            if code is not None:
                self._ensure_key_released(code, self.keyboard_uinput)
            return

        if action.action_type == ActionType.MOUSE:
            code = self._resolve_code(action.target)
            if code is not None:
                self._ensure_key_released(code, self.mouse_uinput)
            return

        if action.action_type == ActionType.GAMEPAD:
            is_trigger, axis_code = self._get_trigger_axis(action.target)
            if is_trigger and axis_code is not None:
                self._ensure_trigger_released(axis_code)
                return
            code = self._resolve_code(action.target)
            if code is not None:
                self._ensure_key_released(code, self.gamepad_uinput)

    async def _process_event(self, event: evdev.InputEvent) -> None:
        started_ns = time.perf_counter_ns()
        diag_label = "unknown"
        combo_consumed = False
        combo_passthrough_requested = False

        event_name = self._get_event_name(event)
        consumed = await self.event_callback(
            self.hardware_id,
            self.path,
            event.type,
            event.code,
            event.value,
            self.stable_path,
            self.interface_id,
        )
        if consumed is True:
            return
        if isinstance(consumed, ComboDecision):
            if consumed.consume_current_event:
                if not (
                    event.type == evdev.ecodes.EV_KEY
                    and int(event.value) == 0
                    and (
                        event_name in self._held_source_actions
                        or event_name in self._combo_passthrough_held
                    )
                ):
                    return
                combo_consumed = True
            if consumed.passthrough_current_event:
                combo_passthrough_requested = True

        if event.type == evdev.ecodes.EV_SYN:
            diag_label = "syn"
            if self.diagnostics_recorder:
                self.diagnostics_recorder(
                    diag_label, (time.perf_counter_ns() - started_ns) / 1000.0
                )
            return

        if event.type not in (evdev.ecodes.EV_KEY, evdev.ecodes.EV_REL):
            self._passthrough(event)
            diag_label = "passthrough_other"
            if self.diagnostics_recorder:
                self.diagnostics_recorder(
                    diag_label, (time.perf_counter_ns() - started_ns) / 1000.0
                )
            return

        if event.type == evdev.ecodes.EV_KEY and event_name in self._combo_passthrough_held:
            self._passthrough(event)
            if int(event.value) == 0:
                self._combo_passthrough_held.discard(event_name)
            diag_label = "combo_passthrough_held"
            if self.diagnostics_recorder:
                self.diagnostics_recorder(
                    diag_label, (time.perf_counter_ns() - started_ns) / 1000.0
                )
            return

        recording_active = bool(self.recording_manager and self.recording_manager.is_recording)
        mapping = self.mapping_getter()
        has_held_source_action = (
            event.type == evdev.ecodes.EV_KEY and event_name in self._held_source_actions
        )
        if not mapping and not recording_active and not has_held_source_action:
            if (
                combo_passthrough_requested
                and event.type == evdev.ecodes.EV_KEY
                and int(event.value) == 1
            ):
                self._combo_passthrough_held.add(event_name)
            self._passthrough(event)
            diag_label = "combo_passthrough" if combo_passthrough_requested else "passthrough_fast"
            if self.diagnostics_recorder:
                self.diagnostics_recorder(
                    diag_label, (time.perf_counter_ns() - started_ns) / 1000.0
                )
            return

        action = self._find_action_for_event(event, mapping)
        if event.type == evdev.ecodes.EV_KEY:
            held_action = self._held_source_actions.get(event_name)
            if int(event.value) == 1 and event_name not in self._held_source_actions:
                self._held_source_actions[event_name] = action
            elif int(event.value) in (0, 2) and event_name in self._held_source_actions:
                action = held_action

        if recording_active:
            if not (
                action
                and action.action_type
                in (
                    ActionType.START_MACRO_RECORDING,
                    ActionType.STOP_MACRO_RECORDING,
                    ActionType.CANCEL_MACRO_PLAYBACK,
                )
            ):
                recording_manager = self.recording_manager
                if recording_manager is None:
                    return
                recording_manager.record_event(
                    classify_event_device_type(event, self.device_types),
                    event,
                )

        if self.verbosity >= 2:
            if event.type == evdev.ecodes.EV_REL and event.code in (
                evdev.ecodes.REL_X,
                evdev.ecodes.REL_Y,
            ):
                pass
            elif action:
                if action.action_type == ActionType.SUPPRESS:
                    log.debug(
                        f"[{self.hardware_id}] {event_name} ({event.code}) "
                        f"-> SUPPRESS value={event.value}"
                    )
                elif action.action_type in (
                    ActionType.KEYBOARD,
                    ActionType.MOUSE,
                    ActionType.GAMEPAD,
                ):
                    target = action.target or "?"
                    mods: list[str] = []
                    if action.rapidfire_enabled:
                        mods.append(f"rf:{action.rapidfire_hold_ms}/{action.rapidfire_wait_ms}")
                    if action.tap_enabled:
                        mods.append(f"tap:{action.tap_hold_ms}")
                    mod_str = f" [{', '.join(mods)}]" if mods else ""
                    log.debug(
                        f"[{self.hardware_id}] {event_name} ({event.code}) -> "
                        f"{action.action_type.value}:{target}{mod_str} value={event.value}"
                    )
                elif action.action_type in (
                    ActionType.MOUSE_MOVE_REL,
                    ActionType.MOUSE_MOVE_ABS,
                ):
                    log.debug(
                        f"[{self.hardware_id}] {event_name} ({event.code}) -> "
                        f"{action.action_type.value} x={int(action.move_x)} "
                        f"y={int(action.move_y)} value={event.value}"
                    )
                elif action.action_type == ActionType.EXEC:
                    log.debug(
                        f"[{self.hardware_id}] {event_name} ({event.code}) -> "
                        f"EXEC {action.cmd or ''} value={event.value}"
                    )
                elif action.action_type == ActionType.SUPERKEY:
                    sk_name = action.superkey_config.name if action.superkey_config else "?"
                    log.debug(
                        f"[{self.hardware_id}] {event_name} ({event.code}) -> "
                        f"SUPERKEY:{sk_name} value={event.value}"
                    )
            else:
                log.debug(
                    f"[{self.hardware_id}] {event_name} ({event.code}) "
                    f"-> PASSTHROUGH value={event.value}"
                )

        if action:
            await self._execute_action(action, event, event_name)
            diag_label = (
                f"combo_release_action_{action.action_type.value}"
                if combo_consumed
                else f"action_{action.action_type.value}"
            )
        else:
            if (
                combo_passthrough_requested
                and event.type == evdev.ecodes.EV_KEY
                and int(event.value) == 1
            ):
                self._combo_passthrough_held.add(event_name)
            self._passthrough(event)
            diag_label = (
                "combo_passthrough" if combo_passthrough_requested else "passthrough_mapped"
            )

        if event.type == evdev.ecodes.EV_KEY and int(event.value) == 0:
            self._held_source_actions.pop(event_name, None)

        if self.diagnostics_recorder:
            self.diagnostics_recorder(diag_label, (time.perf_counter_ns() - started_ns) / 1000.0)

    def _find_action_for_event(
        self,
        event: evdev.InputEvent,
        mapping: dict[str, MappingAction],
    ) -> MappingAction | None:
        event_name = self._get_event_name(event)
        return self._find_action_for_code(int(event.code), event_name, mapping)

    def _find_action_for_code(
        self,
        event_code: int,
        event_name: str,
        mapping: dict[str, MappingAction],
    ) -> MappingAction | None:
        button_id = self.evdev_code_to_button.get(int(event_code))
        if button_id and button_id in mapping:
            return mapping[button_id]
        return self._find_action_for_name(event_name, mapping)

    def _find_action_for_name(
        self,
        event_name: str,
        mapping: dict[str, MappingAction],
    ) -> MappingAction | None:
        button_id = self.evdev_to_button.get(event_name.lower())
        if not button_id:
            canonical_name = canonical_gamepad_button_name(event_name)
            if canonical_name != event_name.lower():
                button_id = self.evdev_to_button.get(canonical_name)

        if button_id and button_id in mapping:
            return mapping[button_id]

        return None

    async def _execute_action(
        self,
        action: MappingAction,
        event: evdev.InputEvent,
        event_name: str,
    ) -> None:
        if action.action_type == ActionType.PASSTHROUGH:
            self._passthrough(event)

        elif action.action_type == ActionType.SUPPRESS:
            pass

        elif action.action_type == ActionType.KEYBOARD:
            if action.target:
                code = self._resolve_code(action.target)
                if code:
                    if action.rapidfire_enabled:
                        if event.value == 1:
                            self._start_rapidfire_task(
                                event_name,
                                "key",
                                lambda: asyncio.create_task(
                                    self._rapidfire_key(
                                        code,
                                        action.rapidfire_hold_ms,
                                        action.rapidfire_wait_ms,
                                        event_name,
                                        self.keyboard_uinput,
                                    )
                                ),
                                code=code,
                                uinput=self.keyboard_uinput,
                            )
                        elif event.value == 0:
                            await self._stop_rapidfire_async(event_name)
                    elif action.tap_enabled:
                        if event.value == 1 and not self._tap_active.get(event_name, False):
                            self._tap_active[event_name] = True
                            _fire_and_observe(
                                self._tap_key(
                                    code, action.tap_hold_ms, event_name, self.keyboard_uinput
                                ),
                                f"tap action {event_name}",
                            )
                    else:
                        self._write_key(self.keyboard_uinput, code, int(event.value))

        elif action.action_type == ActionType.MOUSE:
            if action.target:
                code = self._resolve_code(action.target)
                if code:
                    if action.rapidfire_enabled:
                        if event.value == 1:
                            self._start_rapidfire_task(
                                event_name,
                                "key",
                                lambda: asyncio.create_task(
                                    self._rapidfire_key(
                                        code,
                                        action.rapidfire_hold_ms,
                                        action.rapidfire_wait_ms,
                                        event_name,
                                        self.mouse_uinput,
                                    )
                                ),
                                code=code,
                                uinput=self.mouse_uinput,
                            )
                        elif event.value == 0:
                            await self._stop_rapidfire_async(event_name)
                    elif action.tap_enabled:
                        if event.value == 1 and not self._tap_active.get(event_name, False):
                            self._tap_active[event_name] = True
                            _fire_and_observe(
                                self._tap_key(
                                    code, action.tap_hold_ms, event_name, self.mouse_uinput
                                ),
                                f"tap action {event_name}",
                            )
                    else:
                        self._write_key(self.mouse_uinput, code, int(event.value))

        elif action.action_type == ActionType.GAMEPAD:
            if action.target:
                is_trigger, axis_code = self._get_trigger_axis(action.target)
                if is_trigger:
                    if axis_code is None:
                        return
                    if action.rapidfire_enabled:
                        if event.value == 1:
                            self._start_rapidfire_task(
                                event_name,
                                "trigger",
                                lambda: asyncio.create_task(
                                    self._rapidfire_trigger(
                                        axis_code,
                                        action.rapidfire_hold_ms,
                                        action.rapidfire_wait_ms,
                                        event_name,
                                    )
                                ),
                                axis_code=axis_code,
                            )
                        elif event.value == 0:
                            await self._stop_rapidfire_async(event_name)
                    elif action.tap_enabled:
                        if event.value == 1 and not self._tap_active.get(event_name, False):
                            self._tap_active[event_name] = True
                            _fire_and_observe(
                                self._tap_trigger(axis_code, action.tap_hold_ms, event_name),
                                f"tap action {event_name}",
                            )
                    else:
                        gamepad_uinput = _uinput_writer(self.gamepad_uinput)
                        if gamepad_uinput is None:
                            return
                        gamepad_uinput.write(
                            evdev.ecodes.EV_ABS,
                            axis_code,
                            255 if event.value else 0,
                        )
                        gamepad_uinput.syn()
                else:
                    code = self._resolve_code(action.target)
                    if code:
                        if action.rapidfire_enabled:
                            if event.value == 1:
                                self._start_rapidfire_task(
                                    event_name,
                                    "key",
                                    lambda: asyncio.create_task(
                                        self._rapidfire_key(
                                            code,
                                            action.rapidfire_hold_ms,
                                            action.rapidfire_wait_ms,
                                            event_name,
                                            self.gamepad_uinput,
                                        )
                                    ),
                                    code=code,
                                    uinput=self.gamepad_uinput,
                                )
                            elif event.value == 0:
                                await self._stop_rapidfire_async(event_name)
                        elif action.tap_enabled:
                            if event.value == 1 and not self._tap_active.get(event_name, False):
                                self._tap_active[event_name] = True
                                _fire_and_observe(
                                    self._tap_key(
                                        code, action.tap_hold_ms, event_name, self.gamepad_uinput
                                    ),
                                    f"tap action {event_name}",
                                )
                        else:
                            self._write_key(self.gamepad_uinput, code, int(event.value))

        elif action.action_type == ActionType.EXEC:
            if event.value == 1 and action.exec_ref is not None and self.broadcast_callback:
                _fire_and_observe(
                    self.broadcast_callback(
                        CommandType.ACTION_TRIGGER,
                        {
                            "action_type": "exec",
                            "exec_ref": action.exec_ref,
                            "source_device": self.hardware_id,
                            "source_button": event_name,
                        },
                    ),
                    f"exec action {event_name}",
                )

        elif action.action_type == ActionType.COMPOSITOR_DISPATCH:
            if event.value == 1 and self.broadcast_callback:
                _fire_and_observe(
                    self.broadcast_callback(
                        CommandType.ACTION_TRIGGER,
                        {
                            "action_type": "compositor_dispatch",
                            "compositor": action.compositor_id or "",
                            "dispatcher": action.compositor_dispatcher or "",
                            "args": action.compositor_args or "",
                            "source_device": self.hardware_id,
                            "source_button": event_name,
                        },
                    ),
                    f"compositor action {event_name}",
                )

        elif action.action_type == ActionType.START_MACRO_RECORDING:
            if event.value == 1 and self.broadcast_callback:
                _fire_and_observe(
                    self.broadcast_callback(
                        CommandType.ACTION_TRIGGER,
                        {
                            "action_type": "start_macro_recording",
                            "source_device": self.hardware_id,
                            "source_button": event_name,
                        },
                    ),
                    f"start recording action {event_name}",
                )

        elif action.action_type == ActionType.STOP_MACRO_RECORDING:
            if event.value == 1 and self.broadcast_callback:
                _fire_and_observe(
                    self.broadcast_callback(
                        CommandType.ACTION_TRIGGER,
                        {
                            "action_type": "stop_macro_recording",
                            "source_device": self.hardware_id,
                            "source_button": event_name,
                        },
                    ),
                    f"stop recording action {event_name}",
                )

        elif action.action_type == ActionType.CANCEL_MACRO_PLAYBACK:
            if event.value == 1 and self.broadcast_callback:
                _fire_and_observe(
                    self.broadcast_callback(
                        CommandType.ACTION_TRIGGER,
                        {
                            "action_type": "cancel_macro_playback",
                            "source_device": self.hardware_id,
                            "source_button": event_name,
                        },
                    ),
                    f"cancel macro action {event_name}",
                )

        elif action.action_type in (
            ActionType.PROFILE_ENABLE,
            ActionType.PROFILE_DISABLE,
            ActionType.PROFILE_TOGGLE,
        ):
            if event.value == 1 and self.broadcast_callback:
                _fire_and_observe(
                    self.broadcast_callback(
                        CommandType.ACTION_TRIGGER,
                        {
                            "action_type": action.action_type.value,
                            "profile_name": action.profile_name or action.target or "",
                            "source_device": self.hardware_id,
                            "source_button": event_name,
                        },
                    ),
                    f"profile action {event_name}",
                )

        elif action.action_type == ActionType.MACRO:
            if (
                event.value in (0, 1)
                and (action.macro_events or action.macro_name)
                and self.macro_player
            ):
                _fire_and_observe(
                    self.macro_player(
                        macro_events=action.macro_events or [],
                        macro_name=action.macro_name or "",
                        replay_mouse_movement=action.macro_replay_mouse_movement,
                        replay_mouse_clicks=action.macro_replay_mouse_clicks,
                        speed=action.macro_speed,
                        loop_mode=action.macro_loop_mode,
                        loop_count=action.macro_loop_count,
                        move_to_start=action.macro_move_to_start,
                        start_x=action.macro_start_x,
                        start_y=action.macro_start_y,
                        block_mouse_movement=action.macro_block_mouse_movement,
                        source_device=self.hardware_id,
                        source_button=event_name,
                        trigger_value=int(event.value),
                    ),
                    f"macro action {event_name}",
                )

        elif action.action_type == ActionType.MOUSE_MOVE_REL:
            if action.rapidfire_enabled:
                if event.value == 1:
                    self._start_rapidfire_task(
                        event_name,
                        "move",
                        lambda: asyncio.create_task(
                            self._rapidfire_move(
                                action,
                                event_name,
                                action.rapidfire_hold_ms,
                                action.rapidfire_wait_ms,
                            )
                        ),
                    )
                elif event.value == 0:
                    await self._stop_rapidfire_async(event_name)
            elif action.tap_enabled:
                if event.value == 1 and not self._tap_active.get(event_name, False):
                    self._tap_active[event_name] = True
                    _fire_and_observe(
                        self._tap_move(action, event_name, action.tap_hold_ms),
                        f"tap move {event_name}",
                    )
            elif event.value == 1:
                self._emit_mouse_move(action)

        elif action.action_type == ActionType.MOUSE_MOVE_ABS:
            if action.rapidfire_enabled:
                if event.value == 1:
                    self._start_rapidfire_task(
                        event_name,
                        "move",
                        lambda: asyncio.create_task(
                            self._rapidfire_move(
                                action,
                                event_name,
                                action.rapidfire_hold_ms,
                                action.rapidfire_wait_ms,
                            )
                        ),
                    )
                elif event.value == 0:
                    await self._stop_rapidfire_async(event_name)
            elif action.tap_enabled:
                if event.value == 1 and not self._tap_active.get(event_name, False):
                    self._tap_active[event_name] = True
                    _fire_and_observe(
                        self._tap_move(action, event_name, action.tap_hold_ms),
                        f"tap move {event_name}",
                    )
            elif event.value == 1:
                self._emit_mouse_move(action)

        elif action.action_type == ActionType.SUPERKEY:
            if action.superkey_config:
                machine = self._superkey_machines.get(event_name)
                if event.value == 1 and not machine:

                    async def superkey_broadcast(data: JsonObject) -> None:
                        if self.broadcast_callback:
                            _fire_and_observe(
                                self.broadcast_callback(CommandType.ACTION_TRIGGER, data),
                                f"superkey action {event_name}",
                            )

                    machine = SuperkeyMachine(
                        config=cast(SuperkeyConfig, action.superkey_config),
                        event_name=event_name,
                        keyboard_uinput=self.keyboard_uinput,
                        mouse_uinput=self.mouse_uinput,
                        gamepad_uinput=self.gamepad_uinput,
                        broadcast_callback=superkey_broadcast,
                        key_event_tracker=self._track_superkey_output,
                    )
                    self._superkey_machines[event_name] = machine

                if event.value == 1 and machine is not None:
                    await machine.on_down()
                elif event.value == 0 and machine is not None:
                    await machine.on_up()

    def _start_rapidfire_task(
        self,
        event_name: str,
        kind: str,
        task_factory: RapidfireTaskFactory,
        *,
        code: int | None = None,
        uinput: evdev.UInput | None = None,
        axis_code: int | None = None,
    ) -> None:
        self._stop_rapidfire(event_name)
        task = task_factory()
        self._rapidfire_active[event_name] = True
        self._rapidfire_tasks[event_name] = task
        state: dict[str, object] = {"kind": kind}
        if code is not None:
            state["code"] = int(code)
        if uinput is not None:
            state["uinput"] = uinput
        if axis_code is not None:
            state["axis_code"] = int(axis_code)
        self._rapidfire_outputs[event_name] = state

    def _stop_rapidfire(self, event_name: str) -> None:
        self._rapidfire_active[event_name] = False
        task = self._rapidfire_tasks.pop(event_name, None)
        if task is not None and not task.done():
            task.cancel()
        state = self._rapidfire_outputs.pop(event_name, None)
        if not state:
            return
        kind = str(state.get("kind", "") or "")
        if kind == "trigger":
            axis_code = state.get("axis_code")
            if axis_code is not None:
                self._ensure_trigger_released(cast(int, axis_code))
            return
        if kind == "key":
            code = state.get("code")
            uinput = cast(evdev.UInput | None, state.get("uinput"))
            if code is not None:
                self._ensure_key_released(cast(int, code), uinput)
            return

    async def _stop_rapidfire_async(self, event_name: str) -> None:
        task = self._rapidfire_tasks.get(event_name)
        self._stop_rapidfire(event_name)
        if task is not None and not task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _finish_rapidfire_task(self, event_name: str, task: asyncio.Task[None]) -> None:
        if self._rapidfire_tasks.get(event_name) is not task:
            return
        self._rapidfire_tasks.pop(event_name, None)
        self._rapidfire_active.pop(event_name, None)
        state = self._rapidfire_outputs.pop(event_name, None)
        if not state:
            return
        kind = str(state.get("kind", "") or "")
        if kind == "trigger":
            axis_code = state.get("axis_code")
            if axis_code is not None:
                self._ensure_trigger_released(cast(int, axis_code))
            return
        if kind == "key":
            code = state.get("code")
            uinput = cast(evdev.UInput | None, state.get("uinput"))
            if code is not None:
                self._ensure_key_released(cast(int, code), uinput)

    def _bucket_for_uinput(self, uinput_dev: evdev.UInput | None) -> str | None:
        if uinput_dev is None:
            return None
        if self.uinput is not None and uinput_dev is self.uinput:
            return "passthrough"
        if self.keyboard_uinput is not None and uinput_dev is self.keyboard_uinput:
            return "keyboard"
        if self.mouse_uinput is not None and uinput_dev is self.mouse_uinput:
            return "mouse"
        if self.gamepad_uinput is not None and uinput_dev is self.gamepad_uinput:
            return "gamepad"
        return None

    def _track_key_state(self, uinput_dev: evdev.UInput | None, code: int, value: int) -> None:
        bucket = self._bucket_for_uinput(uinput_dev)
        if not bucket:
            return
        held = self._held_output_keys[bucket]
        if int(value) == 1:
            held.add(int(code))
        elif int(value) == 0:
            held.discard(int(code))

    def _write_key(self, uinput_dev: evdev.UInput | None, code: int, value: int) -> None:
        writer = _uinput_writer(uinput_dev)
        if writer is None:
            return
        writer.write(evdev.ecodes.EV_KEY, int(code), int(value))
        writer.syn()
        self._track_key_state(uinput_dev, int(code), int(value))

    def _track_superkey_output(self, action_type: str, code: int, value: int) -> bool:
        bucket = action_type if action_type in self._superkey_output_refcounts else None
        if bucket is None:
            return True

        refcounts = self._superkey_output_refcounts[bucket]
        current = refcounts.get(int(code), 0)

        if int(value) == 1:
            refcounts[int(code)] = current + 1
            self._held_output_keys[bucket].add(int(code))
            return current == 0

        if int(value) == 0:
            if current <= 1:
                refcounts.pop(int(code), None)
                self._held_output_keys[bucket].discard(int(code))
                return current == 1

            refcounts[int(code)] = current - 1
            return False

        return True

    def _passthrough(self, event: evdev.InputEvent) -> None:
        if (
            self.suppress_rel_getter
            and event.type == evdev.ecodes.EV_REL
            and event.code in (evdev.ecodes.REL_X, evdev.ecodes.REL_Y)
            and self.suppress_rel_getter()
        ):
            return
        if event.type == evdev.ecodes.EV_KEY:
            self._write_key(self.uinput, int(event.code), int(event.value))
            return
        uinput = self.uinput
        writer = _uinput_writer(uinput)
        if writer is None:
            return
        writer.write(event.type, event.code, event.value)
        writer.syn()

    def _resolve_code(self, key_name: str) -> int | None:
        return resolve_output_code(key_name)

    def _get_trigger_axis(self, target: str) -> tuple[bool, int | None]:
        return get_trigger_axis(target)

    async def _rapidfire_trigger(
        self, axis_code: int, hold_ms: int, wait_ms: int, event_name: str
    ) -> None:
        hold = hold_ms / 1000.0
        wait = wait_ms / 1000.0
        task = cast(asyncio.Task[None] | None, asyncio.current_task())
        pressed = False

        try:
            while self._rapidfire_active.get(event_name, False) and self._running:
                gamepad_uinput = _uinput_writer(self.gamepad_uinput)
                if gamepad_uinput is None:
                    return
                gamepad_uinput.write(evdev.ecodes.EV_ABS, axis_code, 255)
                gamepad_uinput.syn()
                pressed = True
                await asyncio.sleep(hold)

                if pressed:
                    gamepad_uinput.write(evdev.ecodes.EV_ABS, axis_code, 0)
                    gamepad_uinput.syn()
                    pressed = False
                if not self._rapidfire_active.get(event_name, False):
                    break

                await asyncio.sleep(wait)
        except Exception:
            pass
        finally:
            if pressed:
                self._ensure_trigger_released(axis_code)
            if task is not None:
                self._finish_rapidfire_task(event_name, task)

    def _ensure_trigger_released(self, axis_code: int) -> None:
        try:
            gamepad_uinput = _uinput_writer(self.gamepad_uinput)
            if gamepad_uinput is not None:
                gamepad_uinput.write(evdev.ecodes.EV_ABS, axis_code, 0)
                gamepad_uinput.syn()
        except Exception:
            pass

    async def _tap_trigger(self, axis_code: int, hold_ms: int, event_name: str) -> None:
        hold = hold_ms / 1000.0

        try:
            gamepad_uinput = _uinput_writer(self.gamepad_uinput)
            if gamepad_uinput is None:
                return
            gamepad_uinput.write(evdev.ecodes.EV_ABS, axis_code, 255)
            gamepad_uinput.syn()
            await asyncio.sleep(hold)
            gamepad_uinput.write(evdev.ecodes.EV_ABS, axis_code, 0)
            gamepad_uinput.syn()
        except Exception:
            pass
        finally:
            self._tap_active.pop(event_name, None)

    async def _rapidfire_key(
        self, code: int, hold_ms: int, wait_ms: int, event_name: str, uinput_dev: evdev.UInput
    ) -> None:
        hold = hold_ms / 1000.0
        wait = wait_ms / 1000.0
        task = cast(asyncio.Task[None] | None, asyncio.current_task())
        pressed = False

        try:
            while self._rapidfire_active.get(event_name, False) and self._running:
                self._write_key(uinput_dev, code, 1)
                pressed = True
                await asyncio.sleep(hold)

                if pressed:
                    self._write_key(uinput_dev, code, 0)
                    pressed = False
                if not self._rapidfire_active.get(event_name, False):
                    break

                await asyncio.sleep(wait)
        except Exception:
            pass
        finally:
            if pressed:
                self._ensure_key_released(code, uinput_dev)
            if task is not None:
                self._finish_rapidfire_task(event_name, task)

    def _ensure_key_released(self, code: int, uinput_dev: evdev.UInput | None) -> None:
        try:
            if uinput_dev:
                self._write_key(uinput_dev, code, 0)
        except Exception:
            pass

    def _release_all_keys(self) -> None:
        devices = {
            "passthrough": self.uinput,
            "keyboard": self.keyboard_uinput,
            "mouse": self.mouse_uinput,
            "gamepad": self.gamepad_uinput,
        }
        for bucket, uinput_dev in devices.items():
            writer = _uinput_writer(uinput_dev)
            if writer is None:
                continue
            held = sorted(self._held_output_keys.get(bucket, set()))
            if not held:
                continue
            try:
                for code in held:
                    writer.write(evdev.ecodes.EV_KEY, int(code), 0)
                writer.syn()
            except Exception:
                pass
            finally:
                self._held_output_keys[bucket].clear()
                if bucket in self._superkey_output_refcounts:
                    self._superkey_output_refcounts[bucket].clear()

        gamepad_uinput = _uinput_writer(self.gamepad_uinput)
        if gamepad_uinput is not None:
            try:
                gamepad_uinput.write(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_Z, 0)
                gamepad_uinput.write(evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RZ, 0)
                gamepad_uinput.syn()
            except Exception:
                pass

        for task in list(self._rapidfire_tasks.values()):
            if not task.done():
                task.cancel()
        self._rapidfire_tasks.clear()
        self._rapidfire_outputs.clear()
        self._rapidfire_active.clear()
        self._tap_active.clear()
        self._combo_passthrough_held.clear()
        self._held_source_actions.clear()

    async def _tap_key(
        self, code: int, hold_ms: int, event_name: str, uinput_dev: evdev.UInput
    ) -> None:
        hold = hold_ms / 1000.0

        try:
            self._write_key(uinput_dev, code, 1)
            await asyncio.sleep(hold)
            self._write_key(uinput_dev, code, 0)
        except Exception:
            pass
        finally:
            self._tap_active.pop(event_name, None)

    def _emit_mouse_move(self, action: MappingAction) -> None:
        emit_mouse_move(
            self.mouse_uinput,
            int(action.move_x),
            int(action.move_y),
            absolute=action.action_type == ActionType.MOUSE_MOVE_ABS,
        )

    async def _rapidfire_move(
        self,
        action: MappingAction,
        event_name: str,
        hold_ms: int,
        wait_ms: int,
    ) -> None:
        hold = hold_ms / 1000.0
        wait = wait_ms / 1000.0
        task = cast(asyncio.Task[None] | None, asyncio.current_task())

        try:
            while self._rapidfire_active.get(event_name, False) and self._running:
                self._emit_mouse_move(action)
                await asyncio.sleep(hold)

                if not self._rapidfire_active.get(event_name, False):
                    break

                await asyncio.sleep(wait)
        except Exception:
            pass
        finally:
            if task is not None:
                self._finish_rapidfire_task(event_name, task)

    async def _tap_move(self, action: MappingAction, event_name: str, hold_ms: int) -> None:
        hold = hold_ms / 1000.0

        try:
            self._emit_mouse_move(action)
            await asyncio.sleep(hold)
        except Exception:
            pass
        finally:
            self._tap_active.pop(event_name, None)
