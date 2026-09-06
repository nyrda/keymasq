"""Analog control models, curves, and validation."""

from dataclasses import dataclass, field, replace
from typing import cast

from keymasq.common.gamepad_axes import normalize_gamepad_axis_target
from keymasq.common.model.actions import MappingAction, normalize_output_id
from keymasq.common.model.core import ActionType
from keymasq.common.virtual_devices import SAME_DEVICE_OUTPUT_ID as SAME_DEVICE_OUTPUT_ID

ANALOG_THRESHOLD_ACTION_TYPES = frozenset(
    action_type
    for action_type in ActionType
    if action_type
    not in {
        ActionType.PASSTHROUGH,
        ActionType.SUPPRESS,
        ActionType.ANALOG_CONTROL,
        ActionType.SUPERKEY,
    }
)

ANALOG_MOUSE_DIRECTIONS = frozenset({"left", "right", "up", "down", "horizontal", "vertical"})
ANALOG_MOUSE_MODES = frozenset({"velocity", "area"})
ANALOG_GAMEPAD_OUTPUT_TARGETS = frozenset({"same", "left", "right", "analog", "axis"})
ANALOG_GAMEPAD_OUTPUT_DIRECTIONS = frozenset({"min", "max", "both"})
MIN_ANALOG_GAMEPAD_OUTPUT_SENSITIVITY = 0.1
MAX_ANALOG_GAMEPAD_OUTPUT_SENSITIVITY = 2.0
MIN_ANALOG_GAMEPAD_OUTPUT_RESPONSE_CURVE = 0.25
MAX_ANALOG_GAMEPAD_OUTPUT_RESPONSE_CURVE = 4.0


def clamp_analog_value(value: object) -> float:
    return max(-1.0, min(1.0, float(cast(int | float | str | bytes, value))))


def analog_gamepad_output_distance(
    value: float,
    *,
    deadzone: float,
    sensitivity: float,
    response_curve: float,
) -> float:
    value = max(0.0, min(1.0, float(value)))
    deadzone = max(0.0, min(0.95, float(deadzone)))
    sensitivity = max(
        MIN_ANALOG_GAMEPAD_OUTPUT_SENSITIVITY,
        min(MAX_ANALOG_GAMEPAD_OUTPUT_SENSITIVITY, float(sensitivity)),
    )
    response_curve = max(
        MIN_ANALOG_GAMEPAD_OUTPUT_RESPONSE_CURVE,
        min(MAX_ANALOG_GAMEPAD_OUTPUT_RESPONSE_CURVE, float(response_curve)),
    )
    if value <= deadzone:
        return 0.0
    normalized = (value - deadzone) / max(0.001, 1.0 - deadzone)
    return max(0.0, min(1.0, (normalized**response_curve) * sensitivity))


@dataclass
class AnalogMouseMotionConfig:
    enabled: bool = False
    mode: str = "velocity"
    speed: float = 900.0
    speed_x: float | None = None
    speed_y: float | None = None
    area_radius_x: float = 400.0
    area_radius_y: float = 400.0
    area_start_enabled: bool = False
    area_start_x: int = 0
    area_start_y: int = 0
    deadzone: float = 0.15
    sensitivity: float = 1.0
    response_curve: float = 1.0
    direction: str = "right"
    invert_x: bool = False
    invert_y: bool = False
    tick_ms: int = 8

    def __post_init__(self) -> None:
        self.mode = str(self.mode or "velocity").lower()
        if self.mode not in ANALOG_MOUSE_MODES:
            self.mode = "velocity"
        self.speed = max(0.0, float(self.speed))
        self.speed_x = self.speed if self.speed_x is None else max(0.0, float(self.speed_x))
        self.speed_y = self.speed if self.speed_y is None else max(0.0, float(self.speed_y))
        self.area_radius_x = max(0.0, float(self.area_radius_x))
        self.area_radius_y = max(0.0, float(self.area_radius_y))
        self.area_start_enabled = bool(self.area_start_enabled)
        self.area_start_x = int(self.area_start_x)
        self.area_start_y = int(self.area_start_y)
        self.deadzone = max(0.0, min(0.95, float(self.deadzone)))
        self.sensitivity = max(
            MIN_ANALOG_GAMEPAD_OUTPUT_SENSITIVITY,
            min(MAX_ANALOG_GAMEPAD_OUTPUT_SENSITIVITY, float(self.sensitivity)),
        )
        self.response_curve = max(
            MIN_ANALOG_GAMEPAD_OUTPUT_RESPONSE_CURVE,
            min(MAX_ANALOG_GAMEPAD_OUTPUT_RESPONSE_CURVE, float(self.response_curve)),
        )
        self.direction = str(self.direction or "right").lower()
        if self.direction not in ANALOG_MOUSE_DIRECTIONS:
            self.direction = "right"
        self.tick_ms = max(1, int(self.tick_ms))


