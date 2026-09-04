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
    state_key_prefix: str | None = None,
) -> None:
    """Release all analog state, or only tracked state matching a prefix."""

    preserved = set(preserve_state_keys or ())
    state_configs: dict[str, tuple[str, AnalogControlConfig]] = {}
    if state_key_prefix is None:
        for source_id, action in device_runtime.mapping_getter().items():
            configs = action_analog_control_configs(action)
            for index, config in enumerate(configs):
                state_configs[control_state_key(source_id, index, len(configs))] = (
                    source_id,
                    config,
                )

    reset_recorded_gamepad_outputs(
        device_runtime,
        deps=deps,
        preserved=preserved,
        state_key_prefix=state_key_prefix,
    )
    for state_key, active in list(device_runtime.state.analog_active_thresholds.items()):
        if not _should_reset_state_key(state_key, preserved, state_key_prefix):
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
        if not _should_reset_state_key(state_key, preserved, state_key_prefix):
            continue
        if analog_control_primary_mode(config) == "gamepad":
            reset_gamepad_output(
                device_runtime,
                state_key,
                source_id,
                config,
                deps=deps,
            )

    await cancel_mouse_tasks(
        device_runtime,
        preserve_state_keys=preserved,
        state_key_prefix=state_key_prefix,
    )

    _discard_reset_state_keys(
        device_runtime.state.analog_axis_values,
        preserved,
        state_key_prefix,
    )
    _discard_reset_state_keys(
        device_runtime.state.analog_active_thresholds,
        preserved,
        state_key_prefix,
    )
    for key in list(device_runtime.state.analog_active_threshold_actions):
        if _should_reset_state_key(
            threshold_source_key(key),
            preserved,
            state_key_prefix,
        ):
            device_runtime.state.analog_active_threshold_actions.pop(key, None)
    _discard_reset_state_keys(
        device_runtime.state.analog_mouse_tasks,
        preserved,
        state_key_prefix,
    )
    _discard_reset_state_keys(
        device_runtime.state.analog_mouse_accumulators,
        preserved,
        state_key_prefix,
    )
    _discard_reset_state_keys(
        device_runtime.state.analog_mouse_area_offsets,
        preserved,
        state_key_prefix,
    )
    for state_key in list(device_runtime.state.analog_mouse_area_active):
        if _should_reset_state_key(state_key, preserved, state_key_prefix):
            device_runtime.state.analog_mouse_area_active.discard(state_key)
    _discard_reset_state_keys(
        device_runtime.state.analog_gamepad_outputs,
        preserved,
        state_key_prefix,
    )


def _should_reset_state_key(
    state_key: str,
    preserved: set[str],
    state_key_prefix: str | None,
) -> bool:
    return state_key not in preserved and (
        state_key_prefix is None or state_key.startswith(state_key_prefix)
    )


def _discard_reset_state_keys[StateValue](
    mapping: dict[str, StateValue],
    preserved: set[str],
    state_key_prefix: str | None,
) -> None:
    for state_key in list(mapping):
        if _should_reset_state_key(state_key, preserved, state_key_prefix):
            mapping.pop(state_key, None)
