import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from types import SimpleNamespace
from typing import Final, cast

import evdev

from keymasq.common.combos import normalize_combo_evdev
from keymasq.common.devices import (
    get_interface_id,
    normalize_evdev_binding_value,
    resolve_evdev_code,
    resolve_evdev_event_type,
    resolve_stable_path,
)
from keymasq.common.models import ActionType, DeviceType, MappingAction
from keymasq.keymasqd.evdev_clock import set_evdev_clock_monotonic
from keymasq.keymasqd.output_helpers import resolve_output_code
from keymasq.keymasqd.recording import RecordingManager
from keymasq.keymasqd.runtime import adapters as runtime_adapters
from keymasq.keymasqd.runtime import analog_controls as runtime_analog_controls
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
    EmergencyResetter,
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
DEFAULT_UINPUT_VERSION = 0x0001
DEFAULT_UINPUT_BUSTYPE = 0x0003

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
    device = cast(object, evdev.InputDevice(path))
    set_evdev_clock_monotonic(device, device_path=path, logger=log)
    return cast(_ManagedInputDevice, device)


def _uinput_writer(device: object | None) -> _WritableUInput | None:
    return identity_uinput_writer(device)


def _is_gamepad_passthrough(device_type: DeviceType, device_types: Sequence[str]) -> bool:
    if device_type == DeviceType.GAMEPAD:
        return True
    return "gamepad" in {str(value or "").strip().lower() for value in device_types}


def _passthrough_name(
    device: _ManagedInputDevice,
    hardware_id: str,
    interface_id: str,
    *,
    is_gamepad: bool,
) -> str:
    if not is_gamepad:
        return f"keymasq-{hardware_id}"

    source_name = str(getattr(device, "name", "") or "").strip()
    if source_name:
        return source_name

    suffix = str(interface_id or "").strip() or str(hardware_id or "").strip()
    if suffix:
        return f"Keymasq Gamepad Passthrough ({suffix})"
    return "Keymasq Gamepad Passthrough"


def _int_u16(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value, 0)
        except ValueError:
            return None
    else:
        return None
    if 0 <= parsed <= 0xFFFF:
        return parsed
    return None


def _hardware_id_vendor_product(hardware_id: str) -> tuple[int | None, int | None]:
    parts = str(hardware_id or "").split(":", 1)
    if len(parts) != 2:
        return None, None
    try:
        vendor = int(parts[0], 16)
        product = int(parts[1].split("@", 1)[0], 16)
    except ValueError:
        return None, None
    return _int_u16(vendor), _int_u16(product)


def _passthrough_input_id(
    device: _ManagedInputDevice,
    hardware_id: str,
) -> tuple[int | None, int | None, int, int]:
    info = getattr(device, "info", None)
    vendor = _int_u16(getattr(info, "vendor", None))
    product = _int_u16(getattr(info, "product", None))
    if vendor is None or product is None:
        vendor, product = _hardware_id_vendor_product(hardware_id)

    version = _int_u16(getattr(info, "version", None))
    bustype = _int_u16(getattr(info, "bustype", None))
    return (
        vendor,
        product,
        DEFAULT_UINPUT_VERSION if version is None else version,
        DEFAULT_UINPUT_BUSTYPE if bustype is None else bustype,
    )


