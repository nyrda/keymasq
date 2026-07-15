from dataclasses import dataclass

from keymasq.common.model.analog import (
    AnalogActionThreshold,
    AnalogControlConfig,
    analog_control_primary_mode,
)


@dataclass(frozen=True, slots=True)
class SelectOption:
    item_id: str
    label: str
    input_type: str | None = None


def analog_control_search_text(
    config: AnalogControlConfig | None,
    name: str,
    group_title: str,
) -> str:
    if config is None:
        return f"{name} {group_title}"
    output_parts: list[str] = []
    if config.mouse_motion.enabled:
        output_parts.append("mouse")
    if config.gamepad_output.enabled:
        output_parts.append("gamepad output")
    if config.thresholds:
        output_parts.append(f"{len(config.thresholds)} ranges thresholds")
    return " ".join(
        [
            str(config.name or ""),
            str(config.description or ""),
            group_title,
            config.input_type,
            analog_control_primary_mode(config),
            " ".join(output_parts),
        ]
    )


INPUT_TYPE_OPTIONS = (
    SelectOption("stick", "Stick"),
    SelectOption("axis", "1D Axis / Trigger"),
)
_MODE_OPTIONS = (
    SelectOption("mouse", "Mouse Movement", "stick"),
    SelectOption("mouse_area", "Mouse Area", "stick"),
    SelectOption("digital", "Digital Actions", "stick"),
    SelectOption("gamepad", "Analog Output", "stick"),
    SelectOption("digital", "Digital Actions", "axis"),
    SelectOption("gamepad", "Analog Output", "axis"),
    SelectOption("mouse", "Mouse Movement", "axis"),
)
_GAMEPAD_OUTPUT_TARGET_OPTIONS = (
    SelectOption("same", "Same Stick", "stick"),
    SelectOption("left", "Left Stick", "stick"),
    SelectOption("right", "Right Stick", "stick"),
    SelectOption("same", "Same Axis", "axis"),
    SelectOption("left", "Left Trigger", "axis"),
    SelectOption("right", "Right Trigger", "axis"),
)
MOUSE_DEADZONE_DEFAULT = 0.15
_CONTROL_GROUPS = (("axis", "1D Axes / Triggers"), ("stick", "Sticks"))
AXIS_ITEMS = ("x", "y")


def compute_hysteresis(threshold: AnalogActionThreshold) -> float:
    margin_low = threshold.trigger_min - threshold.release_min
    margin_high = threshold.release_max - threshold.trigger_max
    if margin_low < 0.001 and margin_high >= 0.001:
        return round(margin_high, 2)
    if margin_high < 0.001 and margin_low >= 0.001:
        return round(margin_low, 2)
    return round(min(margin_low, margin_high), 2)


def _clamp_threshold_value(value: float) -> float:
    return max(-1.0, min(1.0, float(value)))


def to_percent(value: float) -> float:
    return round(_clamp_threshold_value(value) * 100.0, 0)


def from_percent(value: float) -> float:
    return round(max(-100.0, min(100.0, float(value))) / 100.0, 2)


def group_analog_control_names(
    names: list[str],
    configs: dict[str, AnalogControlConfig],
) -> list[tuple[str, list[str]]]:
    grouped: list[tuple[str, list[str]]] = []
    used: set[str] = set()
    for input_type, title in _CONTROL_GROUPS:
        group_names = [
            name
            for name in names
            if (config := configs.get(name)) is not None and config.input_type == input_type
        ]
        if group_names:
            grouped.append((title, group_names))
            used.update(group_names)

    other_names = [name for name in names if name not in used]
    if other_names:
        grouped.append(("Other", other_names))
    return grouped


def _option_ids(options: tuple[SelectOption, ...]) -> tuple[str, ...]:
    return tuple(option.item_id for option in options)


def option_labels(options: tuple[SelectOption, ...]) -> tuple[str, ...]:
    return tuple(option.label for option in options)


def _option_index(options: tuple[SelectOption, ...], item_id: str) -> int:
    for index, option in enumerate(options):
        if option.item_id == item_id:
            return index
    raise ValueError(f"unknown option id: {item_id}")


def _options_for_input_type(
    options: tuple[SelectOption, ...],
    input_type: str,
) -> tuple[SelectOption, ...]:
    return tuple(option for option in options if option.input_type == input_type)


def input_type_index(input_type: str) -> int:
    return _option_index(INPUT_TYPE_OPTIONS, input_type)


def _mode_options_for_input_type(input_type: str) -> tuple[SelectOption, ...]:
    options = _options_for_input_type(_MODE_OPTIONS, input_type)
    if options:
        return options
    return _options_for_input_type(_MODE_OPTIONS, "stick")


def mode_items_for_input_type(input_type: str) -> tuple[str, ...]:
    return _option_ids(_mode_options_for_input_type(input_type))


def mode_labels_for_input_type(input_type: str) -> tuple[str, ...]:
    return option_labels(_mode_options_for_input_type(input_type))


def mode_index_for_input_type(input_type: str, mode: str) -> int:
    return _option_index(_mode_options_for_input_type(input_type), mode)


def gamepad_output_target_options_for_input_type(
    input_type: str,
) -> tuple[SelectOption, ...]:
    options = _options_for_input_type(_GAMEPAD_OUTPUT_TARGET_OPTIONS, input_type)
    if options:
        return options
    return _options_for_input_type(_GAMEPAD_OUTPUT_TARGET_OPTIONS, "stick")


def gamepad_output_target_label_for_input_type(input_type: str, target: str) -> str:
    options = gamepad_output_target_options_for_input_type(input_type)
    for option in options:
        if option.item_id == target:
            return option.label
    return options[0].label
