"""Resolution and runtime serialization for motion controls."""

from typing import TYPE_CHECKING

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.motion import MotionControlConfig

from ..common import JsonObject

if TYPE_CHECKING:
    from ..core import SessionManager


def resolve(manager: "SessionManager", action: MappingAction) -> MotionControlConfig | None:
    if action.motion_control_config is not None:
        return action.motion_control_config
    if action.motion_control_name:
        return manager.motion_controls.get_motion_control(action.motion_control_name)
    return None


def serialize(config: MotionControlConfig) -> JsonObject:
    return {
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
            "area_radius_x": config.tilt.area_radius_x,
            "area_radius_y": config.tilt.area_radius_y,
            "drag_center": config.tilt.drag_center,
        },
    }
