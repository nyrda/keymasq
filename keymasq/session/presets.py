import copy
import logging
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from keymasq.common import paths
from keymasq.common.model.actions import MappingAction
from keymasq.common.model.analog import AnalogControlConfig
from keymasq.common.model.core import ActionType
from keymasq.common.model.hardware import HardwareConfig
from keymasq.common.model.profiles import DeviceProfileLayer, ProfileConfig
from keymasq.session.action_toml import mapping_action_type_from_toml
from keymasq.session.analog_controls import AnalogControlManager
from keymasq.session.config_loading import load_config_files_sync
from keymasq.session.profile.codec import ProfileCodec
from keymasq.session.profile.manager import ProfileManager

log = logging.getLogger("keymasq-session.presets")
type TomlDict = dict[str, object]

_SAVED_REFERENCE_ACTIONS = {
    ActionType.SUPERKEY: "superkey",
    ActionType.MOTION_CONTROL: "motion control",
    ActionType.MACRO: "macro",
}
_ANALOG_INPUT_ACTIONS = {ActionType.ANALOG_CONTROL, ActionType.SUPPRESS}
_BUTTON_EXCLUDED_ACTIONS = {
    ActionType.ANALOG_CONTROL,
    ActionType.MOTION_CONTROL,
    ActionType.POINTER_MOVEMENT,
}


@dataclass(frozen=True)
class ProfilePreset:
    preset_id: str
    name: str
    path: Path
    description: str = ""
    device_types: tuple[str, ...] = ()
    hardware_models: tuple[str, ...] = ()
    mappings: dict[str, MappingAction] = field(default_factory=dict[str, MappingAction])
    analog_controls: dict[str, AnalogControlConfig] = field(
        default_factory=dict[str, AnalogControlConfig]
    )

    def applies_to(self, hardware: HardwareConfig) -> bool:
        if self.hardware_models and hardware.model_id.lower() not in self.hardware_models:
            return False
        if not self.device_types:
            return True
        hardware_types = {device.device_type.value for device in hardware.evdev_devices}
        return not hardware_types.isdisjoint(self.device_types)


@dataclass
class PresetPlan:
    mappings: dict[str, MappingAction] = field(default_factory=dict[str, MappingAction])
    kept: list[str] = field(default_factory=list[str])
    unavailable: list[str] = field(default_factory=list[str])
    new_analog_controls: list[AnalogControlConfig] = field(
        default_factory=list[AnalogControlConfig]
    )


def _table(value: object, context: str) -> TomlDict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be a TOML table")
    return cast(TomlDict, value)


