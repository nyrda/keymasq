import logging
import re
import tomllib
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from keymasq.common import paths
from keymasq.common.controller_capabilities import controller_control_label
from keymasq.common.devices import canonical_gamepad_button_name
from keymasq.common.model.hardware import (
    AnalogAxisDefinition,
    AnalogInputDefinition,
    HardwareConfig,
)
from keymasq.session.config_loading import load_config_files_sync

log = logging.getLogger("keymasq-session.hardware_presets")
type TomlDict = dict[str, object]

_MODEL_ID = re.compile(r"[0-9a-f]{4}:[0-9a-f]{4}")
_CONTROL_ID = re.compile(r"[a-z0-9_]+")
_AXIS_ROLES = {"stick": ("x", "y"), "axis": ("x",)}
_CALIBRATION_FIELDS = ("minimum", "maximum", "center", "rest")


@dataclass(frozen=True)
class PresetAxis:
    role: str
    evdev: str
    invert: bool = False
    minimum: int | None = None
    maximum: int | None = None
    center: int | None = None
    rest: int | None = None


@dataclass(frozen=True)
class PresetAnalogInput:
    id: str
    label: str
    type: str
    axes: tuple[PresetAxis, ...]


@dataclass(frozen=True)
class HardwarePreset:
    preset_id: str
    name: str
    path: Path
    hardware_models: tuple[str, ...]
    description: str = ""
    device_types: tuple[str, ...] = ()
    button_labels: dict[str, str] = field(default_factory=dict[str, str])
    analog_inputs: tuple[PresetAnalogInput, ...] = ()

    def applies_to(self, model_id: str, device_types: Collection[str] | None = None) -> bool:
        if model_id.lower() not in self.hardware_models:
            return False
        if not self.device_types or device_types is None:
            return True
        return not set(device_types).isdisjoint(self.device_types)


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
        item.strip().lower() if isinstance(item, str) else "" for item in cast(list[object], value)
    ]
    if not all(items):
        raise ValueError(f"{context} must be a list of non-empty strings")
    return tuple(items)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _calibration_is_consistent(
    minimum: int | None,
    maximum: int | None,
    center: int | None,
    rest: int | None,
) -> bool:
    if minimum is not None and maximum is not None and minimum >= maximum:
        return False
    return all(
        (minimum is None or neutral >= minimum) and (maximum is None or neutral <= maximum)
        for neutral in (center, rest)
        if neutral is not None
    )


def _axis_from_toml(data: TomlDict, *, analog_type: str, context: str) -> PresetAxis:
    role = _text(data.get("role")).lower()
    evdev_name = _text(data.get("evdev")).lower()
    if not evdev_name.startswith("abs_"):
        raise ValueError(f"{context}: axis evdev must be an abs_ name")
    calibration: dict[str, int | None] = {}
    for name in _CALIBRATION_FIELDS:
        value = data.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(f"{context}: {name} must be a whole number")
        calibration[name] = value
    if not _calibration_is_consistent(**calibration):
        raise ValueError(f"{context}: minimum, maximum, center and rest contradict each other")
    invert = data.get("invert", False)
    if not isinstance(invert, bool):
        raise ValueError(f"{context}: invert must be true or false")
    if invert and analog_type != "stick":
        raise ValueError(f"{context}: invert is only supported on stick axes")
    return PresetAxis(role=role, evdev=evdev_name, invert=invert, **calibration)


def _analog_input_from_toml(data: TomlDict) -> PresetAnalogInput:
    analog_id = _text(data.get("id")).lower()
    if _CONTROL_ID.fullmatch(analog_id) is None:
        raise ValueError("analog input id must use only a-z, 0-9 and _")
    context = f"analog input '{analog_id}'"
    analog_type = _text(data.get("type")).lower()
    if analog_type not in _AXIS_ROLES:
        raise ValueError(f"{context}: type must be 'stick' or 'axis'")
    raw_axes = data.get("axes")
    if not isinstance(raw_axes, list):
        raise ValueError(f"{context}: axes must be a list")
    axes = tuple(
        _axis_from_toml(_table(axis, f"{context} axis"), analog_type=analog_type, context=context)
        for axis in cast(list[object], raw_axes)
    )
    if tuple(axis.role for axis in axes) != _AXIS_ROLES[analog_type]:
        expected = " and ".join(_AXIS_ROLES[analog_type])
        raise ValueError(f"{context}: a {analog_type} needs axes with role {expected}")
    return PresetAnalogInput(
        id=analog_id,
        label=_text(data.get("label")) or analog_id.replace("_", " ").title(),
        type=analog_type,
        axes=axes,
    )