@dataclass
class AnalogGamepadOutputConfig:
    enabled: bool = False
    output_id: str | None = None
    deadzone: float = 0.0
    target: str = "same"
    target_analog_id: str | None = None
    target_axis: str | None = None
    output_rest: int | None = None
    output_direction: str = ""
    output_invert: bool = False
    output_invert_x: bool = False
    output_invert_y: bool = False
    sensitivity: float = 1.0
    response_curve: float = 1.0

    def __post_init__(self) -> None:
        self.output_id = normalize_output_id(self.output_id)
        self.deadzone = max(0.0, min(0.95, float(self.deadzone)))
        self.target = str(self.target or "same").lower()
        if self.target not in ANALOG_GAMEPAD_OUTPUT_TARGETS:
            self.target = "same"
        self.target_analog_id = normalize_output_id(self.target_analog_id)
        if self.target != "analog":
            self.target_analog_id = None
        if self.target == "axis":
            normalized_axis = normalize_gamepad_axis_target(self.target_axis)
            if normalized_axis is None:
                raise ValueError("analog output target_axis must be a valid ABS axis")
            self.target_axis = normalized_axis
        else:
            self.target_axis = None
        if self.output_rest is not None:
            self.output_rest = int(self.output_rest)
        output_invert = bool(self.output_invert)
        self.output_invert_x = bool(self.output_invert_x)
        self.output_invert_y = bool(self.output_invert_y)
        self.output_direction = str(self.output_direction or "").lower()
        if self.output_direction not in ANALOG_GAMEPAD_OUTPUT_DIRECTIONS:
            self.output_direction = "min" if output_invert else "max"
        if self.output_direction == "min":
            self.output_invert = True
        elif self.output_direction == "max":
            self.output_invert = False
        else:
            self.output_invert = output_invert
        self.sensitivity = max(
            MIN_ANALOG_GAMEPAD_OUTPUT_SENSITIVITY,
            min(MAX_ANALOG_GAMEPAD_OUTPUT_SENSITIVITY, float(self.sensitivity)),
        )
        self.response_curve = max(
            MIN_ANALOG_GAMEPAD_OUTPUT_RESPONSE_CURVE,
            min(MAX_ANALOG_GAMEPAD_OUTPUT_RESPONSE_CURVE, float(self.response_curve)),
        )


@dataclass
class AnalogActionThreshold:
    axis: str
    trigger_min: float
    trigger_max: float
    release_min: float
    release_max: float
    actions: list[MappingAction] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.axis = str(self.axis or "").lower()
        self.trigger_min = clamp_analog_value(self.trigger_min)
        self.trigger_max = clamp_analog_value(self.trigger_max)
        self.release_min = clamp_analog_value(self.release_min)
        self.release_max = clamp_analog_value(self.release_max)


@dataclass
class AnalogControlConfig:
    name: str
    description: str | None = None
    input_type: str = "stick"
    mouse_motion: AnalogMouseMotionConfig = field(default_factory=AnalogMouseMotionConfig)
    gamepad_output: AnalogGamepadOutputConfig = field(default_factory=AnalogGamepadOutputConfig)
    thresholds: list[AnalogActionThreshold] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.input_type = str(self.input_type or "stick").lower()
        validate_analog_control_config(self)


def analog_control_primary_mode(config: AnalogControlConfig) -> str:
    if config.gamepad_output.enabled:
        return "gamepad"
    if config.thresholds:
        return "digital"
    if config.mouse_motion.enabled:
        return "mouse"
    return "none"


def normalize_analog_control_features(config: AnalogControlConfig) -> AnalogControlConfig:
    mode = analog_control_primary_mode(config)
    if mode == "gamepad":
        if config.mouse_motion.enabled or config.thresholds:
            return replace(
                config,
                mouse_motion=replace(config.mouse_motion, enabled=False),
                thresholds=[],
            )
    elif mode == "digital" and config.mouse_motion.enabled:
        return replace(config, mouse_motion=replace(config.mouse_motion, enabled=False))
    return config


def validate_analog_control_config(config: AnalogControlConfig) -> None:
    if not str(config.name or "").strip():
        raise ValueError("analog control name is required")
    if config.input_type not in {"stick", "axis"}:
        raise ValueError("analog control input_type must be 'stick' or 'axis'")
    if config.input_type == "axis" and config.mouse_motion.mode == "area":
        raise ValueError("analog mouse area mode requires a stick control")
    if config.gamepad_output.target == "axis" and config.input_type != "axis":
        raise ValueError("an individual output axis requires a 1D axis control")
    for index, threshold in enumerate(config.thresholds, start=1):
        allowed_axes = {"x", "y"} if config.input_type == "stick" else {"x"}
        if threshold.axis not in allowed_axes:
            if config.input_type == "axis":
                raise ValueError(f"threshold {index} axis must be 'x' for axis controls")
            raise ValueError(f"threshold {index} axis must be 'x' or 'y'")
        if threshold.trigger_min > threshold.trigger_max:
            raise ValueError(f"threshold {index} activation range is invalid")
        if threshold.release_min > threshold.release_max:
            raise ValueError(f"threshold {index} release range is invalid")
        if (
            threshold.trigger_min < threshold.release_min
            or threshold.trigger_max > threshold.release_max
        ):
            raise ValueError(f"threshold {index} activation range must be inside release range")
        for action in threshold.actions:
            if action.action_type not in ANALOG_THRESHOLD_ACTION_TYPES:
                raise ValueError(
                    f"invalid analog threshold action type: {action.action_type.value}"
                )
