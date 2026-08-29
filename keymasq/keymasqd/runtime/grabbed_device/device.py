import asyncio
import inspect
import logging
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from types import SimpleNamespace
from typing import NotRequired, TypedDict, cast

import evdev

from keymasq.common.combos import normalize_combo_evdev
from keymasq.common.devices import (
    get_interface_id,
    input_classes_include_gamepad,
    normalize_evdev_binding_value,
    resolve_evdev_code,
    resolve_evdev_event_type,
    resolve_stable_path,
)
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType, DeviceType
from keymasq.keymasqd.evdev_clock import set_evdev_clock_monotonic
from keymasq.keymasqd.output_helpers import resolve_output_code
from keymasq.keymasqd.recording import RecordingManager
from keymasq.keymasqd.runtime import adapters, force_feedback, source_hiding
from keymasq.keymasqd.runtime.adapters import identity_uinput_writer
from keymasq.keymasqd.runtime.analog.binding_state import preserved_analog_state_keys
from keymasq.keymasqd.runtime.analog.reset import reset_analog_controls
from keymasq.keymasqd.runtime.grabbed_device import grab, outputs
from keymasq.keymasqd.runtime.grabbed_device.event import pipeline
from keymasq.keymasqd.runtime.grabbed_device.types import (
    BroadcastCallback,
    CursorPositionSetter,
    DeviceEventCallback,
    DeviceInspectorActiveGetter,
    DeviceInspectorEventCallback,
    DeviceInspectorSuppressedIdsGetter,
    DeviceInspectorSuppressionDisabler,
    DeviceInspectorSuppressionGetter,
    EmergencyResetter,
    GrabbedDeviceState,
    MacroPlayer,
    ManagedInputDevice,
    MappingGetter,
    NaturalMouseMover,
    RuntimeDisconnectCallback,
)
from keymasq.keymasqd.runtime.outputs import (
    create_uinput_with_permission_hint,
    uinput_identity,
)
from keymasq.keymasqd.runtime.repeat import RepeatRuntimeState

log = logging.getLogger("keymasqd.devices")
ACTIVE_KEY_IDLE_LOG_INTERVAL_S = 1.0
ACTIVE_KEY_IDLE_MAX_WAIT_S = 300.0
COMBO_HELD_REARM_MODIFIERS = frozenset({"shift", "ctrl", "alt", "meta"})
DEFAULT_UINPUT_VERSION = 0x0001
DEFAULT_UINPUT_BUSTYPE = 0x0003
INFLIGHT_ACTION_CANCEL_TIMEOUT_S = 0.5


class _PassthroughUInputKwargs(TypedDict):
    events: dict[int, Sequence[int]]
    name: str
    vendor: NotRequired[int]
    product: NotRequired[int]
    version: NotRequired[int]
    bustype: NotRequired[int]
    input_props: NotRequired[Sequence[int]]
    max_effects: NotRequired[int]


def _device_input(path: str) -> ManagedInputDevice:
    device = cast(object, evdev.InputDevice(path))
    set_evdev_clock_monotonic(device, device_path=path, logger=log)
    return cast(ManagedInputDevice, device)


def _is_gamepad_passthrough(device_type: DeviceType, device_types: Sequence[str]) -> bool:
    return input_classes_include_gamepad(device_types, device_type)


