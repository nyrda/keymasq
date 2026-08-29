"""Release transaction for tracked analog-control runtime state."""

from keymasq.common.model.analog import AnalogControlConfig, analog_control_primary_mode
from keymasq.keymasqd.runtime.analog.binding_state import (
    action_analog_control_configs,
    control_state_key,
)
from keymasq.keymasqd.runtime.analog.gamepad import reset_gamepad_output
from keymasq.keymasqd.runtime.analog.mouse import cancel_mouse_tasks
from keymasq.keymasqd.runtime.analog.output_state import (
    reset_recorded_gamepad_outputs,
)
from keymasq.keymasqd.runtime.analog.threshold_state import (
    threshold_index,
    threshold_source_key,
)
from keymasq.keymasqd.runtime.analog.thresholds import release_threshold_actions
from keymasq.keymasqd.runtime.grabbed_device.types import (
    ActionExecutionDeps,
    GrabbedDeviceRuntime,
)


async def reset_analog_controls(
    device_runtime: GrabbedDeviceRuntime,
    *,
    deps: ActionExecutionDeps,
    preserve_state_keys: set[str] | None = None,
    release_threshold_transitions: bool = True,
) -> None:
    preserved = set(preserve_state_keys or ())
    mapping = device_runtime.mapping_getter()
    state_configs: dict[str, tuple[str, AnalogControlConfig]] = {}
    for source_id, action in mapping.items():
        configs = action_analog_control_configs(action)
        for index, config in enumerate(configs):
            state_configs[control_state_key(source_id, index, len(configs))] = (
                source_id,
                config,
            )

    reset_recorded_gamepad_outputs(device_runtime, deps=deps, preserved=preserved)
    if release_threshold_transitions:
        for state_key, active in list(device_runtime.state.analog_active_thresholds.items()):
            if state_key in preserved:
                continue
            state_config = state_configs.get(state_key)
            config = state_config[1] if state_config is not None else None
            for key in list(active):
                index = threshold_index(key)
                actions = device_runtime.state.analog_active_threshold_actions.get(key)
                threshold = (
                    config.thresholds[index]
                    if config is not None and index is not None and index < len(config.thresholds)
                    else None
                )
                if index is None or (threshold is None and actions is None):
                    continue
                await release_threshold_actions(
                    device_runtime,
                    state_key,
                    index,
                    threshold,
                    deps=deps,
                    active_actions=actions,
                )

    for state_key, (source_id, config) in state_configs.items():
        if state_key in preserved:
            continue
        if analog_control_primary_mode(config) == "gamepad":
            reset_gamepad_output(
                device_runtime,
                state_key,
                source_id,
                config,
                deps=deps,
            )

    await cancel_mouse_tasks(device_runtime, preserve_state_keys=preserved)

    _discard_unpreserved_keys(device_runtime.state.analog_axis_values, preserved)
    _discard_unpreserved_keys(device_runtime.state.analog_active_thresholds, preserved)
    for key in list(device_runtime.state.analog_active_threshold_actions):
        if threshold_source_key(key) not in preserved:
            device_runtime.state.analog_active_threshold_actions.pop(key, None)
    _discard_unpreserved_keys(device_runtime.state.analog_mouse_tasks, preserved)
    _discard_unpreserved_keys(device_runtime.state.analog_mouse_accumulators, preserved)
    _discard_unpreserved_keys(device_runtime.state.analog_mouse_area_offsets, preserved)
    device_runtime.state.analog_mouse_area_active.intersection_update(preserved)
    _discard_unpreserved_keys(device_runtime.state.analog_gamepad_outputs, preserved)


def _discard_unpreserved_keys[StateValue](
    mapping: dict[str, StateValue],
    preserved: set[str],
) -> None:
    for state_key in list(mapping):
        if state_key not in preserved:
            mapping.pop(state_key, None)
