"""Mapping, wheel, recording, and passthrough action dispatch."""

from typing import cast

import evdev

from keymasq.common.devices import (
    high_res_wheel_low_res_code,
    normalize_wheel_value,
    wheel_button_id,
)
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.common.types import SyntheticInputEvent
from keymasq.keymasqd.runtime.action.triggers import source_trigger_id
from keymasq.keymasqd.runtime.grabbed_device import actions
from keymasq.keymasqd.runtime.grabbed_device.event.classification import (
    find_action_for_code,
    find_button_id_for_code,
    get_event_name,
)
from keymasq.keymasqd.runtime.grabbed_device.event.diagnostics import (
    action_diagnostic_label,
    log_mapped_action,
    passthrough_diagnostic_label,
)
from keymasq.keymasqd.runtime.grabbed_device.event.passthrough import (
    emit_passthrough_event,
)
from keymasq.keymasqd.runtime.grabbed_device.types import (
    EvdevModule,
    EventProcessingDeps,
    GrabbedDeviceRuntime,
    InputEventLike,
)


async def process_wheel_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    mapping: dict[str, MappingAction],
    *,
    recording_manager: object | None,
    deps: EventProcessingDeps,
) -> str | None:
    evdev_mod = deps.evdev_mod
    high_res_wheel_action = _find_high_res_wheel_low_res_action(
        device_runtime,
        event,
        mapping,
        evdev_mod=evdev_mod,
    )
    if (
        high_res_wheel_action is not None
        and high_res_wheel_action.action_type == ActionType.PASSTHROUGH
    ):
        _record_grabbed_event_if_allowed(
            device_runtime,
            event,
            recording_manager=recording_manager,
            deps=deps,
        )
        emit_passthrough_event(device_runtime, event, evdev_mod=evdev_mod)
        return "wheel_passthrough"
    if (
        high_res_wheel_action is not None
        and high_res_wheel_action.action_type != ActionType.PASSTHROUGH
    ):
        if not _is_recording_control_action(high_res_wheel_action):
            _record_grabbed_event_if_allowed(
                device_runtime,
                event,
                recording_manager=recording_manager,
                deps=deps,
            )
        return "wheel_high_res_suppressed"
    return await _process_wheel_pulse_event(
        device_runtime,
        event,
        event_name,
        mapping,
        recording_manager=recording_manager,
        deps=deps,
    )


async def apply_mapped_action_or_passthrough(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    mapping: dict[str, MappingAction],
    *,
    recording_manager: object | None,
    combo_consumed: bool,
    combo_passthrough_requested: bool,
    deps: EventProcessingDeps,
) -> str:
    evdev_mod = deps.evdev_mod
    action = find_action_for_code(
        device_runtime,
        int(event.type),
        int(event.code),
        int(event.value),
        event_name,
        mapping,
    )
    if event.type == evdev_mod.ecodes.EV_KEY:
        held_action = device_runtime.state.held_source_actions.get(event_name)
        if int(event.value) == 1 and event_name not in device_runtime.state.held_source_actions:
            device_runtime.state.held_source_actions[event_name] = action
        elif int(event.value) in (0, 2) and event_name in device_runtime.state.held_source_actions:
            action = held_action

    if not _is_recording_control_action(action):
        _record_grabbed_event_if_allowed(
            device_runtime,
            event,
            recording_manager=recording_manager,
            deps=deps,
        )

    log_mapped_action(
        device_runtime,
        action,
        event,
        event_name,
        evdev_mod=evdev_mod,
        log=deps.log,
    )

    if action:
        _record_profile_action_if_countable(
            device_runtime,
            action,
            event,
            event_name,
            evdev_mod=evdev_mod,
        )
        await actions.execute_action(
            device_runtime,
            action,
            event,
            event_name,
            deps=deps.action_deps,
        )
        diag_label = action_diagnostic_label(action, combo_consumed=combo_consumed)
    else:
        _mark_combo_passthrough_press(
            device_runtime,
            event,
            event_name,
            combo_passthrough_requested=combo_passthrough_requested,
            evdev_mod=evdev_mod,
        )
        emit_passthrough_event(
            device_runtime,
            event,
            evdev_mod=evdev_mod,
            event_name=event_name,
            remember_for_repeat=True,
        )
        _record_profile_input_if_countable(
            device_runtime,
            event,
            event_name,
            evdev_mod=evdev_mod,
        )
        diag_label = passthrough_diagnostic_label(
            combo_passthrough_requested=combo_passthrough_requested,
            mapped_route=True,
        )

    if event.type == evdev_mod.ecodes.EV_KEY and int(event.value) == 0:
        device_runtime.state.held_source_actions.pop(event_name, None)

    return diag_label


def apply_fast_passthrough(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    *,
    combo_passthrough_requested: bool,
    evdev_mod: EvdevModule,
) -> str:
    _mark_combo_passthrough_press(
        device_runtime,
        event,
        event_name,
        combo_passthrough_requested=combo_passthrough_requested,
        evdev_mod=evdev_mod,
    )
    emit_passthrough_event(
        device_runtime,
        event,
        evdev_mod=evdev_mod,
        event_name=event_name,
        remember_for_repeat=True,
    )
    _record_profile_input_if_countable(
        device_runtime,
        event,
        event_name,
        evdev_mod=evdev_mod,
    )
    return passthrough_diagnostic_label(
        combo_passthrough_requested=combo_passthrough_requested,
        mapped_route=False,
    )


def clear_released_source_action(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    *,
    evdev_mod: EvdevModule,
) -> None:
    if int(event.type) == int(evdev_mod.ecodes.EV_KEY) and int(event.value) == 0:
        device_runtime.state.held_source_actions.pop(event_name, None)