def _passthrough_name(
    device: ManagedInputDevice,
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


def _passthrough_abs_neutral_value(code: int, info: object | None) -> int:
    """Infer a stable resting value without sampling the current axis position."""

    minimum = getattr(info, "min", None)
    maximum = getattr(info, "max", None)
    if not isinstance(minimum, int) or not isinstance(maximum, int):
        return 0

    hat_first = int(getattr(evdev.ecodes, "ABS_HAT0X", 0x10))
    hat_last = int(getattr(evdev.ecodes, "ABS_HAT3Y", 0x17))
    if hat_first <= code <= hat_last:
        return min(max(0, minimum), maximum)

    centered_codes = {
        int(getattr(evdev.ecodes, "ABS_X", 0x00)),
        int(getattr(evdev.ecodes, "ABS_Y", 0x01)),
        int(getattr(evdev.ecodes, "ABS_RX", 0x03)),
        int(getattr(evdev.ecodes, "ABS_RY", 0x04)),
    }
    if code in centered_codes:
        if minimum < 0 < maximum:
            return 0
        return minimum + ((maximum - minimum) // 2)
    if minimum <= 0 <= maximum:
        return 0
    return minimum


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
    device: ManagedInputDevice,
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


def _passthrough_input_props(device: ManagedInputDevice) -> Sequence[int] | None:
    try:
        converted: list[int] = []
        for value in device.input_props():
            parsed = _int_u16(value)
            if parsed is None:
                return None
            converted.append(parsed)
        return converted
    except OSError as exc:
        log.debug("Failed to read passthrough input props: %s", exc)
        return None
    except Exception:
        log.exception("Unexpected failure reading passthrough input props")
        return None


def _copy_passthrough_capabilities(
    device: ManagedInputDevice,
) -> tuple[dict[int, Sequence[object]], int]:
    caps: dict[int, Sequence[object]] = {
        int(event_type): list(codes) for event_type, codes in device.capabilities().items()
    }
    caps.pop(evdev.ecodes.EV_SYN, None)
    ff_max_effects = force_feedback.passthrough_ff_max_effects(caps, device)
    if ff_max_effects <= 0:
        force_feedback.disable_force_feedback(caps)
    return caps, ff_max_effects


def _uinput_supports_max_effects(uinput_factory: Callable[..., object]) -> bool:
    try:
        parameters = inspect.signature(uinput_factory).parameters
    except (TypeError, ValueError):
        return True
    return "max_effects" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


def _passthrough_uinput_kwargs(
    *,
    caps: dict[int, Sequence[object]],
    passthrough_name: str,
    passthrough_vendor: int | None,
    passthrough_product: int | None,
    passthrough_version: int | None,
    passthrough_bustype: int | None,
    passthrough_input_props: Sequence[int] | None,
    ff_max_effects: int,
    supports_max_effects: bool,
) -> _PassthroughUInputKwargs:
    events = {int(event_type): list(codes) for event_type, codes in caps.items()}
    kwargs: _PassthroughUInputKwargs = {
        "events": cast(dict[int, Sequence[int]], events),
        "name": passthrough_name,
    }
    if passthrough_vendor is not None and passthrough_product is not None:
        kwargs["vendor"] = passthrough_vendor
        kwargs["product"] = passthrough_product
    if passthrough_version is not None and passthrough_bustype is not None:
        kwargs["version"] = passthrough_version
        kwargs["bustype"] = passthrough_bustype
    if passthrough_input_props is not None:
        kwargs["input_props"] = passthrough_input_props
    if supports_max_effects:
        kwargs["max_effects"] = ff_max_effects
    return kwargs


def _close_passthrough_uinput(
    uinput: object | None,
    *,
    context: str,
    close_error_log_level: int = logging.DEBUG,
) -> None:
    if uinput is None:
        return
    try:
        close = getattr(uinput, "close", None)
        if callable(close):
            close()
    except OSError as exc:
        if close_error_log_level <= logging.DEBUG:
            log.debug("Failed to close passthrough uinput during %s", context, exc_info=True)
        else:
            log.log(
                close_error_log_level,
                "Failed to close passthrough uinput during %s: %s",
                context,
                exc,
            )
    except Exception:
        log.exception("Unexpected failure closing passthrough uinput during %s", context)


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
        natural_mouse_mover: NaturalMouseMover | None = None,
        recording_manager: RecordingManager | None = None,
        macro_player: MacroPlayer | None = None,
        emergency_resetter: EmergencyResetter | None = None,
        inspector_event_callback: DeviceInspectorEventCallback | None = None,
        inspector_active_getter: DeviceInspectorActiveGetter | None = None,
        inspector_suppression_getter: DeviceInspectorSuppressionGetter | None = None,
        inspector_suppressed_ids_getter: DeviceInspectorSuppressedIdsGetter | None = None,
        inspector_suppression_disabler: DeviceInspectorSuppressionDisabler | None = None,
        profile_activation_recorder: Callable[[str | None, str | None], None] | None = None,
        profile_activation_trigger_start_observer: Callable[[str | None], None] | None = None,
        profile_activation_trigger_end_observer: Callable[[str | None], None] | None = None,
        suppress_rel_getter: Callable[[], bool] | None = None,
        mouse_rel_suppression_start_callback: Callable[[], None] | None = None,
        diagnostics_recorder: Callable[[str, float], None] | None = None,
        runtime_cleanup_callback: Callable[[str, str | None], Awaitable[None]] | None = None,
        runtime_disconnect_callback: RuntimeDisconnectCallback | None = None,
        repeat_state: RepeatRuntimeState | None = None,
        button_codes: dict[str, int] | None = None,
        button_values: dict[str, int] | None = None,
        analog_inputs: dict[str, object] | None = None,
        interface_id: str | None = None,
    ) -> None:
        self.path = path
        self.resolved_event_path = os.path.realpath(path)
        self.hardware_id = hardware_id
        self.stable_path = resolve_stable_path(path)
        self.interface_id = str(interface_id or get_interface_id(self.stable_path) or "").lower()
        self.button_map: dict[str, str] = {}
        self.evdev_to_button: dict[str, str] = {}
        self.event_binding_to_button: dict[tuple[int, int, int | None], str] = {}
        self.event_code_to_button: dict[tuple[int, int], str] = {}
        self.device: ManagedInputDevice | None = None
        self.uinput: evdev.UInput | None = None
        self.force_feedback_proxy: force_feedback.PassthroughForceFeedbackProxy | None = None
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
        self.natural_mouse_mover = natural_mouse_mover
        self.recording_manager: RecordingManager | None = recording_manager
        self.macro_player = macro_player
        self.emergency_resetter = emergency_resetter
        self.inspector_event_callback = inspector_event_callback
        self.inspector_active_getter = inspector_active_getter
        self.inspector_suppression_getter = inspector_suppression_getter
        self.inspector_suppressed_ids_getter = inspector_suppressed_ids_getter
        self.inspector_suppression_disabler = inspector_suppression_disabler
        self.profile_activation_recorder = profile_activation_recorder
        self.profile_activation_trigger_start_observer = profile_activation_trigger_start_observer
        self.profile_activation_trigger_end_observer = profile_activation_trigger_end_observer
        self.suppress_rel_getter = suppress_rel_getter
        self.mouse_rel_suppression_start_callback = mouse_rel_suppression_start_callback
        self.diagnostics_recorder = diagnostics_recorder
        self.runtime_cleanup_callback = runtime_cleanup_callback
        self.runtime_disconnect_callback = runtime_disconnect_callback
        if repeat_state is None:
            log.warning(
                "GrabbedDevice %s created without shared RepeatRuntimeState; "
                "using isolated test-only repeat state",
                hardware_id,
            )
        self.repeat_state = repeat_state if repeat_state is not None else RepeatRuntimeState()
        self.task: asyncio.Task[None] | None = None
        self.running = False
        self.input_suspended = False
        self.current_event_task: asyncio.Task[None] | None = None
        self.pending_key_clear_task: asyncio.Task[None] | None = None
        self.background_tasks: set[asyncio.Task[object]] = set()
        self.source_hidden_kernel_names: list[str] = []
        self.source_pending_hidden_kernel_names: list[str] = []
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
            preserved_analog_state_keys(
                previous_mapping,
                self.mapping_getter(),
            )
            if previous_mapping is not None
            else set[str]()
        )
        await self.reset_analog_controls(preserve_state_keys=preserve_analog_state_keys)
        await self.reset_superkeys()
        grab.seed_startup_held_actions(self)

    async def reset_superkeys(self) -> None:
        for machine in self.state.superkey_machines.values():
            await machine.stop()
        self.state.superkey_machines.clear()

    async def neutralize_superkeys(self) -> None:
        for machine in self.state.superkey_machines.values():
            await machine.neutralize()
        self.state.superkey_machines.clear()

    async def cancel_pending_superkey_timers(self) -> None:
        for machine in list(self.state.superkey_machines.values()):
            await machine.cancel_pending_gesture_timers()

    async def reset_analog_controls(
        self,
        preserve_state_keys: set[str] | None = None,
    ) -> None:
        await reset_analog_controls(
            self,
            deps=pipeline.build_action_execution_deps(),
            preserve_state_keys=preserve_state_keys,
        )

    async def neutralize_analog_controls(self) -> None:
        """Discard active analog gestures without emitting release actions."""

        await reset_analog_controls(
            self,
            deps=pipeline.build_action_execution_deps(),
            release_threshold_transitions=False,
        )

    async def _cleanup_failed_grab(self) -> None:
        await self._stop_force_feedback_proxy()
        uinput = self.uinput
        if uinput is not None:
            _close_passthrough_uinput(uinput, context="failed grab")
        self.uinput = None

        device = self.device
        if device is not None:
            try:
                device.close()
            except OSError:
                log.debug("Failed to close input device after failed grab", exc_info=True)
            except Exception:
                log.exception("Unexpected failure closing input device after failed grab")
        self.device = None

        hidden_names = self.source_hidden_kernel_names
        self.source_hidden_kernel_names = []
        self.source_pending_hidden_kernel_names = []
        if hidden_names:
            try:
                await source_hiding.restore_source_by_kernel_names(hidden_names)
            except Exception:
                log.exception("Unexpected failure restoring hidden source after failed grab")

    async def grab(self) -> None:
        self.resolved_event_path = os.path.realpath(self.path)
        self.source_hidden_kernel_names = []
        self.device = _device_input(self.path)

        try:
            self._refresh_analog_axis_ranges()
            caps, ff_max_effects = _copy_passthrough_capabilities(self.device)
            await self._refresh_passthrough_abs_neutral_values(caps)
            is_gamepad_passthrough = _is_gamepad_passthrough(
                self.device_type,
                self.device_types,
            )

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
            passthrough_vendor = _int_u16(passthrough_vendor)
            passthrough_product = _int_u16(passthrough_product)
            passthrough_version: int | None = None
            passthrough_bustype: int | None = None
            passthrough_input_props = None
            if is_gamepad_passthrough and (
                passthrough_vendor is None or passthrough_product is None
            ):
                (
                    passthrough_vendor,
                    passthrough_product,
                    passthrough_version,
                    passthrough_bustype,
                ) = _passthrough_input_id(self.device, self.hardware_id)
                passthrough_input_props = _passthrough_input_props(self.device)

            if passthrough_vendor is None or passthrough_product is None:
                passthrough_version = None
                passthrough_bustype = None
                passthrough_input_props = None

            supports_max_effects = _uinput_supports_max_effects(evdev.UInput)
            if ff_max_effects > 0 and not supports_max_effects:
                log.debug(
                    "python-evdev UInput does not support max_effects; "
                    "using evdev's default force-feedback capacity for %s",
                    self.path,
                )

            def make_passthrough_uinput(max_effects: int) -> evdev.UInput:
                return create_uinput_with_permission_hint(
                    "passthrough",
                    lambda: evdev.UInput(
                        **_passthrough_uinput_kwargs(
                            caps=caps,
                            passthrough_name=passthrough_name,
                            passthrough_vendor=passthrough_vendor,
                            passthrough_product=passthrough_product,
                            passthrough_version=passthrough_version,
                            passthrough_bustype=passthrough_bustype,
                            passthrough_input_props=passthrough_input_props,
                            ff_max_effects=max_effects,
                            supports_max_effects=supports_max_effects,
                        )
                    ),
                )

            self.uinput = make_passthrough_uinput(ff_max_effects)
            if ff_max_effects > 0:
                try:
                    self._start_force_feedback_proxy()
                except Exception:  # noqa: BLE001 - retry without advertised FF support.
                    log.warning(
                        "Disabling passthrough force feedback for %s after proxy start failure",
                        self.path,
                        exc_info=True,
                    )
                    _close_passthrough_uinput(self.uinput, context="force-feedback retry")
                    self.uinput = None
                    force_feedback.disable_force_feedback(caps)
                    ff_max_effects = 0
                    self.uinput = make_passthrough_uinput(ff_max_effects)

            if self.input_suspended:
                raise grab.GrabInterruptedForSleepError(
                    f"Grab of {self.path} interrupted before suspend"
                )
            key_clear_task = adapters.ASYNCIO_RUNTIME.create_task(
                grab.wait_for_active_keys_to_clear(
                    self,
                    asyncio_mod=adapters.ASYNCIO_RUNTIME,
                    time_mod=time,
                    log=log,
                    active_key_idle_max_wait_s=ACTIVE_KEY_IDLE_MAX_WAIT_S,
                    active_key_idle_log_interval_s=ACTIVE_KEY_IDLE_LOG_INTERVAL_S,
                )
            )
            self.pending_key_clear_task = key_clear_task
            try:
                await key_clear_task
            except asyncio.CancelledError:
                current_task = asyncio.current_task()
                if not self.input_suspended or (
                    current_task is not None and current_task.cancelling()
                ):
                    raise
                raise grab.GrabInterruptedForSleepError(
                    f"Grab of {self.path} interrupted before suspend"
                ) from None
            finally:
                if self.pending_key_clear_task is key_clear_task:
                    self.pending_key_clear_task = None
            if self.input_suspended:
                raise grab.GrabInterruptedForSleepError(
                    f"Grab of {self.path} interrupted before suspend"
                )
            self.device.grab()
            if is_gamepad_passthrough:
                self.source_hidden_kernel_names = []
                self.source_pending_hidden_kernel_names = source_hiding.node_kernel_names(
                    self.resolved_event_path
                )
                try:
                    self.source_hidden_kernel_names = await source_hiding.hide_source(
                        self.resolved_event_path
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Unexpected failure hiding source for %s", self.path)
                finally:
                    self.source_pending_hidden_kernel_names = []
        except asyncio.CancelledError:
            await self._cleanup_failed_grab()
            raise
        except Exception:
            await self._cleanup_failed_grab()
            raise

        self.running = True
        self.task = asyncio.create_task(
            pipeline.event_loop(
                self,
                asyncio_mod=adapters.ASYNCIO_RUNTIME,
                log=log,
            )
        )

        log.info("Grabbed %s for %s", self.path, self.hardware_id)

    async def stop_event_loop(self) -> None:
        self.running = False
        if self.task:
            task = self.task
            self.task = None
            if task is not asyncio.current_task():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=1.0)
                except (TimeoutError, asyncio.CancelledError):
                    pass

    async def release(self) -> None:
        await self.stop_event_loop()
        await self.reset_runtime_state()

        await self._stop_force_feedback_proxy()

        if self.device:
            try:
                self.device.ungrab()
            except OSError as exc:
                log.warning("Failed to ungrab %s: %s", self.path, exc)
            except Exception:
                log.exception("Unexpected failure ungrabbing %s", self.path)
            try:
                self.device.close()
            except OSError as exc:
                log.warning("Failed to close input device %s: %s", self.path, exc)
            except Exception:
                log.exception("Unexpected failure closing input device %s", self.path)

        if self.uinput:
            _close_passthrough_uinput(
                self.uinput,
                context=f"release {self.path}",
                close_error_log_level=logging.WARNING,
            )

        self.device = None
        self.uinput = None
        hidden_names = self.source_hidden_kernel_names
        self.source_hidden_kernel_names = []
        self.source_pending_hidden_kernel_names = []
        if hidden_names:
            try:
                await source_hiding.restore_source_by_kernel_names(hidden_names)
            except Exception:
                log.exception("Unexpected failure restoring hidden source for %s", self.path)

        log.info("Released %s", self.path)

    async def neutralize_runtime_state(self) -> None:
        """Release generated state without dropping the physical device grab."""

        await self._clear_runtime_state(neutralize=True)

    async def reset_runtime_state(self) -> None:
        """Release runtime state using normal input-release semantics."""

        await self._clear_runtime_state(neutralize=False)

    async def _clear_runtime_state(self, *, neutralize: bool) -> None:
        """Cancel runtime work and release every generated output."""

        if neutralize:
            self.state.resume_suppressed_source_keys.update(self.state.held_source_keys)
        else:
            self.state.resume_suppressed_source_keys.clear()
        await self.cancel_inflight_actions()
        if neutralize:
            try:
                outputs.flush_passthrough_frame(
                    self,
                    self.uinput,
                    uinput_writer=identity_uinput_writer,
                )
            except Exception:
                log.exception("Failed to flush passthrough input on %s", self.path)
            try:
                outputs.neutralize_passthrough_abs(
                    self,
                    evdev_mod=evdev,
                    uinput_writer=identity_uinput_writer,
                )
            except Exception:
                log.exception("Failed to neutralize passthrough ABS input on %s", self.path)
        try:
            if neutralize:
                await self.neutralize_analog_controls()
            else:
                await self.reset_analog_controls()
        except Exception:
            log.exception("Failed to reset analog controls on %s", self.path)
        try:
            if neutralize:
                await self.neutralize_superkeys()
            else:
                await self.reset_superkeys()
        except Exception:
            log.exception("Failed to reset superkeys on %s", self.path)
        pipeline.observe_profile_trigger_end_for_held_sources(self)
        try:
            outputs.release_all_keys(
                self,
                evdev_mod=evdev,
                uinput_writer=identity_uinput_writer,
            )
        except Exception:
            log.exception("Failed to release generated outputs on %s", self.path)
        self.state.held_source_keys.clear()
        self.state.held_source_actions.clear()
        self.state.combo_passthrough_held.clear()
        self.state.combo_recalled_bindings.clear()

    def fire_and_observe(
        self,
        coro: Awaitable[object],
        label: str,
    ) -> asyncio.Task[object]:
        """Launch and track action work that can outlive one input event."""

        task = pipeline.fire_and_observe(coro, label)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task

    async def cancel_inflight_actions(
        self,
        *,
        timeout_s: float = INFLIGHT_ACTION_CANCEL_TIMEOUT_S,
    ) -> None:
        """Cancel current event handling and detached action work."""

        tasks: list[asyncio.Task[object]] = []
        current_event_task = self.current_event_task
        if current_event_task is not None:
            tasks.append(cast(asyncio.Task[object], current_event_task))
        pending_key_clear_task = self.pending_key_clear_task
        if pending_key_clear_task is not None:
            tasks.append(cast(asyncio.Task[object], pending_key_clear_task))
        tasks.extend(self.background_tasks)
        self.background_tasks.clear()
        current_task = asyncio.current_task()
        tasks = list(dict.fromkeys(task for task in tasks if task is not current_task))
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            done, pending = await asyncio.wait(
                tasks,
                timeout=max(0.0, float(timeout_s)),
            )
            if done:
                await asyncio.gather(*done, return_exceptions=True)
            if pending:
                log.warning(
                    "Timed out draining %d cancelled input action task(s) on %s",
                    len(pending),
                    self.path,
                )
                self.background_tasks.update(pending)
                for task in pending:
                    task.add_done_callback(self.background_tasks.discard)

    def _start_force_feedback_proxy(self) -> None:
        if self.uinput is None or self.device is None:
            return
        proxy = force_feedback.PassthroughForceFeedbackProxy(
            cast(force_feedback.ForceFeedbackUInput, self.uinput),
            cast(force_feedback.ForceFeedbackTarget, self.device),
            label=f"{self.hardware_id}:{self.interface_id or self.path}",
        )
        proxy.start()
        self.force_feedback_proxy = proxy

    async def _stop_force_feedback_proxy(self) -> None:
        proxy = self.force_feedback_proxy
        self.force_feedback_proxy = None
        if proxy is None:
            return
        stop_and_wait = cast(
            Callable[[], Awaitable[None]] | None,
            getattr(proxy, "stop_and_wait", None),
        )
        if callable(stop_and_wait):
            await stop_and_wait()
            return
        proxy.stop()

    def release_tracked_outputs(self) -> None:
        outputs.release_all_keys(
            self,
            evdev_mod=evdev,
            uinput_writer=identity_uinput_writer,
        )

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
        outputs.write_key(
            self,
            target_uinput,
            code,
            0,
            evdev_mod=evdev,
            uinput_writer=identity_uinput_writer,
            bucket=bucket,
        )

    def emit_combo_press(self, evdev_name: str) -> None:
        output = self._combo_binding_output(evdev_name)
        if output is None:
            return
        target_uinput, code, bucket = output
        outputs.write_key(
            self,
            target_uinput,
            code,
            1,
            evdev_mod=evdev,
            uinput_writer=identity_uinput_writer,
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
            except OSError as exc:
                log.debug("Failed to read ABS info for %s code=%s: %s", self.path, code, exc)
                info = None
            except Exception:
                log.exception("Unexpected failure reading ABS info for %s code=%s", self.path, code)
                info = None
            if info is None:
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

    async def _refresh_passthrough_abs_neutral_values(
        self,
        caps: dict[int, Sequence[object]],
    ) -> None:
        if self.device is None:
            return
        neutral_values = self.state.passthrough_abs_neutral_values
        slot_code = int(getattr(evdev.ecodes, "ABS_MT_SLOT", -1))
        tracking_id_code = int(getattr(evdev.ecodes, "ABS_MT_TRACKING_ID", -1))
        abs_entries = caps.get(evdev.ecodes.EV_ABS, ())
        abs_codes = {
            raw_code
            for entry in abs_entries
            if isinstance(
                raw_code := (cast(object, entry[0]) if isinstance(entry, tuple) else entry),
                int,
            )
        }
        self.state.passthrough_mt_uses_slots = slot_code in abs_codes
        for entry in abs_entries:
            raw_code = cast(object, entry[0]) if isinstance(entry, tuple) else entry
            if not isinstance(raw_code, int):
                continue
            code = raw_code
            if code == tracking_id_code:
                neutral_values[code] = -1
                continue
            try:
                info = await asyncio.to_thread(self.device.absinfo, code)
            except Exception:  # noqa: BLE001 - optional neutral-state probe.
                info = None
            neutral_values[code] = _passthrough_abs_neutral_value(code, info)


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
