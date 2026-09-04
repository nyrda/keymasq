"""Resolution and serialization of analog-control runtime payloads."""

from typing import TYPE_CHECKING

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.analog import (
    AnalogActionThreshold,
    AnalogControlConfig,
    normalize_analog_control_features,
)

from ..common import JsonObject

if TYPE_CHECKING:
    from ..core import SessionManager


def resolve(
    manager: "SessionManager",
    action: MappingAction,
) -> list[AnalogControlConfig]:
    """Resolve inline and named analog controls in their configured order."""
    configs: list[AnalogControlConfig] = []
    if action.analog_control_configs:
        configs.extend(action.analog_control_configs)
    elif action.analog_control_config is not None:
        configs.append(action.analog_control_config)

    names = action.analog_control_names
    if not names and action.analog_control_name:
        names = [action.analog_control_name]
    if not names:
        return configs
    analog_controls = getattr(manager, "analog_controls", None)
    get_analog_control = getattr(analog_controls, "get_analog_control", None)
    if not callable(get_analog_control):
        return configs
    for name in names:
        config = get_analog_control(name)
        if isinstance(config, AnalogControlConfig):
            configs.append(config)
    return configs


def serialize(
    manager: "SessionManager",
    config: AnalogControlConfig,
    hardware_id: str,
) -> JsonObject:
    return _serialize_control(manager, config, hardware_id, signature=False)


def serialize_signature(
    manager: "SessionManager",
    config: AnalogControlConfig,
    hardware_id: str,
) -> JsonObject:
    return _serialize_control(manager, config, hardware_id, signature=True)


def _serialize_control(
    manager: "SessionManager",
    config: AnalogControlConfig,
    hardware_id: str,
    *,
    signature: bool,
) -> JsonObject:
    config = normalize_analog_control_features(config)
    return {
        "name": config.name,
        "input_type": config.input_type,
        "mouse_motion": {
            "enabled": bool(config.mouse_motion.enabled),
            "mode": config.mouse_motion.mode,
            "speed": float(config.mouse_motion.speed),
            "speed_x": float(
                config.mouse_motion.speed_x
                if config.mouse_motion.speed_x is not None
                else config.mouse_motion.speed
            ),
            "speed_y": float(
                config.mouse_motion.speed_y
                if config.mouse_motion.speed_y is not None
                else config.mouse_motion.speed
            ),
            "area_radius_x": float(config.mouse_motion.area_radius_x),
            "area_radius_y": float(config.mouse_motion.area_radius_y),
            "area_start_enabled": bool(config.mouse_motion.area_start_enabled),
            "area_start_x": int(config.mouse_motion.area_start_x),
            "area_start_y": int(config.mouse_motion.area_start_y),
            "deadzone": float(config.mouse_motion.deadzone),
            "sensitivity": float(config.mouse_motion.sensitivity),
            "response_curve": float(config.mouse_motion.response_curve),
            "direction": config.mouse_motion.direction,
            "invert_x": bool(config.mouse_motion.invert_x),
            "invert_y": bool(config.mouse_motion.invert_y),
            "tick_ms": int(config.mouse_motion.tick_ms),
        },
        "gamepad_output": {
            "enabled": bool(config.gamepad_output.enabled),
            "output_id": config.gamepad_output.output_id,
            "deadzone": float(config.gamepad_output.deadzone),
            "target": config.gamepad_output.target,
            "target_analog_id": config.gamepad_output.target_analog_id,
            "output_rest": config.gamepad_output.output_rest,
            "output_direction": config.gamepad_output.output_direction,
            "output_invert": bool(config.gamepad_output.output_invert),
            "output_invert_x": bool(config.gamepad_output.output_invert_x),
            "output_invert_y": bool(config.gamepad_output.output_invert_y),
            "sensitivity": float(config.gamepad_output.sensitivity),
            "response_curve": float(config.gamepad_output.response_curve),
        },
        "thresholds": [
            _serialize_threshold(
                manager,
                threshold,
                hardware_id,
                signature=signature,
            )
            for threshold in config.thresholds
        ],
    }


def _serialize_threshold(
    manager: "SessionManager",
    threshold: AnalogActionThreshold,
    hardware_id: str,
    *,
    signature: bool,
) -> JsonObject:
    from .action import action_signature_payload, serialize_overload_action

    return {
        "axis": threshold.axis,
        "trigger_min": float(threshold.trigger_min),
        "trigger_max": float(threshold.trigger_max),
        "release_min": float(threshold.release_min),
        "release_max": float(threshold.release_max),
        "actions": [
            action_signature_payload(manager, action, hardware_id)
            if signature
            else serialize_overload_action(manager, action, hardware_id)
            for action in threshold.actions
        ],
    }