def _mark_combo_passthrough_press(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    *,
    combo_passthrough_requested: bool,
    evdev_mod: EvdevModule,
) -> None:
    if (
        combo_passthrough_requested
        and event.type == evdev_mod.ecodes.EV_KEY
        and int(event.value) == 1
    ):
        device_runtime.state.combo_passthrough_held.add(event_name)


def _record_profile_input_if_countable(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    *,
    evdev_mod: EvdevModule,
) -> None:
    if int(event.type) != int(evdev_mod.ecodes.EV_KEY) or int(event.value) != 1:
        return
    recorder = device_runtime.profile_activation_recorder
    if recorder is not None:
        recorder(None, source_trigger_id(device_runtime.hardware_id, event_name))


def _record_profile_action_if_countable(
    device_runtime: GrabbedDeviceRuntime,
    action: MappingAction,
    event: InputEventLike,
    event_name: str,
    *,
    evdev_mod: EvdevModule,
) -> None:
    if int(event.type) == int(evdev_mod.ecodes.EV_KEY) and int(event.value) != 1:
        return
    if int(event.type) == int(evdev_mod.ecodes.EV_REL):
        return
    recorder = device_runtime.profile_activation_recorder
    if recorder is not None:
        recorder(
            action.source_profile_name,
            source_trigger_id(device_runtime.hardware_id, event_name),
        )


def _is_recording_control_action(action: MappingAction | None) -> bool:
    return bool(
        action
        and action.action_type
        in (
            ActionType.START_MACRO_RECORDING,
            ActionType.STOP_MACRO_RECORDING,
            ActionType.PLAY_MACRO_SLOT,
            ActionType.CANCEL_MACRO_PLAYBACK,
            ActionType.EMERGENCY_RESET,
        )
    )


def _record_grabbed_event_if_allowed(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    *,
    recording_manager: object | None = None,
    deps: EventProcessingDeps,
) -> None:
    if recording_manager is None:
        recording_manager = device_runtime.recording_manager
    if not (recording_manager and bool(getattr(recording_manager, "is_recording", False))):
        return

    should_record_grabbed_event = getattr(
        recording_manager,
        "should_record_grabbed_event",
        None,
    )
    record_grabbed_event = True
    if callable(should_record_grabbed_event):
        record_grabbed_event = bool(
            should_record_grabbed_event(
                device_runtime.stable_path,
                device_runtime.device_types,
            )
        )
    if not record_grabbed_event:
        return

    input_event = cast(evdev.InputEvent, event)
    record_event = getattr(recording_manager, "record_event", None)
    if callable(record_event):
        record_event(
            deps.classify_event_device_type_fn(input_event, device_runtime.device_types),
            input_event,
        )


async def _process_wheel_pulse_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    mapping: dict[str, MappingAction],
    *,
    recording_manager: object | None,
    deps: EventProcessingDeps,
) -> str | None:
    evdev_mod = deps.evdev_mod
    if not _is_low_res_wheel_event(event, evdev_mod=evdev_mod):
        return None
    normalized_value = normalize_wheel_value(int(event.value))
    if normalized_value is None:
        return None

    action = find_action_for_code(
        device_runtime,
        int(event.type),
        int(event.code),
        int(event.value),
        event_name,
        mapping,
    )
    if action is None:
        return None

    if not _is_recording_control_action(action):
        _record_grabbed_event_if_allowed(
            device_runtime,
            event,
            recording_manager=recording_manager,
            deps=deps,
        )

    if action.action_type == ActionType.PASSTHROUGH:
        emit_passthrough_event(device_runtime, event, evdev_mod=evdev_mod)
        return "wheel_passthrough"
    if action.action_type == ActionType.SUPPRESS:
        return "action_suppress"

    button_id = find_button_id_for_code(
        device_runtime,
        int(event.type),
        int(event.code),
        int(event.value),
        mapping,
    )
    pulse_event_name = button_id or wheel_button_id(event_name, normalized_value) or event_name
    pulse_count = max(1, abs(int(event.value)))
    for _ in range(pulse_count):
        recorder = device_runtime.profile_activation_recorder
        if recorder is not None:
            recorder(
                action.source_profile_name,
                source_trigger_id(device_runtime.hardware_id, pulse_event_name),
            )
        await actions.execute_action_pulse(
            device_runtime,
            action,
            event,
            pulse_event_name,
            deps=deps.action_deps,
        )
    return f"action_{action.action_type.value}"


def _is_low_res_wheel_event(
    event: InputEventLike,
    *,
    evdev_mod: EvdevModule,
) -> bool:
    return int(event.type) == evdev_mod.ecodes.EV_REL and int(event.code) in {
        int(evdev_mod.ecodes.REL_WHEEL),
        int(evdev_mod.ecodes.REL_HWHEEL),
    }


def _find_high_res_wheel_low_res_action(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    mapping: dict[str, MappingAction],
    *,
    evdev_mod: EvdevModule,
) -> MappingAction | None:
    if int(event.type) != evdev_mod.ecodes.EV_REL:
        return None
    low_res_code = high_res_wheel_low_res_code(int(event.code))
    if low_res_code is None:
        return None
    normalized_value = normalize_wheel_value(int(event.value))
    if normalized_value is None:
        return None
    return find_action_for_code(
        device_runtime,
        int(evdev_mod.ecodes.EV_REL),
        low_res_code,
        normalized_value,
        get_event_name(
            SyntheticInputEvent(
                int(evdev_mod.ecodes.EV_REL),
                low_res_code,
                normalized_value,
            ),
            evdev_mod=evdev_mod,
        ),
        mapping,
    )
