"""UI-independent Motion Control editor drafts."""

from dataclasses import dataclass

from keymasq.common.model.motion import (
    MotionAxisRoutingConfig,
    MotionControlConfig,
    MotionGamepadConfig,
    MotionMouseConfig,
)


@dataclass(frozen=True, slots=True)
class MotionAxisRoutingDraft:
    yaw: str
    pitch: str
    roll: str

    @classmethod
    def from_config(cls, config: MotionAxisRoutingConfig) -> "MotionAxisRoutingDraft":
        return cls(yaw=config.yaw, pitch=config.pitch, roll=config.roll)

    def to_config(self) -> MotionAxisRoutingConfig:
        return MotionAxisRoutingConfig(yaw=self.yaw, pitch=self.pitch, roll=self.roll)


@dataclass(frozen=True, slots=True)
class MotionMouseDraft:
    sensitivity_x: float
    sensitivity_y: float
    deadzone_dps: float
    smoothing: float
    response_curve: float
    invert_x: bool
    invert_y: bool

    @classmethod
    def from_config(cls, config: MotionMouseConfig) -> "MotionMouseDraft":
        return cls(
            sensitivity_x=config.sensitivity_x,
            sensitivity_y=config.sensitivity_y,
            deadzone_dps=config.deadzone_dps,
            smoothing=config.smoothing,
            response_curve=config.response_curve,
            invert_x=config.invert_x,
            invert_y=config.invert_y,
        )

    def to_config(self) -> MotionMouseConfig:
        return MotionMouseConfig(
            sensitivity_x=self.sensitivity_x,
            sensitivity_y=self.sensitivity_y,
            deadzone_dps=self.deadzone_dps,
            smoothing=self.smoothing,
            response_curve=self.response_curve,
            invert_x=self.invert_x,
            invert_y=self.invert_y,
        )


@dataclass(frozen=True, slots=True)
class MotionGamepadDraft:
    output_id: str | None
    target: str
    target_analog_id: str | None
    max_rate_dps: float
    deadzone_dps: float
    smoothing: float
    response_curve: float
    invert_x: bool
    invert_y: bool

    @classmethod
    def from_config(cls, config: MotionGamepadConfig) -> "MotionGamepadDraft":
        return cls(
            output_id=config.output_id,
            target=config.target,
            target_analog_id=config.target_analog_id,
            max_rate_dps=config.max_rate_dps,
            deadzone_dps=config.deadzone_dps,
            smoothing=config.smoothing,
            response_curve=config.response_curve,
            invert_x=config.invert_x,
            invert_y=config.invert_y,
        )

    def to_config(self) -> MotionGamepadConfig:
        return MotionGamepadConfig(
            output_id=self.output_id,
            target=self.target,
            target_analog_id=self.target_analog_id,
            max_rate_dps=self.max_rate_dps,
            deadzone_dps=self.deadzone_dps,
            smoothing=self.smoothing,
            response_curve=self.response_curve,
            invert_x=self.invert_x,
            invert_y=self.invert_y,
        )


@dataclass(frozen=True, slots=True)
class MotionControlDraft:
    name: str
    description: str
    mode: str
    axis_routing: MotionAxisRoutingDraft
    mouse: MotionMouseDraft
    gamepad: MotionGamepadDraft

    @classmethod
    def from_config(cls, config: MotionControlConfig) -> "MotionControlDraft":
        return cls(
            name=config.name,
            description=config.description or "",
            mode=config.mode,
            axis_routing=MotionAxisRoutingDraft.from_config(config.axis_routing),
            mouse=MotionMouseDraft.from_config(config.mouse),
            gamepad=MotionGamepadDraft.from_config(config.gamepad),
        )

    @classmethod
    def new(cls) -> "MotionControlDraft":
        return cls.from_config(MotionControlConfig(name="New Motion Control"))

    def to_config(self) -> MotionControlConfig:
        name = self.name.strip()
        if not name:
            raise ValueError("motion control name is required")
        return MotionControlConfig(
            name=name,
            description=self.description.strip() or None,
            mode=self.mode,
            axis_routing=self.axis_routing.to_config(),
            mouse=self.mouse.to_config(),
            gamepad=self.gamepad.to_config(),
        )

    def is_pristine_new_draft(self) -> bool:
        return self == self.new()