def _passthrough_input_props(device: _ManagedInputDevice) -> Sequence[int] | None:
    try:
        converted: list[int] = []
        for value in device.input_props():
            parsed = _int_u16(value)
            if parsed is None:
                return None
            converted.append(parsed)
        return converted
    except Exception:
        return None


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
        gamepad_output_resolver: Callable[[str | None, str], object | None] | None = None,
        broadcast_callback: BroadcastCallback | None = None,
        cursor_position_setter: CursorPositionSetter | None = None,
        recording_manager: RecordingManager | None = None,
        macro_player: MacroPlayer | None = None,
        emergency_resetter: EmergencyResetter | None = None,
        suppress_rel_getter: Callable[[], bool] | None = None,
        mouse_rel_suppression_start_callback: Callable[[], None] | None = None,
        diagnostics_recorder: Callable[[str, float], None] | None = None,
        runtime_cleanup_callback: Callable[[str, str | None], Awaitable[None]] | None = None,
        button_codes: dict[str, int] | None = None,
        button_values: dict[str, int] | None = None,
        analog_inputs: dict[str, object] | None = None,
    ) -> None:
        self.path = path
        self.hardware_id = hardware_id
        self.stable_path = resolve_stable_path(path)
        self.interface_id = str(get_interface_id(self.stable_path) or "").lower()
        self.button_map: dict[str, str] = {}
        self.evdev_to_button: dict[str, str] = {}
        self.event_binding_to_button: dict[tuple[int, int, int | None], str] = {}
        self.event_code_to_button: dict[tuple[int, int], str] = {}
        self.device: _ManagedInputDevice | None = None
        self.uinput: evdev.UInput | None = None
        self.update_button_map(button_map, button_codes, button_values)
        self.analog_inputs: dict[str, object] = {}
        self.analog_axis_bindings: dict[tuple[int, int], tuple[str, str]] = {}
        self.analog_axis_output_codes: dict[tuple[str, str], int] = {}
        self.analog_axis_ranges: dict[tuple[str, str], tuple[int, int]] = {}
        self.analog_axis_calibrations: dict[tuple[str, str], dict[str, object]] = {}
        self.analog_input_types: dict[str, str] = {}
        self.update_analog_inputs(analog_inputs or {})
        self.mapping_getter = mapping_getter
        self.event_callback = event_callback
        self.device_type = device_type
        self.device_types = device_types or [device_type.value]
        self.verbosity = verbosity
        self.keyboard_uinput = keyboard_uinput
        self.mouse_uinput = mouse_uinput
        self.gamepad_uinput = gamepad_uinput
        self._gamepad_output_resolver = gamepad_output_resolver
        self.broadcast_callback = broadcast_callback
        self.cursor_position_setter = cursor_position_setter
        self.recording_manager: RecordingManager | None = recording_manager
        self.macro_player = macro_player
        self.emergency_resetter = emergency_resetter
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

    def update_analog_inputs(self, analog_inputs: dict[str, object]) -> None:
        self.analog_inputs = dict(analog_inputs)
        self.analog_axis_bindings = {}
        self.analog_axis_output_codes = {}
        self.analog_axis_ranges = {}
        self.analog_axis_calibrations = {}
        self.analog_input_types = {}
        for analog_id, raw_input in self.analog_inputs.items():
            if not isinstance(raw_input, dict):
                continue
            input_data = cast(dict[str, object], raw_input)
            source = str(input_data.get("source", "") or "").strip().lower()
            if source and source != self.interface_id:
                continue
            self.analog_input_types[str(analog_id)] = str(
                input_data.get("type", "stick") or "stick"
            ).lower()
            raw_axes = input_data.get("axes")
            if not isinstance(raw_axes, list):
                continue
            for raw_axis in cast(list[object], raw_axes):
                if not isinstance(raw_axis, dict):
                    continue
                axis_data = cast(dict[str, object], raw_axis)
                role = str(axis_data.get("role", "") or "").strip().lower()
                if role not in {"x", "y"}:
                    continue
                code = _axis_code(axis_data)
                if code is None:
                    continue
                self.analog_axis_bindings[(int(evdev.ecodes.EV_ABS), int(code))] = (
                    str(analog_id),
                    role,
                )
                self.analog_axis_output_codes[(str(analog_id), role)] = int(code)
                calibration = _axis_calibration(axis_data)
                if calibration:
                    self.analog_axis_calibrations[(str(analog_id), role)] = calibration
        self._refresh_analog_axis_ranges()

    async def reset_mapping_runtime_state(
        self,
        previous_mapping: dict[str, MappingAction] | None = None,
    ) -> None:
        for event_name in self.state.combo_passthrough_held:
            self.state.held_source_keys.add(event_name)
            self.state.held_source_actions.setdefault(event_name, None)
        self.state.combo_passthrough_held.clear()
        self.state.combo_recalled_bindings.clear()
        preserve_analog_state_keys = (
            runtime_analog_controls.preserved_analog_state_keys(
                previous_mapping,
                self.mapping_getter(),
            )
            if previous_mapping is not None
            else set[str]()
        )
        await self.reset_analog_controls(preserve_state_keys=preserve_analog_state_keys)
        await self.reset_superkeys()
        runtime_grab.seed_startup_held_actions(self)

    async def reset_superkeys(self) -> None:
        for machine in self.state.superkey_machines.values():
            await machine.stop()
        self.state.superkey_machines.clear()

    async def reset_analog_controls(
        self,
        preserve_state_keys: set[str] | None = None,
    ) -> None:
        await runtime_analog_controls.reset_analog_controls(
            self,
            deps=runtime_events.build_action_execution_deps(),
            preserve_state_keys=preserve_state_keys,
        )

    async def grab(self) -> None:
        self.device = _device_input(self.path)
        self._refresh_analog_axis_ranges()
        caps = self.device.capabilities()
        caps.pop(evdev.ecodes.EV_SYN, None)
        is_gamepad_passthrough = _is_gamepad_passthrough(self.device_type, self.device_types)

        passthrough_name, passthrough_vendor, passthrough_product = uinput_identity(
            _passthrough_name(
                self.device,
                self.hardware_id,
                self.interface_id,
                is_gamepad=is_gamepad_passthrough,
            ),
            "passthrough",
            test_name=f"passthrough-{self.hardware_id}",
        )
        passthrough_version: int | None = None
        passthrough_bustype: int | None = None
        passthrough_input_props = None
        if (
            is_gamepad_passthrough
            and passthrough_vendor is None
            and passthrough_product is None
        ):
            (
                passthrough_vendor,
                passthrough_product,
                passthrough_version,
                passthrough_bustype,
            ) = _passthrough_input_id(self.device, self.hardware_id)
            passthrough_input_props = _passthrough_input_props(self.device)

        if passthrough_vendor is None or passthrough_product is None:
            self.uinput = evdev.UInput(
                events=cast(dict[int, Sequence[int]], caps),
                name=passthrough_name,
            )
        elif passthrough_version is not None and passthrough_bustype is not None:
            if passthrough_input_props is None:
                self.uinput = evdev.UInput(
                    events=cast(dict[int, Sequence[int]], caps),
                    name=passthrough_name,
                    vendor=passthrough_vendor,
                    product=passthrough_product,
                    version=passthrough_version,
                    bustype=passthrough_bustype,
                )
            else:
                self.uinput = evdev.UInput(
                    events=cast(dict[int, Sequence[int]], caps),
                    name=passthrough_name,
                    vendor=passthrough_vendor,
                    product=passthrough_product,
                    version=passthrough_version,
                    bustype=passthrough_bustype,
                    input_props=passthrough_input_props,
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
                finally:
                    runtime_outputs.unregister_passthrough_frame_output(self.uinput)
                self.uinput = None
            raise

        self._running = True
        self.task = asyncio.create_task(
            runtime_events.event_loop(self, asyncio_mod=ASYNCIO_RUNTIME, log=log)
        )

        log.info("Grabbed %s for %s", self.path, self.hardware_id)

    async def release(self) -> None:
        self._running = False
        await self.reset_analog_controls()
        await self.reset_superkeys()
        runtime_outputs.release_all_keys(self, evdev_mod=evdev, uinput_writer=_uinput_writer)
        self.state.held_source_keys.clear()
        self.state.held_source_actions.clear()
        self.state.combo_passthrough_held.clear()
        self.state.combo_recalled_bindings.clear()

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
            finally:
                runtime_outputs.unregister_passthrough_frame_output(self.uinput)

        self.device = None
        self.uinput = None

        log.info("Released %s", self.path)

    def release_tracked_outputs(self) -> None:
        runtime_outputs.release_all_keys(self, evdev_mod=evdev, uinput_writer=_uinput_writer)

    def resolve_gamepad_output(self, output_id: str | None, context: str) -> object | None:
        if self._gamepad_output_resolver is None:
            return SimpleNamespace(
                output_id=output_id or "virtual-gamepad-1",
                uinput=self.gamepad_uinput,
                bucket="gamepad",
                is_virtual=True,
            )
        return self._gamepad_output_resolver(output_id, context)

    def _combo_binding_action(self, evdev_name: str) -> object | None:
        return self.state.held_source_actions.get(str(evdev_name or "").lower())

    def _combo_binding_target_uinput(self, action_type: ActionType) -> object | None:
        if action_type == ActionType.KEYBOARD:
            return self.keyboard_uinput
        if action_type == ActionType.MOUSE:
            return self.mouse_uinput
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
        code = resolve_output_code(held_action.target or "")
        if code is None:
            return None
        if held_action.action_type == ActionType.KEYBOARD:
            target_uinput = self._combo_binding_target_uinput(held_action.action_type)
            if target_uinput is None:
                return None
            bucket = "keyboard"
        elif held_action.action_type == ActionType.MOUSE:
            target_uinput = self._combo_binding_target_uinput(held_action.action_type)
            if target_uinput is None:
                return None
            bucket = "mouse"
        elif held_action.action_type == ActionType.GAMEPAD:
            target = self.resolve_gamepad_output(
                held_action.output_id,
                f"combo binding {evdev_name} -> {held_action.target}",
            )
            if target is None:
                return None
            target_uinput = getattr(target, "uinput", None)
            if target_uinput is None:
                return None
            bucket = str(getattr(target, "bucket", "gamepad"))
        else:
            return None
        return (target_uinput, int(code), bucket)

    def emit_combo_release(self, evdev_name: str) -> None:
        output = self._combo_binding_output(evdev_name)
        if output is None:
            return
        target_uinput, code, bucket = output
        runtime_outputs.write_key(
            self,
            target_uinput,
            code,
            0,
            evdev_mod=evdev,
            uinput_writer=_uinput_writer,
            bucket=bucket,
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
            bucket=bucket,
        )
        if bucket == "passthrough":
            self.state.combo_passthrough_held.add(str(evdev_name or "").lower())

    def combo_passthrough_binding_active(self, evdev_name: str) -> bool:
        output = self._combo_binding_output(evdev_name)
        if output is None:
            return False
        _target_uinput, code, bucket = output
        return int(code) in self.state.held_output_keys.get(bucket, set())

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

    def _refresh_analog_axis_ranges(self) -> None:
        if self.device is None:
            return
        for (_event_type, code), (analog_id, role) in self.analog_axis_bindings.items():
            try:
                info = self.device.absinfo(int(code))
            except Exception:
                continue
            minimum = getattr(info, "min", None)
            maximum = getattr(info, "max", None)
            key = (analog_id, role)
            calibration = self.analog_axis_calibrations.setdefault(key, {})
            if "minimum" not in calibration and isinstance(minimum, int):
                calibration["minimum"] = minimum
            if "maximum" not in calibration and isinstance(maximum, int):
                calibration["maximum"] = maximum
            current = getattr(info, "value", None)
            if (
                self.analog_input_types.get(analog_id) == "axis"
                and "rest" not in calibration
                and isinstance(current, int)
            ):
                calibration["rest"] = current
            minimum_value = calibration.get("minimum", minimum)
            maximum_value = calibration.get("maximum", maximum)
            if isinstance(minimum_value, int) and isinstance(maximum_value, int):
                self.analog_axis_ranges[key] = (minimum_value, maximum_value)


def _axis_code(axis: dict[str, object]) -> int | None:
    evdev_code = axis.get("evdev_code")
    if isinstance(evdev_code, int):
        return evdev_code
    if isinstance(evdev_code, str):
        try:
            return int(evdev_code, 0)
        except ValueError:
            return None
    return resolve_evdev_code(str(axis.get("evdev", "") or ""))


def _axis_calibration(axis: dict[str, object]) -> dict[str, object]:
    calibration: dict[str, object] = {}
    for field in ("minimum", "maximum", "center", "rest"):
        value = axis.get(field)
        if isinstance(value, int):
            calibration[field] = value
        elif isinstance(value, str):
            try:
                calibration[field] = int(value, 0)
            except ValueError:
                pass
    if bool(axis.get("invert", False)):
        calibration["invert"] = True
    return calibration
