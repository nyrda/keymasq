import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from typing import Final, TypeVar, cast

import evdev

from keyforge.common.combos import normalize_combo_evdev
from keyforge.common.devices import (
    get_interface_id,
    normalize_evdev_binding_value,
    resolve_evdev_event_type,
    resolve_stable_path,
)
from keyforge.common.models import DeviceType
from keyforge.keyforged.output_helpers import resolve_output_code
from keyforge.keyforged.recording import RecordingManager
from keyforge.keyforged.runtime import grabbed_device_events as runtime_events
from keyforge.keyforged.runtime import grabbed_device_grab as runtime_grab
from keyforge.keyforged.runtime import grabbed_device_outputs as runtime_outputs
from keyforge.keyforged.runtime.grabbed_device_types import (
    AsyncioEvent as _AsyncioEvent,
)
from keyforge.keyforged.runtime.grabbed_device_types import (
    AsyncioLoop as _AsyncioLoop,
)
from keyforge.keyforged.runtime.grabbed_device_types import (
    AsyncioModule as _AsyncioModule,
)
from keyforge.keyforged.runtime.grabbed_device_types import (
    BroadcastCallback,
    DeviceEventCallback,
    GrabbedDeviceState,
    MacroPlayer,
    MappingGetter,
)
from keyforge.keyforged.runtime.grabbed_device_types import (
    ManagedInputDevice as _ManagedInputDevice,
)
from keyforge.keyforged.runtime.grabbed_device_types import (
    WritableUInput as _WritableUInput,
)
from keyforge.keyforged.runtime.outputs import uinput_identity

log = logging.getLogger("keyforged.devices")
ACTIVE_KEY_IDLE_LOG_INTERVAL_S = 1.0
ACTIVE_KEY_IDLE_MAX_WAIT_S = 300.0
COMBO_HELD_REARM_MODIFIERS = frozenset({"shift", "ctrl", "alt", "meta"})

_T = TypeVar("_T")

__all__ = [
    "ASYNCIO_RUNTIME",
    "ACTIVE_KEY_IDLE_LOG_INTERVAL_S",
    "ACTIVE_KEY_IDLE_MAX_WAIT_S",
    "COMBO_HELD_REARM_MODIFIERS",
    "GrabbedDevice",
    "GrabbedDeviceState",
    "get_interface_id",
    "resolve_stable_path",
]


class _AsyncioRuntimeAdapter:
    def get_running_loop(self) -> _AsyncioLoop:
        return asyncio.get_running_loop()

    def create_event(self) -> _AsyncioEvent:
        return asyncio.Event()

    def wait_for(self, aw: Awaitable[_T], timeout: float) -> Awaitable[_T]:
        return asyncio.wait_for(aw, timeout)

    async def sleep(self, delay: float, /) -> None:
        await asyncio.sleep(delay)

    def current_task(self) -> asyncio.Task[object] | None:
        return cast(asyncio.Task[object] | None, asyncio.current_task())

    def create_task(self, coro: Coroutine[object, object, _T], /) -> asyncio.Task[_T]:
        return asyncio.create_task(coro)

    def to_thread(
        self,
        func: Callable[..., _T],
        /,
        *args: object,
        **kwargs: object,
    ) -> Awaitable[_T]:
        return asyncio.to_thread(func, *args, **kwargs)


ASYNCIO_RUNTIME: Final[_AsyncioModule] = _AsyncioRuntimeAdapter()


def _device_input(path: str) -> _ManagedInputDevice:
    return cast(_ManagedInputDevice, evdev.InputDevice(path))


