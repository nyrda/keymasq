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
from keymasq.keymasqd.runtime.analog.mouse import emit_mouse_area_motion, ensure_mouse_task
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
        if not await emit_mouse_area_motion(
            device_runtime,
            state_key,
            config,
            deps=deps,
        ):
            ensure_mouse_task(device_runtime, state_key, config, deps=deps)
