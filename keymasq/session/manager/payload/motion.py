"""Resolution and runtime serialization for motion controls."""

from typing import TYPE_CHECKING

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.analog import AnalogControlConfig
from keymasq.common.model.motion import MotionControlConfig

from ..common import JsonObject
from . import analog

if TYPE_CHECKING:
    from ..core import SessionManager


def resolve(manager: "SessionManager", action: MappingAction) -> list[MotionControlConfig]:
    configs: list[MotionControlConfig] = []
    if action.motion_control_configs:
        configs.extend(action.motion_control_configs)
    elif action.motion_control_config is not None:
        configs.append(action.motion_control_config)

    names = action.motion_control_names
    if not names and action.motion_control_name:
        names = [action.motion_control_name]
    for name in names:
        config = manager.motion_controls.get_motion_control(name)
        if config is not None:
            configs.append(config)
    return configs


def serialize(
    manager: "SessionManager",
    config: MotionControlConfig,
    hardware_id: str,
    *,
    signature: bool,
) -> JsonObject:
    analog_control = _resolve_analog_control(manager, config)
    data: JsonObject = {
        "name": config.name,
        "mode": config.mode,
        "axis_routing": {
            "yaw": config.axis_routing.yaw,
            "pitch": config.axis_routing.pitch,
            "roll": config.axis_routing.roll,
        },
        "mouse": {
            "sensitivity_x": config.mouse.sensitivity_x,
            "sensitivity_y": config.mouse.sensitivity_y,
            "deadzone_dps": config.mouse.deadzone_dps,
            "smoothing": config.mouse.smoothing,
            "response_curve": config.mouse.response_curve,
            "invert_x": config.mouse.invert_x,
            "invert_y": config.mouse.invert_y,
        },
        "gamepad": {
            "output_id": config.gamepad.output_id,
            "target": config.gamepad.target,
            "target_analog_id": config.gamepad.target_analog_id,
            "max_rate_dps": config.gamepad.max_rate_dps,
            "minimum_output": config.gamepad.minimum_output,
            "deadzone_dps": config.gamepad.deadzone_dps,
            "smoothing": config.gamepad.smoothing,
            "response_curve": config.gamepad.response_curve,
            "invert_x": config.gamepad.invert_x,
            "invert_y": config.gamepad.invert_y,
        },
        "tilt": {
            "reference": config.tilt.reference,
            "pitch": config.tilt.pitch,
            "roll": config.tilt.roll,
            "deadzone_deg": config.tilt.deadzone_deg,
            "full_scale_deg": config.tilt.full_scale_deg,
            "smoothing": config.tilt.smoothing,
            "response_curve": config.tilt.response_curve,
            "invert_x": config.tilt.invert_x,
            "invert_y": config.tilt.invert_y,
            "speed_x": config.tilt.speed_x,
            "speed_y": config.tilt.speed_y,
        },
        "analog": {
            "analog_control_name": config.analog.analog_control_name,
            "source": config.analog.source,
            "x_axis": config.analog.x_axis,
            "y_axis": config.analog.y_axis,
            "reference": config.analog.reference,
            "full_scale_dps": config.analog.full_scale_dps,
            "full_scale_deg": config.analog.full_scale_deg,
            "smoothing": config.analog.smoothing,
            "invert_x": config.analog.invert_x,
            "invert_y": config.analog.invert_y,
        },
    }
    if analog_control is not None:
        analog_data = data["analog"]
        if isinstance(analog_data, dict):
            serializer = analog.serialize_signature if signature else analog.serialize
            analog_data["analog_control"] = serializer(
                manager,
                analog_control,
                hardware_id,
            )
    return data


def _resolve_analog_control(
    manager: "SessionManager",
    config: MotionControlConfig,
) -> AnalogControlConfig | None:
    if config.analog.analog_control_config is not None:
        return config.analog.analog_control_config
    if config.analog.analog_control_name:
        return manager.analog_controls.get_analog_control(config.analog.analog_control_name)
    return None