def _uinput_writer(device: object | None) -> _WritableUInput | None:
    return cast(_WritableUInput | None, device)


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
        button_values: dict[str, int] | None = None,
    ) -> None:
        self.path = path
        self.hardware_id = hardware_id
        self.stable_path = resolve_stable_path(path)
        self.interface_id = str(get_interface_id(self.stable_path) or "").lower()
        self.button_map: dict[str, str] = {}
        self.evdev_to_button: dict[str, str] = {}
        self.event_binding_to_button: dict[tuple[int, int, int | None], str] = {}
        self.event_code_to_button: dict[tuple[int, int], str] = {}
        self.update_button_map(button_map, button_codes, button_values)
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
        self.state = GrabbedDeviceState()

    def update_button_map(
        self,
        button_map: dict[str, str],
        button_codes: dict[str, int] | None = None,
        button_values: dict[str, int] | None = None,
    ) -> None:
        self.button_map = dict(button_map)
        self.evdev_to_button = {v.lower(): k for k, v in button_map.items()}
        self.event_binding_to_button = {}
        self.event_code_to_button = {}
        resolved_button_values = {
            button_id: int(value) for button_id, value in (button_values or {}).items()
        }
        for button_id, code in (button_codes or {}).items():
            event_type = resolve_evdev_event_type(button_map.get(button_id))
            if event_type is None:
                continue
            normalized_value = normalize_evdev_binding_value(
                event_type,
                resolved_button_values.get(button_id),
            )
            self.event_binding_to_button[(int(event_type), int(code), normalized_value)] = button_id
            if normalized_value is None:
                self.event_code_to_button[(int(event_type), int(code))] = button_id

    async def reset_mapping_runtime_state(self) -> None:
        for event_name in self.state.combo_passthrough_held:
            self.state.held_source_actions.setdefault(event_name, None)
        self.state.combo_passthrough_held.clear()
        self.state.combo_recalled_bindings.clear()
        await self.reset_superkeys()
        runtime_grab.seed_startup_held_actions(self)

    async def reset_superkeys(self) -> None:
        for machine in self.state.superkey_machines.values():
            await machine.stop()
        self.state.superkey_machines.clear()

    async def grab(self) -> None:
        self.device = _device_input(self.path)
        caps = self.device.capabilities()
        caps.pop(evdev.ecodes.EV_SYN, None)

        passthrough_name, passthrough_vendor, passthrough_product = uinput_identity(
            f"keyforge-{self.hardware_id}",
            "passthrough",
            test_name=f"passthrough-{self.hardware_id}",
        )
        if passthrough_vendor is None or passthrough_product is None:
            self.uinput = evdev.UInput(
                events=cast(dict[int, Sequence[int]], caps),
                name=passthrough_name,
            )
        else:
            self.uinput = evdev.UInput(
                events=cast(dict[int, Sequence[int]], caps),
                name=passthrough_name,
                vendor=passthrough_vendor,
                product=passthrough_product,
            )

        try:
            await runtime_grab.wait_for_active_keys_to_clear(
                self,
                asyncio_mod=ASYNCIO_RUNTIME,
                time_mod=time,
                log=log,
                active_key_idle_max_wait_s=ACTIVE_KEY_IDLE_MAX_WAIT_S,
                active_key_idle_log_interval_s=ACTIVE_KEY_IDLE_LOG_INTERVAL_S,
            )
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
        self.task = asyncio.create_task(
            runtime_events.event_loop(self, asyncio_mod=ASYNCIO_RUNTIME, log=log)
        )

        log.info("Grabbed %s for %s", self.path, self.hardware_id)

    async def release(self) -> None:
        self._running = False
        runtime_outputs.release_all_keys(self, evdev_mod=evdev, uinput_writer=_uinput_writer)
        self.state.held_source_actions.clear()
        self.state.combo_passthrough_held.clear()
        self.state.combo_recalled_bindings.clear()

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
            except Exception as exc:
                log.warning("Failed to ungrab %s: %s", self.path, exc)
            try:
                self.device.close()
            except Exception as exc:
                log.warning("Failed to close input device %s: %s", self.path, exc)

        if self.uinput:
            try:
                self.uinput.close()
            except Exception as exc:
                log.warning("Failed to close passthrough uinput for %s: %s", self.path, exc)

        self.device = None
        self.uinput = None

        log.info("Released %s", self.path)

    def release_tracked_outputs(self) -> None:
        runtime_outputs.release_all_keys(self, evdev_mod=evdev, uinput_writer=_uinput_writer)

    def emit_combo_release(self, evdev_name: str) -> None:
        if not self.uinput:
            return
        code = resolve_output_code(evdev_name)
        if code is None:
            return
        runtime_outputs.write_key(
            self,
            self.uinput,
            code,
            0,
            evdev_mod=evdev,
            uinput_writer=_uinput_writer,
        )

    def emit_combo_press(self, evdev_name: str) -> None:
        if not self.uinput:
            return
        code = resolve_output_code(evdev_name)
        if code is None:
            return
        runtime_outputs.write_key(
            self,
            self.uinput,
            code,
            1,
            evdev_mod=evdev,
            uinput_writer=_uinput_writer,
        )

    def combo_passthrough_binding_active(self, evdev_name: str) -> bool:
        code = resolve_output_code(evdev_name)
        if code is None:
            return False
        return int(code) in self.state.held_output_keys["passthrough"]

    def combo_source_binding_held(self, evdev_name: str) -> bool:
        normalized = str(evdev_name or "").lower()
        return (
            normalized in self.state.held_source_actions
            or normalized in self.state.combo_passthrough_held
        )

    def mark_combo_recalled_binding(self, evdev_name: str) -> None:
        self.state.combo_recalled_bindings.add(normalize_combo_evdev(evdev_name))

    def clear_combo_recalled_binding(self, evdev_name: str) -> None:
        self.state.combo_recalled_bindings.discard(normalize_combo_evdev(evdev_name))

    def has_held_source_inputs(self) -> bool:
        return bool(self.state.held_source_actions)

    def combo_passthrough_held_modifiers(self) -> set[str]:
        return {
            event_name
            for event_name in self.state.combo_passthrough_held
            if normalize_combo_evdev(event_name) in COMBO_HELD_REARM_MODIFIERS
        }