def hardware_preset_from_toml(data: TomlDict, *, preset_id: str, path: Path) -> HardwarePreset:
    header = _table(data.get("preset"), "preset")
    hardware_models = _string_tuple(header.get("hardware"), "preset.hardware")
    if not hardware_models:
        raise ValueError("preset.hardware must list at least one vendor:product model")
    for model_id in hardware_models:
        if _MODEL_ID.fullmatch(model_id) is None:
            raise ValueError(f"preset.hardware entry '{model_id}' is not a vendor:product model")

    button_labels: dict[str, str] = {}
    for evdev_name, button_data in _table(data.get("buttons"), "buttons").items():
        name = canonical_gamepad_button_name(evdev_name)
        if not name.startswith(("btn_", "key_")):
            raise ValueError(f"button '{evdev_name}' must be a btn_ or key_ name")
        label = _text(_table(button_data, f"button '{evdev_name}'").get("label"))
        if not label:
            raise ValueError(f"button '{evdev_name}': label is missing")
        button_labels[name] = label

    raw_analogs = data.get("analogs", [])
    if not isinstance(raw_analogs, list):
        raise ValueError("analogs must be an array of tables")
    analog_inputs = tuple(
        _analog_input_from_toml(_table(analog, "analog input"))
        for analog in cast(list[object], raw_analogs)
    )
    analog_ids = [analog.id for analog in analog_inputs]
    if len(set(analog_ids)) != len(analog_ids):
        raise ValueError("analog input ids must be unique")
    axis_names = [axis.evdev for analog in analog_inputs for axis in analog.axes]
    if len(set(axis_names)) != len(axis_names):
        raise ValueError("an axis can belong to only one analog input")
    if not button_labels and not analog_inputs:
        raise ValueError("preset describes no controls")

    return HardwarePreset(
        preset_id=preset_id,
        name=_text(header.get("name")) or preset_id,
        path=path,
        hardware_models=hardware_models,
        description=_text(header.get("description")),
        device_types=_string_tuple(header.get("device_types"), "preset.device_types"),
        button_labels=button_labels,
        analog_inputs=analog_inputs,
    )


def _load_hardware_preset_file(path: Path) -> HardwarePreset:
    with open(path, "rb") as preset_file:
        data = cast(TomlDict, tomllib.load(preset_file))
    return hardware_preset_from_toml(data, preset_id=path.stem, path=path)


def load_hardware_presets(search_dirs: Sequence[Path] | None = None) -> list[HardwarePreset]:
    presets: dict[str, HardwarePreset] = {}
    for preset_dir in paths.hardware_preset_search_dirs() if search_dirs is None else search_dirs:
        for _, preset in load_config_files_sync(
            preset_dir,
            config_kind="hardware preset",
            strict=False,
            load_config=_load_hardware_preset_file,
            logger=log,
            failure_log_message="Failed to load hardware preset %s: %s",
            sort_paths=True,
        ):
            presets[preset.preset_id] = preset
    return sorted(presets.values(), key=lambda preset: (preset.name.lower(), preset.preset_id))


def hardware_presets_for_model(
    presets: Sequence[HardwarePreset],
    model_id: str,
    device_types: Collection[str] | None = None,
) -> list[HardwarePreset]:
    return [preset for preset in presets if preset.applies_to(model_id, device_types)]


