"""UI-independent analog-control editor drafts."""

from dataclasses import dataclass

from keymasq.common.model.analog import (
    SAME_DEVICE_OUTPUT_ID,
    AnalogActionThreshold,
    AnalogControlConfig,
    AnalogGamepadOutputConfig,
    AnalogMouseMotionConfig,
)


def clone_threshold(threshold: AnalogActionThreshold) -> AnalogActionThreshold:
    return AnalogActionThreshold(
        axis=threshold.axis,
        trigger_min=threshold.trigger_min,
        trigger_max=threshold.trigger_max,
        release_min=threshold.release_min,
        release_max=threshold.release_max,
        actions=list(threshold.actions),
    )


def mode_for_config(config: AnalogControlConfig) -> str:
    if config.gamepad_output.enabled:
        return "gamepad"
    if config.mouse_motion.enabled and config.mouse_motion.mode == "area":
        return "mouse_area"
    if config.thresholds:
        return "digital"
    return "mouse"


@dataclass(frozen=True, slots=True)
class MouseDraft:
    speed: float
    speed_x: float
    speed_y: float
    area_radius_x: float
    area_radius_y: float
    area_start_enabled: bool
    area_start_x: int
    area_start_y: int
    deadzone: float
    sensitivity: float
    response_curve: float
    direction: str
    invert_x: bool
    invert_y: bool
    tick_ms: int = 8

    @classmethod
    def from_config(cls, config: AnalogMouseMotionConfig) -> "MouseDraft":
        return cls(
            speed=config.speed,
            speed_x=config.speed_x if config.speed_x is not None else config.speed,
            speed_y=config.speed_y if config.speed_y is not None else config.speed,
            area_radius_x=config.area_radius_x,
            area_radius_y=config.area_radius_y,
            area_start_enabled=config.area_start_enabled,
            area_start_x=config.area_start_x,
            area_start_y=config.area_start_y,
            deadzone=config.deadzone,
            sensitivity=config.sensitivity,
            response_curve=config.response_curve,
            direction=config.direction,
            invert_x=config.invert_x,
            invert_y=config.invert_y,
            tick_ms=config.tick_ms,
        )

    def to_config(self, *, input_type: str, mode: str) -> AnalogMouseMotionConfig:
        return AnalogMouseMotionConfig(
            enabled=mode in {"mouse", "mouse_area"},
            mode="area" if mode == "mouse_area" else "velocity",
            speed=self.speed,
            speed_x=self.speed_x,
            speed_y=self.speed_y,
            area_radius_x=self.area_radius_x,
            area_radius_y=self.area_radius_y,
            area_start_enabled=self.area_start_enabled,
            area_start_x=self.area_start_x,
            area_start_y=self.area_start_y,
            deadzone=self.deadzone,
            sensitivity=self.sensitivity,
            response_curve=self.response_curve,
            direction=self.direction,
            invert_x=self.invert_x,
            invert_y=input_type != "axis" and self.invert_y,
            tick_ms=self.tick_ms,
        )


@dataclass(frozen=True, slots=True)
class GamepadDraft:
    output_id: str | None
    deadzone: float
    target: str
    target_analog_id: str | None
    output_rest: int | None
    output_direction: str
    invert_x: bool
    invert_y: bool
    sensitivity: float
    response_curve: float
    target_axis: str | None = None

    @classmethod
    def from_config(cls, config: AnalogGamepadOutputConfig) -> "GamepadDraft":
        invert_x = config.output_invert_x
        if config.output_direction == "both" and config.output_invert:
            invert_x = True
        return cls(
            output_id=config.output_id,
            deadzone=config.deadzone,
            target=config.target,
            target_analog_id=config.target_analog_id,
            output_rest=config.output_rest,
            target_axis=config.target_axis,
            output_direction=config.output_direction,
            invert_x=invert_x,
            invert_y=config.output_invert_y,
            sensitivity=config.sensitivity,
            response_curve=config.response_curve,
        )

    def to_config(self, *, input_type: str, mode: str) -> AnalogGamepadOutputConfig:
        is_axis = input_type == "axis"
        direction = self.output_direction if is_axis else "max"
        return AnalogGamepadOutputConfig(
            enabled=mode == "gamepad",
            output_id=self.output_id,
            deadzone=self.deadzone,
            target=self.target,
            target_analog_id=self.target_analog_id,
            target_axis=self.target_axis,
            output_rest=self.output_rest if is_axis else None,
            output_direction=direction,
            output_invert=is_axis
            and (direction == "min" or (direction == "both" and self.invert_x)),
            output_invert_x=not is_axis and self.invert_x,
            output_invert_y=not is_axis and self.invert_y,
            sensitivity=self.sensitivity,
            response_curve=self.response_curve,
        )


@dataclass(frozen=True, slots=True)
class ControlDraft:
    name: str
    description: str
    input_type: str
    mode: str
    mouse: MouseDraft
    gamepad: GamepadDraft
    thresholds: tuple[AnalogActionThreshold, ...] = ()

    @classmethod
    def from_config(cls, config: AnalogControlConfig) -> "ControlDraft":
        return cls(
            name=config.name,
            description=config.description or "",
            input_type=config.input_type,
            mode=mode_for_config(config),
            mouse=MouseDraft.from_config(config.mouse_motion),
            gamepad=GamepadDraft.from_config(config.gamepad_output),
            thresholds=tuple(clone_threshold(item) for item in config.thresholds),
        )

    @classmethod
    def new(cls) -> "ControlDraft":
        return cls.from_config(
            AnalogControlConfig(
                name="New Analog Control",
                gamepad_output=AnalogGamepadOutputConfig(output_id=SAME_DEVICE_OUTPUT_ID),
            )
        )

    def to_config(self) -> AnalogControlConfig:
        name = self.name.strip()
        if not name:
            raise ValueError("analog control name is required")
        return AnalogControlConfig(
            name=name,
            description=self.description.strip() or None,
            input_type=self.input_type,
            mouse_motion=self.mouse.to_config(input_type=self.input_type, mode=self.mode),
            gamepad_output=self.gamepad.to_config(input_type=self.input_type, mode=self.mode),
            thresholds=list(self.thresholds) if self.mode == "digital" else [],
        )

    def is_pristine_new_control(self) -> bool:
        return self == self.new()
