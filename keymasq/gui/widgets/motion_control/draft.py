"""UI-independent Motion Control editor drafts."""

from dataclasses import dataclass

from keymasq.common.model.motion import (
    MotionAxisRoutingConfig,
    MotionControlConfig,
    MotionGamepadConfig,
    MotionMouseConfig,
    MotionTiltConfig,
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
class MotionTiltDraft:
    reference: str
    pitch: str
    roll: str
    deadzone_deg: float
    full_scale_deg: float
    smoothing: float
    response_curve: float
    invert_x: bool
    invert_y: bool
    speed_x: float
    speed_y: float
    area_radius_x: float
    area_radius_y: float
    drag_center: bool

    @classmethod
    def from_config(cls, config: MotionTiltConfig) -> "MotionTiltDraft":
        return cls(
            reference=config.reference,
            pitch=config.pitch,
            roll=config.roll,
            deadzone_deg=config.deadzone_deg,
            full_scale_deg=config.full_scale_deg,
            smoothing=config.smoothing,
            response_curve=config.response_curve,
            invert_x=config.invert_x,
            invert_y=config.invert_y,
            speed_x=config.speed_x,
            speed_y=config.speed_y,
            area_radius_x=config.area_radius_x,
            area_radius_y=config.area_radius_y,
            drag_center=config.drag_center,
        )

    def to_config(self) -> MotionTiltConfig:
        return MotionTiltConfig(
            reference=self.reference,
            pitch=self.pitch,
            roll=self.roll,
            deadzone_deg=self.deadzone_deg,
            full_scale_deg=self.full_scale_deg,
            smoothing=self.smoothing,
            response_curve=self.response_curve,
            invert_x=self.invert_x,
            invert_y=self.invert_y,
            speed_x=self.speed_x,
            speed_y=self.speed_y,
            area_radius_x=self.area_radius_x,
            area_radius_y=self.area_radius_y,
            drag_center=self.drag_center,
        )


@dataclass(frozen=True, slots=True)
class MotionControlDraft:
    name: str
    description: str
    mode: str
    axis_routing: MotionAxisRoutingDraft
    mouse: MotionMouseDraft
    gamepad: MotionGamepadDraft
    tilt: MotionTiltDraft

    @classmethod
    def from_config(cls, config: MotionControlConfig) -> "MotionControlDraft":
        return cls(
            name=config.name,
            description=config.description or "",
            mode=config.mode,
            axis_routing=MotionAxisRoutingDraft.from_config(config.axis_routing),
            mouse=MotionMouseDraft.from_config(config.mouse),
            gamepad=MotionGamepadDraft.from_config(config.gamepad),
            tilt=MotionTiltDraft.from_config(config.tilt),
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
            tilt=self.tilt.to_config(),
        )

    def is_pristine_new_draft(self) -> bool:
        return self == self.new()
