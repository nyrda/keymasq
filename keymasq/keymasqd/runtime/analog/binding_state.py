"""Analog-control configuration fan-out and normalized input state."""

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.analog import AnalogControlConfig
from keymasq.common.model.core import ActionType
from keymasq.keymasqd.runtime.analog.curves import (
    DEFAULT_STICK_MAX,
    DEFAULT_STICK_MIN,
    DEFAULT_TRIGGER_MAX,
    DEFAULT_TRIGGER_MIN,
    normalize_axis_value,
    normalize_control_axis_value,
)
from keymasq.keymasqd.runtime.grabbed_device.types import GrabbedDeviceRuntime


def action_analog_control_configs(action: MappingAction | None) -> list[AnalogControlConfig]:
    if action is None or action.action_type != ActionType.ANALOG_CONTROL:
        return []
    if action.analog_control_configs:
        return list(action.analog_control_configs)
    if action.analog_control_config is not None:
        return [action.analog_control_config]
    return []


def control_state_key(source_id: str, index: int, total: int) -> str:
    return source_id if total == 1 else f"{source_id}#analog_control#{index}"


def analog_state_keys_for_action(
    source_id: str,
    action: MappingAction | None,
) -> set[str]:
    configs = action_analog_control_configs(action)
    return {
        control_state_key(source_id, index, len(configs)) for index, _config in enumerate(configs)
    }


def preserved_analog_state_keys(
    old_mapping: dict[str, MappingAction],
    new_mapping: dict[str, MappingAction],
) -> set[str]:
    preserved: set[str] = set()
    for source_id, old_action in old_mapping.items():
        new_action = new_mapping.get(source_id)
        if not same_analog_control_configs(old_action, new_action):
            continue
        preserved.update(analog_state_keys_for_action(source_id, new_action))
    return preserved


def same_analog_control_configs(
    old_action: MappingAction | None,
    new_action: MappingAction | None,
) -> bool:
    old_configs = action_analog_control_configs(old_action)
    if not old_configs:
        return False
    return old_configs == action_analog_control_configs(new_action)


def record_axis_value(
    device_runtime: GrabbedDeviceRuntime,
    state_key: str,
    analog_id: str,
    axis_role: str,
    raw_value: int,
    config: AnalogControlConfig,
) -> None:
    fallback_range = (
        (DEFAULT_TRIGGER_MIN, DEFAULT_TRIGGER_MAX)
        if config.input_type == "axis"
        else (DEFAULT_STICK_MIN, DEFAULT_STICK_MAX)
    )
    minimum, maximum = device_runtime.analog_axis_ranges.get(
        (analog_id, axis_role),
        fallback_range,
    )
    calibration = device_runtime.analog_axis_calibrations.get((analog_id, axis_role), {})
    rest_value = calibration.get("rest")
    center_value = calibration.get("center")
    if config.input_type == "axis":
        rest = rest_value if isinstance(rest_value, int) else (minimum if minimum >= 0 else 0)
        normalized = normalize_control_axis_value(raw_value, minimum, maximum, rest=rest)
        signed_normalized = normalize_axis_value(raw_value, minimum, maximum, center=rest)
    else:
        normalized = normalize_axis_value(
            raw_value,
            minimum,
            maximum,
            center=center_value if isinstance(center_value, int) else None,
            invert=bool(calibration.get("invert", False)),
        )
        signed_normalized = normalized
    axis_values = device_runtime.state.analog_axis_values.setdefault(state_key, {})
    axis_values[axis_role] = normalized
    axis_values[f"{axis_role}_signed"] = signed_normalized
