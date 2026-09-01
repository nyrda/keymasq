import copy
from collections.abc import Iterable
from dataclasses import dataclass

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.common.model.profiles import ProfileConfig

from .types import ProfileInfo


@dataclass(frozen=True)
class Rewrite:
    config: ProfileConfig | None
    count: int = 0


def find_superkey(profiles: Iterable[ProfileInfo], name: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for info in profiles:
        for hardware_id, layer in info.config.device_layers.items():
            if any(
                action.action_type == ActionType.SUPERKEY and action.superkey_name == name
                for action in layer.mappings.values()
            ):
                result.append((hardware_id, info.config.name))
        if any(
            combo.action is not None
            and combo.action.action_type == ActionType.SUPERKEY
            and combo.action.superkey_name == name
            for combo in info.config.combos
        ):
            result.append(("combo", info.config.name))
    return result


def remove_analog_control(config: ProfileConfig, name: str) -> Rewrite:
    updated = copy.deepcopy(config)
    count = 0
    for layer in updated.device_layers.values():
        for button_id, action in list(layer.mappings.items()):
            if action.action_type != ActionType.ANALOG_CONTROL:
                continue
            if name not in action.analog_control_names:
                continue
            remaining_names = [
                control_name for control_name in action.analog_control_names if control_name != name
            ]
            layer.mappings[button_id] = (
                MappingAction(
                    action_type=ActionType.ANALOG_CONTROL,
                    analog_control_names=remaining_names,
                )
                if remaining_names
                else MappingAction(action_type=ActionType.SUPPRESS)
            )
            count += 1
    return Rewrite(updated if count else None, count)


def rename_analog_control(config: ProfileConfig, old_name: str, new_name: str) -> Rewrite:
    if old_name == new_name:
        return Rewrite(None)
    updated = copy.deepcopy(config)
    count = 0
    for layer in updated.device_layers.values():
        for action in layer.mappings.values():
            if action.action_type != ActionType.ANALOG_CONTROL:
                continue
            if old_name not in action.analog_control_names:
                continue
            action.analog_control_names = [
                new_name if name == old_name else name for name in action.analog_control_names
            ]
            action.analog_control_name = action.analog_control_names[0]
            count += 1
    return Rewrite(updated if count else None, count)


def remove_motion_control(config: ProfileConfig, name: str) -> Rewrite:
    updated = copy.deepcopy(config)
    count = 0
    for layer in updated.device_layers.values():
        for source_id, action in list(layer.mappings.items()):
            if action.action_type != ActionType.MOTION_CONTROL:
                continue
            if name not in action.motion_control_names:
                continue
            remaining_names = [
                control_name for control_name in action.motion_control_names if control_name != name
            ]
            layer.mappings[source_id] = (
                MappingAction(
                    action_type=ActionType.MOTION_CONTROL,
                    motion_control_names=remaining_names,
                )
                if remaining_names
                else MappingAction(action_type=ActionType.SUPPRESS)
            )
            count += 1
    return Rewrite(updated if count else None, count)


def rename_motion_control(config: ProfileConfig, old_name: str, new_name: str) -> Rewrite:
    if old_name == new_name:
        return Rewrite(None)
    updated = copy.deepcopy(config)
    count = 0
    for layer in updated.device_layers.values():
        for action in layer.mappings.values():
            if action.action_type != ActionType.MOTION_CONTROL:
                continue
            if old_name not in action.motion_control_names:
                continue
            action.motion_control_names = [
                new_name if name == old_name else name for name in action.motion_control_names
            ]
            action.motion_control_name = action.motion_control_names[0]
            count += 1
    return Rewrite(updated if count else None, count)


def remove_superkey(config: ProfileConfig, name: str) -> Rewrite:
    updated = copy.deepcopy(config)
    count = 0
    for layer in updated.device_layers.values():
        for button_id, action in list(layer.mappings.items()):
            if action.action_type == ActionType.SUPERKEY and action.superkey_name == name:
                layer.mappings[button_id] = MappingAction(action_type=ActionType.SUPPRESS)
                count += 1
    for combo in updated.combos:
        action = combo.action
        if (
            action is not None
            and action.action_type == ActionType.SUPERKEY
            and action.superkey_name == name
        ):
            combo.action = MappingAction(action_type=ActionType.SUPPRESS)
            count += 1
    return Rewrite(updated if count else None, count)


def rename_superkey(config: ProfileConfig, old_name: str, new_name: str) -> Rewrite:
    if old_name == new_name:
        return Rewrite(None)
    updated = copy.deepcopy(config)
    count = 0
    for layer in updated.device_layers.values():
        for action in layer.mappings.values():
            if action.action_type == ActionType.SUPERKEY and action.superkey_name == old_name:
                action.superkey_name = new_name
                count += 1
    for combo in updated.combos:
        action = combo.action
        if (
            action is not None
            and action.action_type == ActionType.SUPERKEY
            and action.superkey_name == old_name
        ):
            action.superkey_name = new_name
            count += 1
    return Rewrite(updated if count else None, count)


def remove_device_layer(config: ProfileConfig, hardware_id: str) -> Rewrite:
    if hardware_id not in config.device_layers:
        return Rewrite(None)
    updated = copy.deepcopy(config)
    updated.device_layers.pop(hardware_id, None)
    return Rewrite(updated, 1)


def remove_button_mapping(
    config: ProfileConfig,
    hardware_id: str,
    button_id: str,
) -> Rewrite:
    layer = config.get_layer(hardware_id)
    if layer is None or button_id not in layer.mappings:
        return Rewrite(None)
    updated = copy.deepcopy(config)
    updated_layer = updated.get_layer(hardware_id)
    if updated_layer is not None:
        updated_layer.mappings.pop(button_id, None)
    return Rewrite(updated, 1)