def _string_tuple(value: object, context: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{context} must be a list of strings")
    items = [
        item.strip().lower() if isinstance(item, str) else ""
        for item in cast(list[object], value)
    ]
    if not all(items):
        raise ValueError(f"{context} must be a list of non-empty strings")
    return tuple(items)


def preset_from_toml(data: TomlDict, *, preset_id: str, path: Path) -> ProfilePreset:
    header = _table(data.get("preset"), "preset")

    analog_controls: dict[str, AnalogControlConfig] = {}
    for control_name, control_data in _table(
        data.get("analog_controls"), "analog_controls"
    ).items():
        control_table = _table(control_data, f"analog control '{control_name}'")
        control = AnalogControlManager.parse_analog_control(
            {**control_table, "name": control_name},
            default_name=control_name,
        )
        for threshold in control.thresholds:
            for action in threshold.actions:
                if action.action_type in _SAVED_REFERENCE_ACTIONS:
                    raise ValueError(
                        f"analog control '{control_name}': presets cannot reference a saved "
                        f"{_SAVED_REFERENCE_ACTIONS[action.action_type]}"
                    )
        analog_controls[control_name] = control

    codec = ProfileCodec()
    mappings: dict[str, MappingAction] = {}
    for source_id, action_data in _table(data.get("mapping"), "mapping").items():
        action_table = _table(action_data, f"mapping '{source_id}'")
        if "action" not in action_table:
            raise ValueError(f"mapping '{source_id}': action is missing")
        try:
            action_type, _ = mapping_action_type_from_toml(action_table, unknown_action="raise")
        except ValueError as exc:
            raise ValueError(f"mapping '{source_id}': {exc}") from exc
        if action_type in _SAVED_REFERENCE_ACTIONS:
            raise ValueError(
                f"mapping '{source_id}': presets cannot reference a saved "
                f"{_SAVED_REFERENCE_ACTIONS[action_type]}"
            )
        action = codec.parse_action(action_table)
        if action_type == ActionType.ANALOG_CONTROL and not _analog_control_names(action):
            raise ValueError(f"mapping '{source_id}': analog_control_name is missing")
        mappings[source_id] = action
    if not mappings:
        raise ValueError("preset has no mappings")

    name = header.get("name")
    description = header.get("description")
    return ProfilePreset(
        preset_id=preset_id,
        name=name.strip() if isinstance(name, str) and name.strip() else preset_id,
        path=path,
        description=description.strip() if isinstance(description, str) else "",
        device_types=_string_tuple(header.get("device_types"), "preset.device_types"),
        hardware_models=_string_tuple(header.get("hardware"), "preset.hardware"),
        mappings=mappings,
        analog_controls=analog_controls,
    )


def _load_preset_file(path: Path) -> ProfilePreset:
    with open(path, "rb") as preset_file:
        data = cast(TomlDict, tomllib.load(preset_file))
    return preset_from_toml(data, preset_id=path.stem, path=path)


def load_presets(search_dirs: Sequence[Path] | None = None) -> list[ProfilePreset]:
    presets: dict[str, ProfilePreset] = {}
    for preset_dir in paths.preset_search_dirs() if search_dirs is None else search_dirs:
        for _, preset in load_config_files_sync(
            preset_dir,
            config_kind="preset",
            strict=False,
            load_config=_load_preset_file,
            logger=log,
            failure_log_message="Failed to load preset %s: %s",
            sort_paths=True,
        ):
            presets[preset.preset_id] = preset
    return sorted(presets.values(), key=lambda preset: (preset.name.lower(), preset.preset_id))


def presets_for_hardware(
    presets: Sequence[ProfilePreset],
    hardware: HardwareConfig,
) -> list[ProfilePreset]:
    matching = [preset for preset in presets if preset.applies_to(hardware)]
    return sorted(matching, key=lambda preset: not preset.hardware_models)


def _control_types(hardware: HardwareConfig) -> dict[str, str]:
    control_types = {button.id: "button" for button in hardware.buttons}
    control_types.update({analog.id: analog.type for analog in hardware.analog_inputs})
    return control_types


def _analog_control_names(action: MappingAction) -> list[str]:
    if action.analog_control_names:
        return list(action.analog_control_names)
    return [action.analog_control_name] if action.analog_control_name else []


def _action_fits_control(action: MappingAction, control_type: str) -> bool:
    if control_type == "button":
        return action.action_type not in _BUTTON_EXCLUDED_ACTIONS
    return action.action_type in _ANALOG_INPUT_ACTIONS


def plan_preset(
    preset: ProfilePreset,
    hardware: HardwareConfig,
    layer: DeviceProfileLayer | None,
    saved_analog_controls: Mapping[str, AnalogControlConfig],
    *,
    require_saved_analog_controls: bool = False,
) -> PresetPlan:
    plan = PresetPlan()
    control_types = _control_types(hardware)
    new_analog_controls: dict[str, AnalogControlConfig] = {}

    for source_id, action in preset.mappings.items():
        control_type = control_types.get(source_id)
        if control_type is None or not _action_fits_control(action, control_type):
            plan.unavailable.append(source_id)
            continue

        existing = layer.mappings.get(source_id) if layer is not None else None
        if existing is not None and existing.action_type != ActionType.PASSTHROUGH:
            plan.kept.append(source_id)
            continue

        needed: dict[str, AnalogControlConfig] = {}
        resolved = True
        for control_name in _analog_control_names(action):
            saved = saved_analog_controls.get(control_name)
            control = saved
            if control is None and not require_saved_analog_controls:
                control = preset.analog_controls.get(control_name)
            if control is None or control.input_type != control_type:
                resolved = False
                break
            if saved is None:
                needed[control_name] = control
        if not resolved:
            plan.unavailable.append(source_id)
            continue

        plan.mappings[source_id] = copy.deepcopy(action)
        new_analog_controls.update(needed)

    plan.new_analog_controls = [copy.deepcopy(control) for control in new_analog_controls.values()]
    return plan


def save_preset_analog_controls(
    preset: ProfilePreset,
    hardware: HardwareConfig,
    layer: DeviceProfileLayer | None,
    analog_control_manager: AnalogControlManager,
) -> dict[str, AnalogControlConfig]:
    plan = plan_preset(preset, hardware, layer, analog_control_manager.get_all_analog_controls())
    for control in plan.new_analog_controls:
        analog_control_manager.save_analog_control(control)
    return analog_control_manager.get_all_analog_controls()


def write_preset_mappings(
    preset: ProfilePreset,
    profile: ProfileConfig,
    hardware: HardwareConfig,
    saved_analog_controls: Mapping[str, AnalogControlConfig],
    *,
    profile_manager: ProfileManager,
) -> PresetPlan:
    hardware_id = hardware.hardware_id
    existing_layer = profile.get_layer(hardware_id)
    plan = plan_preset(
        preset,
        hardware,
        existing_layer,
        saved_analog_controls,
        require_saved_analog_controls=True,
    )
    if not plan.mappings:
        return plan

    layer = profile.ensure_layer(hardware_id)
    previous_mappings = dict(layer.mappings)
    layer.mappings.update(plan.mappings)
    try:
        profile_manager.save_profile(profile)
    except Exception:
        if existing_layer is None:
            profile.device_layers.pop(hardware_id, None)
        else:
            layer.mappings.clear()
            layer.mappings.update(previous_mappings)
        raise
    return plan


def apply_preset(
    preset: ProfilePreset,
    profile: ProfileConfig,
    hardware: HardwareConfig,
    *,
    profile_manager: ProfileManager,
    analog_control_manager: AnalogControlManager,
) -> PresetPlan:
    # Analog controls are saved first so the profile never references a missing one.
    saved_analog_controls = save_preset_analog_controls(
        preset,
        hardware,
        profile.get_layer(hardware.hardware_id),
        analog_control_manager,
    )
    return write_preset_mappings(
        preset,
        profile,
        hardware,
        saved_analog_controls,
        profile_manager=profile_manager,
    )
