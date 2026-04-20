import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Final, cast

import evdev

from keymasq.common.combos import normalize_combo_evdev
from keymasq.common.devices import (
    get_interface_id,
    normalize_evdev_binding_value,
    resolve_evdev_event_type,
    resolve_stable_path,
)
from keymasq.common.models import ActionType, DeviceType, MappingAction
from keymasq.keymasqd.output_helpers import resolve_output_code
from keymasq.keymasqd.recording import RecordingManager
from keymasq.keymasqd.runtime import adapters as runtime_adapters
from keymasq.keymasqd.runtime import grabbed_device_events as runtime_events
from keymasq.keymasqd.runtime import grabbed_device_grab as runtime_grab
from keymasq.keymasqd.runtime import grabbed_device_outputs as runtime_outputs
from keymasq.keymasqd.runtime.grabbed_device_types import (
    AsyncioModule as _AsyncioModule,
)
from keymasq.keymasqd.runtime.grabbed_device_types import (
    BroadcastCallback,
    CursorPositionSetter,
    DeviceEventCallback,
    GrabbedDeviceState,
    MacroPlayer,
    MappingGetter,
    identity_uinput_writer,
)
from keymasq.keymasqd.runtime.grabbed_device_types import (
    ManagedInputDevice as _ManagedInputDevice,
)
from keymasq.keymasqd.runtime.grabbed_device_types import (
    WritableUInput as _WritableUInput,
)
from keymasq.keymasqd.runtime.outputs import uinput_identity

log = logging.getLogger("keymasqd.devices")
ACTIVE_KEY_IDLE_LOG_INTERVAL_S = 1.0
ACTIVE_KEY_IDLE_MAX_WAIT_S = 300.0
COMBO_HELD_REARM_MODIFIERS = frozenset({"shift", "ctrl", "alt", "meta"})

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


ASYNCIO_RUNTIME: Final[_AsyncioModule] = cast(_AsyncioModule, runtime_adapters.ASYNCIO_RUNTIME)


def _device_input(path: str) -> _ManagedInputDevice:
    return cast(_ManagedInputDevice, evdev.InputDevice(path))


def _uinput_writer(device: object | None) -> _WritableUInput | None:
    return identity_uinput_writer(device)


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
        cursor_position_setter: CursorPositionSetter | None = None,
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
        self.cursor_position_setter = cursor_position_setter
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
            self.state.held_source_keys.add(event_name)
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
            f"keymasq-{self.hardware_id}",
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
        self.state.held_source_keys.clear()
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

    def _combo_binding_action(self, evdev_name: str) -> object | None:
        return self.state.held_source_actions.get(str(evdev_name or "").lower())

    def _combo_binding_target_uinput(self, action_type: ActionType) -> object | None:
        if action_type == ActionType.KEYBOARD:
            return self.keyboard_uinput
        if action_type == ActionType.MOUSE:
            return self.mouse_uinput
        if action_type == ActionType.GAMEPAD:
            return self.gamepad_uinput
        return None

    def _combo_binding_output(self, evdev_name: str) -> tuple[object, int, str] | None:
        held_action = cast(MappingAction | None | object, self._combo_binding_action(evdev_name))
        if held_action is None:
            if not self.uinput:
                return None
            code = resolve_output_code(evdev_name)
            if code is None:
                return None
            return (self.uinput, int(code), "passthrough")

        if not isinstance(held_action, MappingAction):
            return None
        if held_action.action_type == ActionType.SUPPRESS:
            return None
        if held_action.action_type == ActionType.PASSTHROUGH:
            if not self.uinput:
                return None
            code = resolve_output_code(evdev_name)
            if code is None:
                return None
            return (self.uinput, int(code), "passthrough")
        if held_action.rapidfire_enabled or held_action.tap_enabled:
            return None
        target_uinput = self._combo_binding_target_uinput(held_action.action_type)
        code = resolve_output_code(held_action.target or "")
        if target_uinput is None or code is None:
            return None
        if held_action.action_type == ActionType.KEYBOARD:
            bucket = "keyboard"
        elif held_action.action_type == ActionType.MOUSE:
            bucket = "mouse"
        elif held_action.action_type == ActionType.GAMEPAD:
            bucket = "gamepad"
        else:
            return None
        return (target_uinput, int(code), bucket)

    def emit_combo_release(self, evdev_name: str) -> None:
        output = self._combo_binding_output(evdev_name)
        if output is None:
            return
        target_uinput, code, _bucket = output
        runtime_outputs.write_key(
            self,
            target_uinput,
            code,
            0,
            evdev_mod=evdev,
            uinput_writer=_uinput_writer,
        )

    def emit_combo_press(self, evdev_name: str) -> None:
        output = self._combo_binding_output(evdev_name)
        if output is None:
            return
        target_uinput, code, bucket = output
        runtime_outputs.write_key(
            self,
            target_uinput,
            code,
            1,
            evdev_mod=evdev,
            uinput_writer=_uinput_writer,
        )
        if bucket == "passthrough":
            self.state.combo_passthrough_held.add(str(evdev_name or "").lower())

    def combo_passthrough_binding_active(self, evdev_name: str) -> bool:
        output = self._combo_binding_output(evdev_name)
        if output is None:
            return False
        _target_uinput, code, bucket = output
        return int(code) in self.state.held_output_keys[bucket]

    def combo_source_binding_held(self, evdev_name: str) -> bool:
        normalized = str(evdev_name or "").lower()
        return normalized in self.state.held_source_keys

    def combo_binding_recalled(self, evdev_name: str) -> bool:
        return normalize_combo_evdev(evdev_name) in self.state.combo_recalled_bindings

    def mark_combo_recalled_binding(self, evdev_name: str) -> None:
        self.state.combo_recalled_bindings.add(normalize_combo_evdev(evdev_name))

    def clear_combo_recalled_binding(self, evdev_name: str) -> None:
        self.state.combo_recalled_bindings.discard(normalize_combo_evdev(evdev_name))

    def has_held_source_inputs(self) -> bool:
        return bool(self.state.held_source_keys)

    def combo_passthrough_held_modifiers(self) -> set[str]:
        return {
            event_name
            for event_name in self.state.held_source_keys
            if normalize_combo_evdev(event_name) in COMBO_HELD_REARM_MODIFIERS
        }

    def combo_held_source_bindings(self) -> set[str]:
        return set(self.state.held_source_keys)
