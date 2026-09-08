"""Coordinate analog input events across focused runtime components."""

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.analog import AnalogControlConfig, analog_control_primary_mode
from keymasq.common.model.core import ActionType
from keymasq.keymasqd.runtime.analog.binding_state import (
    action_analog_control_configs,
    control_state_key,
    record_axis_value,
)
from keymasq.keymasqd.runtime.analog.gamepad import emit_gamepad_output
from keymasq.keymasqd.runtime.analog.mouse import (
    emit_mouse_area_motion,
    emit_mouse_touchpad_motion,
    ensure_mouse_task,
)
from keymasq.keymasqd.runtime.analog.source_state import read_missing_source_axes
from keymasq.keymasqd.runtime.analog.thresholds import evaluate_thresholds
from keymasq.keymasqd.runtime.grabbed_device.types import (
    ActionExecutionDeps,
    GrabbedDeviceRuntime,
    InputEventLike,
)


async def process_analog_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    event_name: str,
    mapping: dict[str, MappingAction],
    *,
    deps: ActionExecutionDeps,
) -> bool:
    del event_name  # Analog child actions have stable names derived from their bindings.
    binding = device_runtime.analog_axis_bindings.get((int(event.type), int(event.code)))
    if binding is None:
        return False

    analog_id, axis_role = binding
    action = mapping.get(analog_id)
    if action is None or action.action_type == ActionType.PASSTHROUGH:
        return False
    if action.action_type == ActionType.SUPPRESS:
        return True
    if action.action_type != ActionType.ANALOG_CONTROL:
        return True

    configs = action_analog_control_configs(action)
    if not configs:
        return False

    for index, config in enumerate(configs):
        state_key = control_state_key(analog_id, index, len(configs))
        mode = analog_control_primary_mode(config)
        touchpad = mode == "mouse" and config.mouse_motion.mode == "touchpad"
        if touchpad and device_runtime.state.analog_mouse_touchpad_resyncing:
            continue
        if touchpad:
            device_runtime.state.analog_mouse_touchpad_pending[state_key] = (analog_id, config)
            continue
        record_axis_value(
            device_runtime,
            state_key,
            analog_id,
            axis_role,
            int(event.value),
            config,
        )
        await _process_analog_values(
            device_runtime,
            state_key,
            analog_id,
            config,
            event,
            mode=mode,
            source_profile_name=action.source_profile_name,
            deps=deps,
        )
    return True


async def observe_analog_source_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    *,
    deps: ActionExecutionDeps,
) -> None:
    """Track physical state even while mappings or input dispatch are suppressed."""
    state = device_runtime.state
    binding = (int(event.type), int(event.code))
    if binding in device_runtime.analog_axis_bindings:
        if not state.analog_mouse_touchpad_resyncing:
            state.analog_source_axis_values[binding] = int(event.value)
        return
    if int(event.type) != int(deps.evdev_mod.ecodes.EV_SYN):
        return
    if int(event.code) == int(deps.evdev_mod.ecodes.SYN_DROPPED):
        affected = (
            state.analog_mouse_touchpad_positions.keys()
            | state.analog_mouse_touchpad_pending.keys()
        )
        state.analog_mouse_touchpad_needs_release.update(affected)
        for state_key in affected:
            state.analog_mouse_accumulators.pop(state_key, None)
        state.analog_mouse_touchpad_positions.clear()
        state.analog_mouse_touchpad_pending.clear()
        state.analog_source_axis_values.clear()
        state.analog_mouse_touchpad_resyncing = True
        return
    if int(event.code) != 0 or not state.analog_mouse_touchpad_resyncing:  # SYN_REPORT
        return
    # The report following SYN_DROPPED is incomplete. Rebuild from EVIOCGABS
    # instead, since unchanged coordinates may never appear in later reports.
    if not await read_missing_source_axes(device_runtime, deps=deps):
        return
    state.analog_mouse_touchpad_resyncing = False
    for analog_id, action in device_runtime.mapping_getter().items():
        configs = action_analog_control_configs(action)
        for index, config in enumerate(configs):
            state_key = control_state_key(analog_id, index, len(configs))
            if state_key not in state.analog_mouse_touchpad_needs_release:
                continue
            _record_touchpad_position(device_runtime, state_key, analog_id, config)
            # A known release in the snapshot rearms the control. A held touch
            # remains gated; this cannot emit movement during suppressed input.
            values = state.analog_axis_values[state_key]
            if values.get("x") == 0.0 and values.get("y") == 0.0:
                state.analog_mouse_touchpad_needs_release.discard(state_key)