def _locate_axes(
    inputs: Sequence[AnalogInputDefinition],
    definition: PresetAnalogInput,
) -> list[tuple[AnalogInputDefinition, AnalogAxisDefinition]] | None:
    for source in dict.fromkeys(analog.source for analog in inputs):
        detected = {
            axis.evdev.lower(): (analog, axis)
            for analog in inputs
            if analog.source == source
            for axis in analog.axes
        }
        if all(axis.evdev in detected for axis in definition.axes):
            return [detected[axis.evdev] for axis in definition.axes]
    return None


def _preset_axis(
    preset_axis: PresetAxis,
    detected: AnalogAxisDefinition,
    donor: AnalogInputDefinition,
    analog_type: str,
) -> AnalogAxisDefinition:
    minimum = detected.minimum if preset_axis.minimum is None else preset_axis.minimum
    maximum = detected.maximum if preset_axis.maximum is None else preset_axis.maximum
    center = rest = None
    if analog_type == "stick":
        center = preset_axis.center
        range_overridden = preset_axis.minimum is not None or preset_axis.maximum is not None
        if center is None and donor.type == "stick" and not range_overridden:
            center = detected.center
        if center is None and minimum is not None and maximum is not None and minimum < maximum:
            center = (minimum + maximum) // 2
    else:
        rest = preset_axis.rest
        if rest is None and donor.type == "axis":
            rest = detected.rest
    return AnalogAxisDefinition(
        role=preset_axis.role,
        evdev=detected.evdev,
        evdev_code=detected.evdev_code,
        minimum=minimum,
        maximum=maximum,
        center=center,
        rest=rest,
        invert=preset_axis.invert,
    )


def _unique_id(base: str, used: set[str]) -> str:
    candidate, suffix = base, 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def apply_hardware_preset(preset: HardwarePreset, config: HardwareConfig) -> list[str]:
    """Rewrite a detected layout in place and return the preset controls it lacks."""
    unavailable: list[str] = []

    for evdev_name, label in preset.button_labels.items():
        matches = [
            button
            for button in config.buttons
            if canonical_gamepad_button_name(button.evdev) == evdev_name
        ]
        if not matches:
            unavailable.append(evdev_name)
        for button in matches:
            button.label = label

    reserved_ids = {button.id for button in config.buttons}
    reserved_ids.update(sensor.id for sensor in config.motion_sensors)
    inputs = list(config.analog_inputs)
    for definition in preset.analog_inputs:
        located = _locate_axes(inputs, definition)
        if located is None:
            unavailable.append(definition.id)
            continue
        donors = list({id(donor): donor for donor, _ in located}.values())
        donor_keys = {id(donor) for donor in donors}
        used_ids = reserved_ids | {analog.id for analog in inputs if id(analog) not in donor_keys}
        if definition.id in used_ids:
            unavailable.append(definition.id)
            continue

        axes = [
            _preset_axis(preset_axis, detected, donor, definition.type)
            for preset_axis, (donor, detected) in zip(definition.axes, located, strict=True)
        ]
        if not all(
            _calibration_is_consistent(axis.minimum, axis.maximum, axis.center, axis.rest)
            for axis in axes
        ):
            unavailable.append(definition.id)
            continue
        used_ids.add(definition.id)

        claimed = {id(axis) for _, axis in located}
        replacements = [
            AnalogInputDefinition(
                id=definition.id,
                label=definition.label,
                type=definition.type,
                source=located[0][0].source,
                axes=axes,
            )
        ]
        for donor in donors:
            for axis in donor.axes:
                if id(axis) in claimed:
                    continue
                replacements.append(
                    AnalogInputDefinition(
                        id=_unique_id(axis.evdev.lower(), used_ids),
                        label=controller_control_label(axis.evdev.lower()),
                        type="axis",
                        source=donor.source,
                        axes=[
                            AnalogAxisDefinition(
                                role="x",
                                evdev=axis.evdev,
                                evdev_code=axis.evdev_code,
                                minimum=axis.minimum,
                                maximum=axis.maximum,
                            )
                        ],
                    )
                )
        position = next(index for index, analog in enumerate(inputs) if id(analog) in donor_keys)
        inputs = [analog for analog in inputs if id(analog) not in donor_keys]
        inputs[position:position] = replacements

    config.analog_inputs = inputs
    return unavailable