def _record_touchpad_position(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    analog_id: str,
    config: AnalogControlConfig,
) -> None:
    device_runtime.state.analog_axis_values[state_key] = {}
    for binding, (source_id, axis_role) in device_runtime.analog_axis_bindings.items():
        raw_value = device_runtime.state.analog_source_axis_values.get(binding)
        if source_id == analog_id and raw_value is not None:
            record_axis_value(device_runtime, state_key, analog_id, axis_role, raw_value, config)


async def process_analog_syn_event(
    device_runtime: GrabbedDeviceRuntime,
    event: InputEventLike,
    *,
    deps: ActionExecutionDeps,
) -> None:
    """Flush touchpad motion using complete coordinates at the report boundary."""
    state = device_runtime.state
    if int(event.code) != 0 or state.analog_mouse_touchpad_resyncing:  # SYN_REPORT
        return
    if not state.analog_mouse_touchpad_pending:
        return
    if not await read_missing_source_axes(device_runtime, deps=deps):
        return
    pending = list(state.analog_mouse_touchpad_pending.items())
    state.analog_mouse_touchpad_pending.clear()
    for state_key, (analog_id, config) in pending:
        _record_touchpad_position(device_runtime, state_key, analog_id, config)
        await emit_mouse_touchpad_motion(device_runtime, state_key, config, deps=deps)


async def process_normalized_analog_values(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    source_id: str,
    config: AnalogControlConfig,
    event: InputEventLike,
    x: float,
    y: float,
    *,
    source_profile_name: str | None,
    deps: ActionExecutionDeps,
) -> None:
    """Run an Analog Control from normalized values supplied by a non-evdev source."""
    x = max(-1.0, min(1.0, float(x)))
    y = max(-1.0, min(1.0, float(y)))
    if config.input_type == "axis":
        device_runtime.state.analog_axis_values[state_key] = {
            "x": abs(x),
            "x_signed": x,
        }
    else:
        device_runtime.state.analog_axis_values[state_key] = {
            "x": x,
            "x_signed": x,
            "y": y,
            "y_signed": y,
        }
    await _process_analog_values(
        device_runtime,
        state_key,
        source_id,
        config,
        event,
        mode=analog_control_primary_mode(config),
        source_profile_name=source_profile_name,
        deps=deps,
    )


async def _process_analog_values(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    source_id: str,
    config: AnalogControlConfig,
    event: InputEventLike,
    *,
    mode: str,
    source_profile_name: str | None,
    deps: ActionExecutionDeps,
) -> None:
    if mode == "digital":
        await evaluate_thresholds(
            device_runtime,
            state_key,
            config,
            event,
            source_profile_name=source_profile_name,
            deps=deps,
        )
    elif mode == "gamepad":
        emit_gamepad_output(
            device_runtime,
            state_key,
            source_id,
            config,
            deps=deps,
        )
    elif mode == "mouse":
        if config.mouse_motion.mode == "touchpad" and config.input_type == "stick":
            await emit_mouse_touchpad_motion(device_runtime, state_key, config, deps=deps)
        elif not await emit_mouse_area_motion(
            device_runtime,
            state_key,
            config,
            deps=deps,
        ):
            ensure_mouse_task(device_runtime, state_key, config, deps=deps)
